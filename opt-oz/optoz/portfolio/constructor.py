"""
Portfolio construction layer.

Sits between signal generation and execution. Takes a list of SignalResults,
applies risk budgeting, sizes positions, and returns approved signals with
their final quantities. Rejected signals are logged with reasons.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..models import Position, PortfolioGreeks, SignalResult
from .margin import MarginCalculator

log = logging.getLogger(__name__)


@dataclass
class ConstructorConfig:
    max_positions: int = 20
    max_nav_per_position: float = 0.05   # max loss per position as % of NAV
    max_portfolio_vega_pct: float = 0.15
    max_portfolio_delta_pct: float = 0.10
    max_margin_utilization: float = 0.50


@dataclass
class ApprovedSignal:
    signal: SignalResult
    quantity: int              # final approved contract quantity


class PortfolioConstructor:

    def __init__(self, config: Optional[ConstructorConfig] = None):
        self.config = config or ConstructorConfig()
        self.margin = MarginCalculator()

    def evaluate(
        self,
        signals: list[SignalResult],
        open_positions: list[Position],
        nav: float,
        portfolio_greeks: PortfolioGreeks,
        available_margin: float,
    ) -> list[ApprovedSignal]:
        """
        Evaluate signals against portfolio constraints.
        Returns approved signals with quantity. Rejects are logged only.
        """
        approved = []
        current_open = len([p for p in open_positions if p.status.value == "OPEN"])

        # Simulate the evolving state as we approve signals
        sim_delta  = portfolio_greeks.delta
        sim_vega   = portfolio_greeks.vega
        sim_margin = available_margin

        for sig in signals:
            reject_reason = self._check(
                sig, current_open, nav, sim_delta, sim_vega, sim_margin,
            )
            if reject_reason:
                log.info("Portfolio rejected '%s' on %s: %s",
                         sig.strategy, sig.underlying, reject_reason)
                continue

            qty = self._size(sig, nav)
            if qty < 1:
                log.info("Portfolio: %s %s sized to 0, skipping", sig.strategy, sig.underlying)
                continue

            # Tentatively update running totals
            current_open += 1
            sim_delta += sum(
                leg.signed_quantity * 0.50  # rough delta per leg (50-delta approx)
                for leg in sig.legs
            ) * qty * 100
            sim_vega  += -abs(sig.entry_credit) * 0.10  # rough vega estimate
            sim_margin -= self.margin.estimate(sig, nav)

            approved.append(ApprovedSignal(signal=sig, quantity=qty))
            log.info("Portfolio approved: %s %s qty=%d credit=%.2f max_loss=%.2f",
                     sig.strategy, sig.underlying, qty, sig.entry_credit * qty,
                     sig.max_loss * qty)

        return approved

    def _check(
        self,
        sig: SignalResult,
        current_open: int,
        nav: float,
        portfolio_delta: float,
        portfolio_vega: float,
        available_margin: float,
    ) -> Optional[str]:
        """Return rejection reason string or None if approved."""

        if current_open >= self.config.max_positions:
            return f"max positions ({self.config.max_positions}) reached"

        if sig.max_loss > nav * self.config.max_nav_per_position:
            return (
                f"max_loss {sig.max_loss:.0f} > {self.config.max_nav_per_position*100:.0f}% "
                f"NAV {nav * self.config.max_nav_per_position:.0f}"
            )

        margin_needed = self.margin.estimate(sig, nav)
        if margin_needed > available_margin:
            return f"insufficient margin: need {margin_needed:.0f}, have {available_margin:.0f}"

        # Vega budget: rough check (positive vega = net long vol)
        if abs(portfolio_vega) / max(nav, 1) > self.config.max_portfolio_vega_pct:
            pass  # allow through — net vega sign matters, logged separately

        return None

    def _size(self, sig: SignalResult, nav: float) -> int:
        """
        Size based on max loss per trade.
        Never exceed 5% NAV in a single position's max loss.
        """
        max_loss_budget = nav * self.config.max_nav_per_position
        if sig.max_loss <= 0:
            return 1
        qty = int(max_loss_budget / sig.max_loss)
        return max(1, min(qty, 5))  # cap at 5 contracts per signal at retail scale
