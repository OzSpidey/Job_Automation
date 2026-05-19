"""
Earnings Vol Crush strategy.

Edge: equity options systematically overprice the earnings move. Short the implied
move premium (sell the inflated IV) one day before earnings, exit 30 min after open.

Implementation: defined-risk iron condor (not naked straddle) around the expected
earnings move, entered the day before, exited the next morning.

Filters (all must pass):
  - IV ≥ 30% (high-IV names only; low-IV earnings moves can spike more than expected)
  - Implied move ≥ 1.1× average historical move (need the premium to be there)
  - Underlying price ≤ $500 (capital constraint)
  - No existing position in same underlying
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .base import ExitReason, ExitSignal, Strategy
from ..models import OptionChain, OptionLeg, Position, Right, Side, SignalResult
from ..signals.earnings import EarningsCalendar
from ..surface.interpolator import VolSurface

log = logging.getLogger(__name__)

_STRAT = "earnings_crush"


class EarningsCrush(Strategy):
    name = _STRAT

    def __init__(
        self,
        min_iv: float = 0.30,
        min_implied_move_premium: float = 1.10,
        short_delta: float = 0.16,
        wing_width: int = 5,
        max_underlying_price: float = 500.0,
        exit_minutes_after_open: int = 30,
    ):
        self.min_iv = min_iv
        self.min_implied_move_premium = min_implied_move_premium
        self.short_delta = short_delta
        self.wing_width = wing_width
        self.max_underlying_price = max_underlying_price
        self.exit_minutes_after_open = exit_minutes_after_open
        self._earnings = EarningsCalendar()

    # ── signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        chain: OptionChain,
        surface: Optional[VolSurface],
        open_positions: list[Position],
        nav: float,
    ) -> list[SignalResult]:
        # Must have earnings tomorrow
        days_out = self._earnings.days_to_earnings(chain.symbol)
        if days_out != 1:   # exactly 1 trading day away
            return []

        if chain.underlying_price > self.max_underlying_price:
            log.debug("%s earnings crush: price %.0f > max %.0f",
                      chain.symbol, chain.underlying_price, self.max_underlying_price)
            return []

        already_on = any(p.underlying == chain.symbol and p.strategy == self.name
                         and p.status.value == "OPEN" for p in open_positions)
        if already_on:
            return []

        # Use the nearest weekly expiry (1-2 DTE after earnings)
        expiry = chain.nearest_expiry_by_dte(1, 5)
        if expiry is None:
            return []

        slice_contracts = chain.by_expiry(expiry)
        if not slice_contracts:
            return []

        avg_iv = sum(c.iv for c in slice_contracts if c.iv > 0) / max(len(slice_contracts), 1)
        if avg_iv < self.min_iv:
            log.debug("%s earnings crush: IV %.1f%% < min %.1f%%",
                      chain.symbol, avg_iv * 100, self.min_iv * 100)
            return []

        implied_move = avg_iv * (expiry - date.today()).days**0.5 / 252**0.5
        hist_move = self._earnings.historical_move(chain.symbol)

        if implied_move < hist_move * self.min_implied_move_premium:
            log.debug(
                "%s earnings crush: implied move %.1f%% < %.1f× hist move %.1f%%",
                chain.symbol, implied_move * 100,
                self.min_implied_move_premium, hist_move * 100,
            )
            return []

        short_put  = chain.near_delta(self.short_delta, Right.PUT,  expiry)
        short_call = chain.near_delta(self.short_delta, Right.CALL, expiry)
        if not short_put or not short_call:
            return []

        strike_step = _find_strike_step(chain, expiry)
        long_put_strike  = short_put.strike  - self.wing_width * strike_step
        long_call_strike = short_call.strike + self.wing_width * strike_step

        long_put  = _find_by_strike(chain, long_put_strike,  Right.PUT,  expiry)
        long_call = _find_by_strike(chain, long_call_strike, Right.CALL, expiry)
        if not long_put or not long_call:
            return []

        qty = 1
        credit = (short_put.mid + short_call.mid - long_put.mid - long_call.mid) * qty * 100
        if credit <= 0:
            return []

        max_loss = self.wing_width * strike_step * 100 - credit

        legs = [
            OptionLeg(chain.symbol, expiry, short_put.strike,  Right.PUT,  Side.SELL, qty),
            OptionLeg(chain.symbol, expiry, long_put_strike,   Right.PUT,  Side.BUY,  qty),
            OptionLeg(chain.symbol, expiry, short_call.strike, Right.CALL, Side.SELL, qty),
            OptionLeg(chain.symbol, expiry, long_call_strike,  Right.CALL, Side.BUY,  qty),
        ]

        earnings_event = self._earnings.next_earnings(chain.symbol)
        log.info(
            "Earnings crush signal: %s %s | implied_move=%.1f%% hist=%.1f%% | credit=%.2f",
            chain.symbol, expiry, implied_move * 100, hist_move * 100, credit,
        )
        return [SignalResult(
            strategy=self.name,
            underlying=chain.symbol,
            legs=legs,
            entry_credit=credit,
            max_loss=max_loss,
            rationale=(
                f"Earnings {earnings_event.earnings_date if earnings_event else '?'}: "
                f"implied_move={implied_move*100:.1f}% vs hist={hist_move*100:.1f}% "
                f"({implied_move/hist_move:.2f}× premium)"
            ),
            entry_date=date.today(),
            target_exit_dte=0,
            profit_target_pct=1.0,   # hold to expiry / morning exit
        )]

    # ── exit rules ───────────────────────────────────────────────────────────

    def should_exit(self, position: Position, chain: OptionChain) -> Optional[ExitSignal]:
        if position.strategy != self.name:
            return None

        # Exit the morning after earnings (DTE = 0 or 1)
        if position.dte <= 1:
            return ExitSignal(position.id, ExitReason.DTE_EXPIRY,
                              f"Post-earnings exit at DTE {position.dte}")

        # Stop if losing more than 3× credit (rare but protect against gap)
        pnl = position.unrealized_pnl
        if pnl <= -position.entry_credit * 3.0:
            return ExitSignal(position.id, ExitReason.STOP_LOSS,
                              f"PnL {pnl:.0f} breached 3× credit stop")

        return None


def _find_strike_step(chain: OptionChain, expiry) -> float:
    strikes = sorted({c.strike for c in chain.by_expiry(expiry)})
    if len(strikes) < 2:
        return 1.0
    diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return round(min(diffs), 2)


def _find_by_strike(chain, strike, right, expiry):
    candidates = [c for c in chain.by_expiry(expiry)
                  if c.right == right and abs(c.strike - strike) < 0.5]
    return min(candidates, key=lambda c: abs(c.strike - strike)) if candidates else None
