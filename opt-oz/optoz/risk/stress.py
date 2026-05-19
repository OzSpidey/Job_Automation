"""
Black-swan stress testing.

Run daily before generating new signals. If the portfolio fails the stress
test, the system logs a warning and blocks new position opens until the
portfolio is de-risked.

Scenarios modeled:
  1. Base case: SPX/underlying down 10%, IV × 2
  2. Vol spike only: underlying flat, IV × 3
  3. Crash + vol spike: underlying down 15%, IV × 4
  4. Gap up: underlying up 10%, IV halves (for short call positions)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..greeks.black_scholes import bs_greeks
from ..models import Position, PositionLeg

log = logging.getLogger(__name__)


@dataclass
class ScenarioResult:
    name: str
    underlying_chg_pct: float    # e.g. -0.10 for down 10%
    vol_multiplier: float
    portfolio_pnl: float
    pnl_pct_nav: float
    breaches_limit: bool


@dataclass
class StressResult:
    as_of: date
    nav: float
    scenarios: list[ScenarioResult]
    worst_case_pnl: float
    worst_case_pct: float
    blocks_new_trades: bool


class StressTester:

    SCENARIOS = [
        ("down10_vol2x",  -0.10, 2.0),
        ("vol_spike_3x",   0.00, 3.0),
        ("crash15_vol4x", -0.15, 4.0),
        ("gap_up_10",      0.10, 0.5),
    ]

    def __init__(
        self,
        max_loss_pct: float = 0.15,
        r: float = 0.0525,
    ):
        self.max_loss_pct = max_loss_pct
        self.r = r

    def run(self, positions: list[Position], nav: float) -> StressResult:
        open_pos = [p for p in positions if p.status.value == "OPEN"]
        scenario_results = []

        for name, s_chg, vol_mult in self.SCENARIOS:
            pnl = self._scenario_pnl(open_pos, s_chg, vol_mult)
            pnl_pct = pnl / max(nav, 1)
            breach = pnl_pct < -self.max_loss_pct
            scenario_results.append(ScenarioResult(
                name=name,
                underlying_chg_pct=s_chg,
                vol_multiplier=vol_mult,
                portfolio_pnl=pnl,
                pnl_pct_nav=pnl_pct,
                breaches_limit=breach,
            ))

        worst = min(scenario_results, key=lambda s: s.portfolio_pnl)

        if worst.breaches_limit:
            log.warning(
                "STRESS TEST FAILED: worst scenario '%s' PnL=%.0f (%.1f%% NAV) exceeds -%.0f%% limit",
                worst.name, worst.portfolio_pnl, worst.pnl_pct_nav * 100, self.max_loss_pct * 100,
            )
        else:
            log.info(
                "Stress test passed: worst scenario '%s' PnL=%.0f (%.1f%% NAV)",
                worst.name, worst.portfolio_pnl, worst.pnl_pct_nav * 100,
            )

        return StressResult(
            as_of=date.today(),
            nav=nav,
            scenarios=scenario_results,
            worst_case_pnl=worst.portfolio_pnl,
            worst_case_pct=worst.pnl_pct_nav,
            blocks_new_trades=worst.breaches_limit,
        )

    def _scenario_pnl(
        self,
        positions: list[Position],
        s_chg: float,
        vol_mult: float,
    ) -> float:
        total_pnl = 0.0
        today = date.today()

        for pos in positions:
            for leg in pos.legs:
                S_current = leg.greeks.price / max(abs(leg.greeks.delta), 0.01)  # rough
                S_shock = S_current * (1 + s_chg)
                iv_shock = leg.greeks.iv * vol_mult
                iv_shock = max(0.01, min(iv_shock, 10.0))

                dte = max((leg.expiry - today).days, 0)
                T = dte / 365.0

                current_val = leg.current_price
                shocked_val = bs_greeks(
                    S=S_shock, K=leg.strike, T=T,
                    r=self.r, sigma=iv_shock,
                    right=leg.right.value, q=0.0,
                )["price"]

                pnl_per_unit = shocked_val - current_val
                total_pnl += leg.signed_quantity * pnl_per_unit * 100

        return total_pnl
