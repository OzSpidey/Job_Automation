"""
Paper trading loop.

Runs once per trading day (scheduled by APScheduler inside Docker).
Also runs immediately on startup.

Flow:
  1. Fetch live chains for all universe underlyings
  2. Fit SVI surface per underlying
  3. Refresh greeks on open positions
  4. Run risk monitor + stress test
  5. Check exit rules → submit closing orders
  6. Generate signals → portfolio construction → risk pre-trade → submit opens
  7. Push state to dashboard

Schedule: weekdays at 16:30 ET (after close snapshot).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from optoz.data.yfinance_provider import YFinanceProvider
from optoz.data.chain_store import ChainStore
from optoz.execution.paper import PaperBroker
from optoz.greeks.portfolio import PortfolioGreeksEngine
from optoz.models import (
    Order, OptionLeg, Position, PositionLeg, PositionStatus,
    Greeks, PortfolioGreeks, Right, Side,
)
from optoz.monitor.app import update_state
from optoz.portfolio.constructor import PortfolioConstructor, ConstructorConfig
from optoz.portfolio.margin import MarginCalculator
from optoz.risk.checks import RiskChecker, RiskConfig
from optoz.risk.monitor import RiskMonitor
from optoz.strategies.vrp_straddle import VRPStraddle
from optoz.strategies.iron_condor import IronCondor
from optoz.strategies.earnings_crush import EarningsCrush
from optoz.strategies.wheel import Wheel
from optoz.surface.svi import SVIFitter
from optoz.surface.interpolator import VolSurface

import yaml

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def load_config():
    path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
    with open(path) as f:
        return yaml.safe_load(f)

def load_universe():
    path = os.path.join(os.path.dirname(__file__), "..", "config", "universe.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


class PaperTradingLoop:

    def __init__(self):
        cfg = load_config()
        uni = load_universe()

        nav = float(os.getenv("PAPER_NAV", cfg["system"]["paper_nav"]))
        self.broker = PaperBroker(starting_nav=nav)
        self.broker.connect()

        self.provider = YFinanceProvider()
        self.store = ChainStore(os.getenv("DATA_DIR", "/data"))
        self.greeks_engine = PortfolioGreeksEngine()
        self.surface_fitter = SVIFitter()
        self.risk_monitor = RiskMonitor(r=cfg["greeks"]["risk_free_rate"])
        self.portfolio_constructor = PortfolioConstructor()
        self.margin_calc = MarginCalculator()

        sc = cfg["strategies"]
        self.strategies = [
            VRPStraddle(**sc.get("vrp_straddle", {})) if sc["vrp_straddle"]["enabled"] else None,
            IronCondor(**{k: v for k, v in sc.get("iron_condor", {}).items() if k != "enabled"})
                if sc["iron_condor"]["enabled"] else None,
            EarningsCrush(**{k: v for k, v in sc.get("earnings_crush", {}).items() if k != "enabled"})
                if sc["earnings_crush"]["enabled"] else None,
            Wheel(**{k: v for k, v in sc.get("wheel", {}).items() if k != "enabled"})
                if sc["wheel"]["enabled"] else None,
        ]
        self.strategies = [s for s in self.strategies if s]

        # Build universe list
        self.underlyings = (
            [u["symbol"] for u in uni.get("etfs", [])]
            + [u["symbol"] for u in uni.get("single_names", [])]
        )

        self.r = cfg["greeks"]["risk_free_rate"]
        self.div_yields = cfg["greeks"].get("dividend_yields", {})

    def run_once(self):
        today = date.today()
        if today.weekday() >= 5:
            log.info("Weekend — skipping paper run")
            return

        log.info("═" * 60)
        log.info("Paper trading run: %s", today)
        log.info("═" * 60)

        chains = {}
        surfaces = {}

        # 1. Fetch chains
        for sym in self.underlyings:
            try:
                chain = self.provider.get_chain(sym)
                self.store.save(chain)
                chains[sym] = chain
                slices = self.surface_fitter.fit_chain(chain, r=self.r)
                if slices:
                    surfaces[sym] = VolSurface(slices, chain.underlying_price, r=self.r)
            except Exception as exc:
                log.warning("Chain fetch failed for %s: %s", sym, exc)

        open_positions = self.broker.open_positions

        # 2. Refresh greeks
        for pos in open_positions:
            chain = chains.get(pos.underlying)
            if chain:
                q = self.div_yields.get(pos.underlying, self.div_yields.get("default", 0.0))
                self.greeks_engine.refresh_position(pos, chain, r=self.r, q=q)

        # 3. Risk snapshot
        pg = PortfolioGreeks.from_positions(open_positions)
        nav = self.broker.nav
        risk_snap = self.risk_monitor.snapshot(open_positions, pg, nav)

        # 4. Check exits
        for pos in list(open_positions):
            chain = chains.get(pos.underlying)
            if not chain:
                continue
            for strat in self.strategies:
                if strat.manages(pos):
                    exit_sig = strat.should_exit(pos, chain)
                    if exit_sig:
                        log.info("EXIT: %s — %s (%s)", pos.underlying, pos.strategy, exit_sig.reason.value)
                        pnl = pos.unrealized_pnl
                        self.broker.close_position(pos.id, pnl)
                        break

        # 5. Generate signals + execute
        if not risk_snap.blocks_new_trades and not _is_paused():
            new_signals = []
            for sym, chain in chains.items():
                surface = surfaces.get(sym)
                for strat in self.strategies:
                    try:
                        sigs = strat.generate_signals(chain, surface, self.broker.open_positions, nav)
                        new_signals.extend(sigs)
                    except Exception as exc:
                        log.warning("Signal error %s %s: %s", strat.name, sym, exc)

            approved = self.portfolio_constructor.evaluate(
                new_signals, self.broker.open_positions, nav, pg,
                available_margin=max(0, nav * 0.50 - sum(p.max_loss for p in self.broker.open_positions)),
            )

            for app_sig in approved:
                sig = app_sig.signal
                try:
                    pos = _signal_to_position(sig, app_sig.quantity, chains)
                    if pos:
                        order = Order(
                            legs=sig.legs,
                            limit_price=sig.entry_credit,
                            position_id=pos.id,
                        )
                        filled_order = self.broker.submit_order(order)
                        if filled_order.status.value == "FILLED":
                            self.broker.open_position(pos)
                            log.info("OPEN: %s %s credit=%.2f max_loss=%.2f",
                                     sig.underlying, sig.strategy, sig.entry_credit, sig.max_loss)
                except Exception as exc:
                    log.error("Order submission error: %s", exc)

        # 6. Push to dashboard
        update_state(
            positions=self.broker.open_positions,
            portfolio_greeks=PortfolioGreeks.from_positions(self.broker.open_positions),
            nav=self.broker.nav,
            stress=risk_snap.stress,
            recent_trades=[
                {
                    "timestamp": str(t.timestamp),
                    "strategy": t.strategy,
                    "legs": [{"symbol": l.symbol, "right": l.right.value,
                              "strike": l.strike, "side": l.side.value}
                             for l in t.legs],
                    "fill_price": t.fill_price,
                    "commission": t.commission,
                }
                for t in self.broker.trade_history[-20:]
            ],
            risk_violations=risk_snap.violations,
            surfaces=surfaces,
        )

        log.info("Run complete: NAV=%.2f open=%d", self.broker.nav, len(self.broker.open_positions))


def _signal_to_position(sig, qty, chains):
    from optoz.models import Position, PositionLeg, PositionStatus, Greeks
    import uuid

    chain = chains.get(sig.underlying)
    if not chain:
        return None

    legs = []
    for leg_spec in sig.legs:
        contract = next(
            (c for c in chain.by_expiry(leg_spec.expiry)
             if c.right == leg_spec.right and abs(c.strike - leg_spec.strike) < 0.5),
            None,
        )
        ep = contract.mid if contract else 0.0
        legs.append(PositionLeg(
            symbol=leg_spec.symbol,
            expiry=leg_spec.expiry,
            strike=leg_spec.strike,
            right=leg_spec.right,
            side=leg_spec.side,
            quantity=qty,
            entry_price=ep,
            current_price=ep,
            greeks=contract.greeks if contract else Greeks(),
        ))

    return Position(
        strategy=sig.strategy,
        underlying=sig.underlying,
        legs=legs,
        entry_date=date.today(),
        max_loss=sig.max_loss * qty,
        entry_credit=sig.entry_credit * qty,
    )


def _is_paused() -> bool:
    from optoz.monitor.app import _state
    return _state.get("paused", False)


def main():
    from apscheduler.schedulers.background import BackgroundScheduler
    loop = PaperTradingLoop()

    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(loop.run_once, "cron", day_of_week="mon-fri", hour=16, minute=30)
    scheduler.start()

    # Run immediately on startup
    loop.run_once()

    log.info("Paper trading loop running. Dashboard at http://localhost:8080")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()
        log.info("Shutting down paper trading loop")


if __name__ == "__main__":
    main()
