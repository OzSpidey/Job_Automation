"""
The Wheel strategy.

Phase 1 (CSP): Sell a 30-delta cash-secured put at 30-45 DTE.
  - Take profit at 50% or roll at 21 DTE if still OTM.
  - If assigned, enter Phase 2.

Phase 2 (Covered Call): Sell a 30-delta covered call on the assigned shares.
  - Take profit at 50% or roll at 21 DTE if still OTM.
  - If called away, return to Phase 1.

Universe: underlyings you are genuinely comfortable owning long-term.
Strike selection: always by delta (30d), never by dollar amount.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .base import ExitReason, ExitSignal, Strategy
from ..models import OptionChain, OptionLeg, Position, Right, Side, SignalResult
from ..surface.interpolator import VolSurface

log = logging.getLogger(__name__)

_STRAT = "wheel"


class Wheel(Strategy):
    name = _STRAT

    def __init__(
        self,
        underlyings: list[str] = None,
        csp_delta: float = 0.30,
        csp_dte_min: int = 25,
        csp_dte_max: int = 40,
        cc_delta: float = 0.30,
        profit_target_pct: float = 0.50,
        roll_dte: int = 21,
    ):
        self.underlyings = underlyings or ["AAPL", "MSFT", "NVDA", "AMZN", "SPY", "QQQ"]
        self.csp_delta = csp_delta
        self.csp_dte_min = csp_dte_min
        self.csp_dte_max = csp_dte_max
        self.cc_delta = cc_delta
        self.profit_target_pct = profit_target_pct
        self.roll_dte = roll_dte

        # Tracks assigned shares per underlying: symbol → (shares, cost_basis)
        self._assigned: dict[str, tuple[int, float]] = {}

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

        sym = chain.symbol
        signals = []

        has_csp = any(p.underlying == sym and p.strategy == self.name
                      and _is_csp(p) and p.status.value == "OPEN"
                      for p in open_positions)
        has_cc  = any(p.underlying == sym and p.strategy == self.name
                      and _is_cc(p)  and p.status.value == "OPEN"
                      for p in open_positions)

        # If assigned shares and no CC active → sell covered call
        if sym in self._assigned and not has_cc:
            cc_sig = self._cc_signal(chain, nav)
            if cc_sig:
                signals.append(cc_sig)

        # If no CSP active and no assignment pending → sell CSP
        elif not has_csp and sym not in self._assigned:
            csp_sig = self._csp_signal(chain, nav)
            if csp_sig:
                signals.append(csp_sig)

        return signals

    # ── exit rules ───────────────────────────────────────────────────────────

    def should_exit(self, position: Position, chain: OptionChain) -> Optional[ExitSignal]:
        if position.strategy != self.name:
            return None

        pnl    = position.unrealized_pnl
        credit = position.entry_credit

        if pnl >= credit * self.profit_target_pct:
            return ExitSignal(position.id, ExitReason.PROFIT_TARGET,
                              f"PnL {pnl:.0f} ≥ {credit * self.profit_target_pct:.0f} target")

        if position.dte <= self.roll_dte:
            # OTM at 21 DTE → roll (re-enter, system will generate new signal next cycle)
            return ExitSignal(position.id, ExitReason.ROLL,
                              f"DTE {position.dte} ≤ {self.roll_dte}, rolling")

        return None

    def record_assignment(self, symbol: str, shares: int, cost_basis: float) -> None:
        """Called by the paper/live trader when a CSP is assigned."""
        self._assigned[symbol] = (shares, cost_basis)
        log.info("Wheel: %s assigned %d shares @ %.2f", symbol, shares, cost_basis)

    def record_called_away(self, symbol: str) -> None:
        """Called when the covered call is exercised against us."""
        self._assigned.pop(symbol, None)
        log.info("Wheel: %s shares called away, returning to CSP phase", symbol)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _csp_signal(self, chain: OptionChain, nav: float) -> Optional[SignalResult]:
        expiry = chain.nearest_expiry_by_dte(self.csp_dte_min, self.csp_dte_max)
        if expiry is None:
            return None

        put = chain.near_delta(self.csp_delta, Right.PUT, expiry)
        if not put:
            return None

        # Capital check: CSP requires cash = strike × 100
        required_cash = put.strike * 100
        if required_cash > nav * 0.25:  # no more than 25% NAV per CSP
            log.debug("Wheel CSP: %s strike %.0f too large for %.0f NAV", chain.symbol, put.strike, nav)
            return None

        qty = 1
        credit = put.mid * qty * 100
        max_loss = put.strike * qty * 100 - credit  # assigned at strike, keep premium

        log.info("Wheel CSP signal: SELL %s %s P%.0f credit=%.2f",
                 chain.symbol, expiry, put.strike, credit)
        return SignalResult(
            strategy=self.name,
            underlying=chain.symbol,
            legs=[OptionLeg(chain.symbol, expiry, put.strike, Right.PUT, Side.SELL, qty)],
            entry_credit=credit,
            max_loss=max_loss,
            rationale=f"CSP: 30d {self.csp_delta:.0%}-delta put, {self.csp_dte_min}-{self.csp_dte_max} DTE",
            entry_date=date.today(),
            target_exit_dte=self.roll_dte,
            profit_target_pct=self.profit_target_pct,
        )

    def _cc_signal(self, chain: OptionChain, nav: float) -> Optional[SignalResult]:
        expiry = chain.nearest_expiry_by_dte(self.csp_dte_min, self.csp_dte_max)
        if expiry is None:
            return None

        shares, cost_basis = self._assigned.get(chain.symbol, (0, 0))
        if shares < 100:
            return None

        call = chain.near_delta(self.cc_delta, Right.CALL, expiry)
        if not call:
            return None

        qty = shares // 100
        credit = call.mid * qty * 100
        # If called away: sell shares at strike (may be above or below cost basis)
        max_loss = max(0.0, (cost_basis - call.strike) * qty * 100)

        log.info("Wheel CC signal: SELL %s %s C%.0f credit=%.2f (shares=%d cost_basis=%.2f)",
                 chain.symbol, expiry, call.strike, credit, shares, cost_basis)
        return SignalResult(
            strategy=self.name,
            underlying=chain.symbol,
            legs=[OptionLeg(chain.symbol, expiry, call.strike, Right.CALL, Side.SELL, qty)],
            entry_credit=credit,
            max_loss=max_loss,
            rationale=(
                f"CC: 30d {self.cc_delta:.0%}-delta call on {shares} assigned shares "
                f"(cost basis ${cost_basis:.2f})"
            ),
            entry_date=date.today(),
            target_exit_dte=self.roll_dte,
            profit_target_pct=self.profit_target_pct,
        )


def _is_csp(p: Position) -> bool:
    return len(p.legs) == 1 and p.legs[0].right.value == "P" and p.legs[0].side.value == "SELL"


def _is_cc(p: Position) -> bool:
    return len(p.legs) == 1 and p.legs[0].right.value == "C" and p.legs[0].side.value == "SELL"
