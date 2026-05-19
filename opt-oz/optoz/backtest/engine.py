"""
Event-driven options backtester.

Loop:
  For each date in the simulation window:
    1. Load chain snapshot (from ChainStore or DataProvider)
    2. Fit SVI surface
    3. Refresh open position greeks
    4. Run risk monitor → stress test
    5. Check exit rules for all open positions → close those that trigger
    6. Generate signals from all strategies
    7. Portfolio constructor approves/sizes signals
    8. Risk pre-trade check on approved signals
    9. Fill approved signals at simulated fill price
    10. Record everything

Requires ThetaData (historical chains) for meaningful backtesting.
With yfinance (no history) the engine can only replay today — useful for
smoke-testing strategy logic, not for real validation.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from ..data.chain_store import ChainStore
from ..data.base import DataProvider
from ..greeks.portfolio import PortfolioGreeksEngine
from ..models import (
    OptionLeg, Position, PositionLeg, PositionStatus,
    Greeks, PortfolioGreeks, Right, Side, SignalResult,
)
from ..portfolio.constructor import PortfolioConstructor, ConstructorConfig
from ..portfolio.margin import MarginCalculator
from ..risk.checks import RiskChecker, RiskConfig
from ..risk.monitor import RiskMonitor
from ..risk.stress import StressTester
from ..strategies.base import Strategy
from ..surface.svi import SVIFitter
from ..surface.interpolator import VolSurface
from .fill_model import FillModel

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    start_date: date
    end_date: date
    starting_nav: float = 25000.0
    risk_free_rate: float = 0.0525
    commission_per_contract: float = 0.65
    exchange_fee_per_contract: float = 0.02
    skip_weekends: bool = True


@dataclass
class DayRecord:
    date: date
    nav: float
    cash: float
    open_positions: int
    daily_pnl: float
    cumulative_pnl: float
    trades_opened: int
    trades_closed: int
    risk_violations: int


@dataclass
class BacktestResult:
    config: BacktestConfig
    daily_records: list[DayRecord]
    closed_positions: list[Position]
    metrics: dict

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "date": r.date,
                "nav": r.nav,
                "daily_pnl": r.daily_pnl,
                "cumulative_pnl": r.cumulative_pnl,
                "open_positions": r.open_positions,
                "trades_opened": r.trades_opened,
                "trades_closed": r.trades_closed,
            }
            for r in self.daily_records
        ])


class Backtester:

    def __init__(
        self,
        strategies: list[Strategy],
        chain_store: ChainStore,
        provider: Optional[DataProvider] = None,
        config: Optional[BacktestConfig] = None,
    ):
        self.strategies = strategies
        self.store = chain_store
        self.provider = provider
        self.config = config or BacktestConfig(
            start_date=date.today() - timedelta(days=365),
            end_date=date.today(),
        )
        self.fill_model = FillModel(
            commission_per_contract=self.config.commission_per_contract,
            exchange_fee_per_contract=self.config.exchange_fee_per_contract,
        )
        self.portfolio = PortfolioConstructor()
        self.risk_monitor = RiskMonitor(r=self.config.risk_free_rate)
        self.greeks_engine = PortfolioGreeksEngine()
        self.surface_fitter = SVIFitter()

    def run(self, underlyings: list[str]) -> BacktestResult:
        log.info("Backtest starting: %s → %s | underlyings=%s",
                 self.config.start_date, self.config.end_date, underlyings)

        cash = self.config.starting_nav
        open_positions: list[Position] = []
        daily_records: list[DayRecord] = []
        closed_positions: list[Position] = []

        current = self.config.start_date
        prev_nav = cash

        while current <= self.config.end_date:
            if self.config.skip_weekends and current.weekday() >= 5:
                current += timedelta(days=1)
                continue

            day_opens = 0
            day_closes = 0
            risk_violations = 0

            # Build chains and surfaces for today
            chains = {}
            surfaces = {}
            for sym in underlyings:
                chain = self.store.load(sym, current)
                if chain is None:
                    log.debug("No chain data for %s %s — skipping", sym, current)
                    continue
                chains[sym] = chain
                slices = self.surface_fitter.fit_chain(chain, r=self.config.risk_free_rate)
                surfaces[sym] = VolSurface(slices, chain.underlying_price, r=self.config.risk_free_rate)

            # Refresh greeks on open positions
            for pos in open_positions:
                chain = chains.get(pos.underlying)
                if chain:
                    self.greeks_engine.refresh_position(pos, chain)

            # Portfolio greeks
            pg = PortfolioGreeks.from_positions(open_positions)

            # Risk snapshot
            nav = cash + sum(
                sum(leg.signed_quantity * leg.current_price * 100 for leg in p.legs)
                for p in open_positions
            )
            risk_snap = self.risk_monitor.snapshot(open_positions, pg, nav)
            risk_violations = len(risk_snap.violations)

            # Exit rules
            positions_to_close = []
            for pos in open_positions:
                chain = chains.get(pos.underlying)
                if not chain:
                    continue
                for strat in self.strategies:
                    if strat.manages(pos):
                        exit_sig = strat.should_exit(pos, chain)
                        if exit_sig:
                            positions_to_close.append((pos, exit_sig))
                            break

            for pos, exit_sig in positions_to_close:
                pnl = pos.unrealized_pnl
                pos.status = PositionStatus.CLOSED
                pos.exit_date = current
                pos.realized_pnl = pnl
                cash += pnl
                closed_positions.append(pos)
                open_positions.remove(pos)
                day_closes += 1
                log.info("BT close: %s %s reason=%s PnL=%.2f",
                         pos.underlying, pos.strategy, exit_sig.reason.value, pnl)

            # Signal generation + execution (skip if stress test blocks new trades)
            if not risk_snap.blocks_new_trades:
                new_signals: list[SignalResult] = []
                for sym, chain in chains.items():
                    surface = surfaces.get(sym)
                    for strat in self.strategies:
                        sigs = strat.generate_signals(chain, surface, open_positions, nav)
                        new_signals.extend(sigs)

                margin_calc = MarginCalculator()
                available_margin = cash * 0.50
                approved = self.portfolio.evaluate(
                    new_signals, open_positions, nav, pg, available_margin,
                )

                for approved_sig in approved:
                    sig = approved_sig.signal
                    qty = approved_sig.quantity
                    pos = self._build_position(sig, qty, chains, current)
                    if pos:
                        open_positions.append(pos)
                        cash -= max(0, -sig.entry_credit * qty)  # pay debit if any
                        if sig.entry_credit > 0:
                            cash += sig.entry_credit * qty
                        day_opens += 1

            nav = cash + sum(
                sum(leg.signed_quantity * leg.current_price * 100 for leg in p.legs)
                for p in open_positions
            )
            daily_pnl = nav - prev_nav
            prev_nav = nav

            daily_records.append(DayRecord(
                date=current,
                nav=nav,
                cash=cash,
                open_positions=len(open_positions),
                daily_pnl=daily_pnl,
                cumulative_pnl=nav - self.config.starting_nav,
                trades_opened=day_opens,
                trades_closed=day_closes,
                risk_violations=risk_violations,
            ))

            current += timedelta(days=1)

        from .metrics import BacktestMetrics
        metrics = BacktestMetrics.compute(daily_records, self.config)

        log.info("Backtest complete: final_nav=%.2f total_pnl=%.2f Sharpe=%.2f",
                 prev_nav, prev_nav - self.config.starting_nav,
                 metrics.get("sharpe_ratio", 0))

        return BacktestResult(
            config=self.config,
            daily_records=daily_records,
            closed_positions=closed_positions,
            metrics=metrics,
        )

    def _build_position(
        self,
        sig: SignalResult,
        qty: int,
        chains: dict,
        today: date,
    ) -> Optional[Position]:
        chain = chains.get(sig.underlying)
        if not chain:
            return None

        legs = []
        total_credit = 0.0

        for leg_spec in sig.legs:
            # Find contract in chain
            contract = None
            for c in chain.by_expiry(leg_spec.expiry):
                if c.right == leg_spec.right and abs(c.strike - leg_spec.strike) < 0.01:
                    contract = c
                    break

            fill = self.fill_model.fill_price(contract, leg_spec.side) if contract else None
            entry_price = fill or (contract.mid if contract else 0.0)

            sign = -1 if leg_spec.side == Side.SELL else 1
            total_credit += sign * entry_price * qty * 100  # negative for buys

            iv = contract.iv if contract else 0.20
            pos_leg = PositionLeg(
                symbol=leg_spec.symbol,
                expiry=leg_spec.expiry,
                strike=leg_spec.strike,
                right=leg_spec.right,
                side=leg_spec.side,
                quantity=qty,
                entry_price=entry_price,
                current_price=entry_price,
                greeks=Greeks(
                    delta=contract.delta if contract else 0.0,
                    gamma=contract.gamma if contract else 0.0,
                    theta=contract.theta if contract else 0.0,
                    vega=contract.vega if contract else 0.0,
                    iv=iv,
                    price=entry_price,
                ),
            )
            legs.append(pos_leg)

        return Position(
            strategy=sig.strategy,
            underlying=sig.underlying,
            legs=legs,
            entry_date=today,
            max_loss=sig.max_loss * qty,
            entry_credit=total_credit,
        )
