"""Real-time risk monitor — aggregates all checks into a single daily snapshot."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..models import Position, PortfolioGreeks
from .checks import RiskChecker, RiskConfig, RiskViolation
from .stress import StressTester, StressResult

log = logging.getLogger(__name__)


@dataclass
class RiskSnapshot:
    timestamp: datetime
    nav: float
    portfolio_greeks: PortfolioGreeks
    violations: list[RiskViolation]
    stress: StressResult
    blocks_new_trades: bool

    @property
    def has_block_violations(self) -> bool:
        return any(v.severity == "BLOCK" for v in self.violations)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "WARN")

    def summary(self) -> str:
        status = "BLOCKED" if self.blocks_new_trades else "OK"
        return (
            f"[{status}] NAV={self.nav:.0f} "
            f"Δ={self.portfolio_greeks.delta:.1f} "
            f"Γ={self.portfolio_greeks.gamma:.4f} "
            f"Θ={self.portfolio_greeks.theta:.1f}/day "
            f"V={self.portfolio_greeks.vega:.1f} "
            f"| stress_worst={self.stress.worst_case_pct*100:.1f}% "
            f"| violations={len(self.violations)} "
            f"(blocks={sum(1 for v in self.violations if v.severity=='BLOCK')})"
        )


class RiskMonitor:

    def __init__(
        self,
        risk_config: RiskConfig = None,
        stress_max_loss_pct: float = 0.15,
        r: float = 0.0525,
    ):
        self.checker = RiskChecker(risk_config)
        self.stress  = StressTester(max_loss_pct=stress_max_loss_pct, r=r)

    def snapshot(
        self,
        positions: list[Position],
        portfolio_greeks: PortfolioGreeks,
        nav: float,
    ) -> RiskSnapshot:
        violations = self.checker.continuous(positions, portfolio_greeks, nav)
        stress_result = self.stress.run(positions, nav)

        blocks = (
            any(v.severity == "BLOCK" for v in violations)
            or stress_result.blocks_new_trades
        )

        snap = RiskSnapshot(
            timestamp=datetime.now(),
            nav=nav,
            portfolio_greeks=portfolio_greeks,
            violations=violations,
            stress=stress_result,
            blocks_new_trades=blocks,
        )
        log.info("Risk snapshot: %s", snap.summary())
        return snap
