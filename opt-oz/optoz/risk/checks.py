"""
Pre-trade and continuous risk checks.

Every order passes through RiskChecker.pre_trade() before submission.
RiskChecker.continuous() runs on every portfolio refresh (EOD or intraday).

Violations are logged and block execution. The system never silently
overrides a risk check — if you want to trade through a breach, the
config limit must be changed explicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..models import Position, PortfolioGreeks, SignalResult

log = logging.getLogger(__name__)


@dataclass
class RiskViolation:
    check: str
    message: str
    severity: str = "BLOCK"    # BLOCK | WARN


@dataclass
class RiskConfig:
    # Portfolio-level limits
    max_portfolio_vega_pct: float = 0.15   # net vega / NAV
    max_portfolio_delta_pct: float = 0.10  # net delta / NAV
    max_portfolio_theta_floor: float = -0.001  # net theta / NAV per day (too negative = problem)

    # Per-position limits
    max_loss_pct_nav: float = 0.05

    # Margin
    max_margin_utilization: float = 0.50

    # Stress test threshold
    stress_max_loss_pct: float = 0.15

    # Expiry risk
    pin_risk_dte: int = 2
    assignment_extrinsic_threshold: float = 0.10  # cents below which short in-the-money is at risk


class RiskChecker:

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()

    # ── pre-trade ────────────────────────────────────────────────────────────

    def pre_trade(
        self,
        signal: SignalResult,
        portfolio_greeks: PortfolioGreeks,
        nav: float,
        available_margin: float,
        required_margin: float,
    ) -> list[RiskViolation]:
        violations = []

        if signal.max_loss > nav * self.cfg.max_loss_pct_nav:
            violations.append(RiskViolation(
                "max_loss_per_position",
                f"max_loss ${signal.max_loss:.0f} > {self.cfg.max_loss_pct_nav*100:.0f}% NAV "
                f"(${nav * self.cfg.max_loss_pct_nav:.0f})",
            ))

        if required_margin > available_margin:
            violations.append(RiskViolation(
                "margin",
                f"Required margin ${required_margin:.0f} > available ${available_margin:.0f}",
            ))

        if required_margin > nav * self.cfg.max_margin_utilization:
            violations.append(RiskViolation(
                "margin_utilization",
                f"This trade would take margin utilization above {self.cfg.max_margin_utilization*100:.0f}%",
                severity="WARN",
            ))

        for v in violations:
            log.warning("RiskCheck PRE_TRADE [%s] %s — %s: %s",
                        v.severity, signal.strategy, v.check, v.message)
        return violations

    # ── continuous ────────────────────────────────────────────────────────────

    def continuous(
        self,
        positions: list[Position],
        portfolio_greeks: PortfolioGreeks,
        nav: float,
    ) -> list[RiskViolation]:
        violations = []
        today = date.today()

        # Portfolio vega
        vega_pct = abs(portfolio_greeks.vega) / max(nav, 1)
        if vega_pct > self.cfg.max_portfolio_vega_pct:
            violations.append(RiskViolation(
                "portfolio_vega",
                f"Net vega {portfolio_greeks.vega:.0f} = {vega_pct*100:.1f}% NAV "
                f"> limit {self.cfg.max_portfolio_vega_pct*100:.0f}%",
                severity="WARN",
            ))

        # Portfolio delta
        delta_pct = abs(portfolio_greeks.delta) / max(nav, 1) * 100
        if delta_pct > self.cfg.max_portfolio_delta_pct * 100:
            violations.append(RiskViolation(
                "portfolio_delta",
                f"Net delta {portfolio_greeks.delta:.1f} = {delta_pct:.1f}% NAV "
                f"> limit {self.cfg.max_portfolio_delta_pct*100:.0f}%",
                severity="WARN",
            ))

        # Pin risk: short positions within 2 DTE near the money
        for pos in positions:
            if pos.status.value != "OPEN":
                continue
            dte = pos.dte
            if dte <= self.cfg.pin_risk_dte:
                for leg in pos.legs:
                    if leg.side.value == "SELL":
                        intrinsic = abs(leg.greeks.delta) > 0.45
                        if intrinsic:
                            violations.append(RiskViolation(
                                "pin_risk",
                                f"{pos.underlying} {leg.right.value}{leg.strike:.0f} "
                                f"DTE={dte} delta={leg.greeks.delta:.2f} — near-pin risk",
                                severity="WARN",
                            ))

            # Assignment risk: short ITM with very low extrinsic
            for leg in pos.legs:
                if leg.side.value == "SELL" and leg.greeks.price > 0:
                    intrinsic = max(0, leg.current_price - leg.greeks.price)
                    extrinsic = leg.greeks.price - intrinsic
                    if extrinsic < self.cfg.assignment_extrinsic_threshold and abs(leg.greeks.delta) > 0.70:
                        violations.append(RiskViolation(
                            "assignment_risk",
                            f"{pos.underlying} {leg.right.value}{leg.strike:.0f} "
                            f"extrinsic=${extrinsic:.2f} < ${self.cfg.assignment_extrinsic_threshold:.2f} "
                            f"delta={leg.greeks.delta:.2f} — early assignment risk",
                            severity="WARN",
                        ))

        for v in violations:
            log.warning("RiskCheck CONTINUOUS [%s] %s: %s", v.severity, v.check, v.message)

        return violations
