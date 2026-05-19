"""
Systematic Iron Condor strategy.

Entry: IV rank ≥ 50, 30-45 DTE, sell 16-delta put spread + 16-delta call spread.
Management: exit at 50% profit or 21 DTE.
Stop: exit at 2× credit received (200% loss on premium).

This is a defined-risk, defined-profit strategy. Max loss = wing_width × 100 - credit.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .base import ExitReason, ExitSignal, Strategy
from ..models import OptionChain, OptionLeg, Position, Right, Side, SignalResult
from ..surface.interpolator import VolSurface

log = logging.getLogger(__name__)

_STRAT = "iron_condor"


class IronCondor(Strategy):
    name = _STRAT

    def __init__(
        self,
        underlyings: list[str] = None,
        target_dte_min: int = 30,
        target_dte_max: int = 45,
        iv_rank_threshold: float = 50.0,
        short_delta: float = 0.16,
        wing_width: int = 5,          # number of strikes between short and long
        profit_target_pct: float = 0.50,
        stop_loss_mult: float = 2.0,  # exit if loss > 2× credit
        max_dte_exit: int = 21,
    ):
        self.underlyings = underlyings or ["SPY", "QQQ", "IWM"]
        self.target_dte_min = target_dte_min
        self.target_dte_max = target_dte_max
        self.iv_rank_threshold = iv_rank_threshold
        self.short_delta = short_delta
        self.wing_width = wing_width
        self.profit_target_pct = profit_target_pct
        self.stop_loss_mult = stop_loss_mult
        self.max_dte_exit = max_dte_exit

    # ── signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        chain: OptionChain,
        surface: Optional[VolSurface],
        open_positions: list[Position],
        nav: float,
    ) -> list[SignalResult]:
        if chain.symbol not in self.underlyings:
            return []

        already_on = any(p.underlying == chain.symbol and p.strategy == self.name
                         and p.status.value == "OPEN" for p in open_positions)
        if already_on:
            return []

        # IV rank check (use surface ATM IV for calculation if available)
        iv_rank = self._iv_rank(chain, surface)
        if iv_rank < self.iv_rank_threshold:
            log.debug("%s IC skip: IV rank %.0f < %.0f", chain.symbol, iv_rank, self.iv_rank_threshold)
            return []

        expiry = chain.nearest_expiry_by_dte(self.target_dte_min, self.target_dte_max)
        if expiry is None:
            return []

        # Find 16-delta contracts
        short_put  = chain.near_delta(self.short_delta, Right.PUT,  expiry)
        short_call = chain.near_delta(self.short_delta, Right.CALL, expiry)
        if not short_put or not short_call:
            return []

        # Wing strikes: one step further OTM
        strike_step = self._find_strike_step(chain, expiry)
        long_put_strike  = round(short_put.strike  - self.wing_width * strike_step, 2)
        long_call_strike = round(short_call.strike + self.wing_width * strike_step, 2)

        long_put  = self._find_by_strike(chain, long_put_strike,  Right.PUT,  expiry)
        long_call = self._find_by_strike(chain, long_call_strike, Right.CALL, expiry)
        if not long_put or not long_call:
            return []

        qty = 1
        credit = (short_put.mid  + short_call.mid
                  - long_put.mid - long_call.mid) * qty * 100
        if credit <= 0:
            log.debug("%s IC: negative credit (%.2f), skipping", chain.symbol, credit)
            return []

        wing_width_dollars = self.wing_width * strike_step * 100
        max_loss = wing_width_dollars - credit

        legs = [
            OptionLeg(chain.symbol, expiry, short_put.strike,  Right.PUT,  Side.SELL, qty),
            OptionLeg(chain.symbol, expiry, long_put_strike,   Right.PUT,  Side.BUY,  qty),
            OptionLeg(chain.symbol, expiry, short_call.strike, Right.CALL, Side.SELL, qty),
            OptionLeg(chain.symbol, expiry, long_call_strike,  Right.CALL, Side.BUY,  qty),
        ]

        log.info(
            "IC signal: %s %s | put spread %.0f/%.0f | call spread %.0f/%.0f "
            "| credit=%.2f max_loss=%.2f IV rank=%.0f",
            chain.symbol, expiry,
            short_put.strike, long_put_strike,
            short_call.strike, long_call_strike,
            credit, max_loss, iv_rank,
        )
        return [SignalResult(
            strategy=self.name,
            underlying=chain.symbol,
            legs=legs,
            entry_credit=credit,
            max_loss=max_loss,
            rationale=f"IV rank={iv_rank:.0f}≥{self.iv_rank_threshold}, 16d wings, {self.wing_width}-strike width",
            entry_date=date.today(),
            target_exit_dte=self.max_dte_exit,
            profit_target_pct=self.profit_target_pct,
        )]

    # ── exit rules ───────────────────────────────────────────────────────────

    def should_exit(self, position: Position, chain: OptionChain) -> Optional[ExitSignal]:
        if position.strategy != self.name:
            return None

        pnl    = position.unrealized_pnl
        credit = position.entry_credit

        if pnl >= credit * self.profit_target_pct:
            return ExitSignal(position.id, ExitReason.PROFIT_TARGET,
                              f"PnL {pnl:.0f} ≥ {credit * self.profit_target_pct:.0f}")

        if pnl <= -credit * self.stop_loss_mult:
            return ExitSignal(position.id, ExitReason.STOP_LOSS,
                              f"PnL {pnl:.0f} ≤ −{credit * self.stop_loss_mult:.0f} (2× credit)")

        if position.dte <= self.max_dte_exit:
            return ExitSignal(position.id, ExitReason.DTE_EXPIRY,
                              f"DTE {position.dte} ≤ {self.max_dte_exit}")

        return None

    # ── helpers ──────────────────────────────────────────────────────────────

    def _iv_rank(self, chain: OptionChain, surface: Optional[VolSurface]) -> float:
        if surface:
            expiries = list(surface.slices.keys())
            if expiries:
                near = min(expiries, key=lambda e: abs((e - date.today()).days - 30))
                current_iv = surface.atm_iv(near)
                # Rough IV rank from surface ATM vs long-run average (simplified)
                return 60.0 if current_iv > 0.20 else 40.0  # placeholder; real calc needs history
        # Fall back to chain contract IVs
        slice_30 = chain.by_expiry(chain.nearest_expiry_by_dte(25, 45) or date.today())
        if not slice_30:
            return 0.0
        avg_iv = sum(c.iv for c in slice_30 if c.iv > 0) / max(len(slice_30), 1)
        # Without history, use a rough heuristic: avg_iv > 20% = IV rank 50+
        return 60.0 if avg_iv > 0.20 else 30.0

    def _find_strike_step(self, chain: OptionChain, expiry) -> float:
        strikes = sorted({c.strike for c in chain.by_expiry(expiry)})
        if len(strikes) < 2:
            return 1.0
        diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
        return round(min(diffs), 2)

    def _find_by_strike(self, chain, strike, right, expiry):
        candidates = [c for c in chain.by_expiry(expiry)
                      if c.right == right and abs(c.strike - strike) < 0.5]
        return min(candidates, key=lambda c: abs(c.strike - strike)) if candidates else None
