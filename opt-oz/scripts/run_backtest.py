"""
Run a backtest against historical chain data in the ChainStore.

Requires ThetaData historical chains to be loaded first.
With only yfinance (no history) this can only smoke-test strategy logic.

Usage:
  docker compose --profile backtest run backtest
  or:
  python scripts/run_backtest.py --start 2023-01-01 --end 2024-01-01
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import click
import yaml

from optoz.backtest.engine import Backtester, BacktestConfig
from optoz.data.chain_store import ChainStore
from optoz.strategies.vrp_straddle import VRPStraddle
from optoz.strategies.iron_condor import IronCondor
from optoz.strategies.earnings_crush import EarningsCrush
from optoz.strategies.wheel import Wheel

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")


@click.command()
@click.option("--start", default="2023-01-01", help="Start date YYYY-MM-DD")
@click.option("--end",   default=str(date.today()), help="End date YYYY-MM-DD")
@click.option("--nav",   default=25000.0, help="Starting NAV")
@click.option("--strategies", "-s",
              multiple=True,
              default=["vrp_straddle", "iron_condor", "earnings_crush", "wheel"],
              help="Strategies to backtest")
@click.option("--output", "-o", default="backtest_results.json", help="Output JSON path")
def main(start, end, nav, strategies, output):
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    uni_path = os.path.join(os.path.dirname(__file__), "..", "config", "universe.yaml")
    with open(uni_path) as f:
        uni = yaml.safe_load(f)

    underlyings = (
        [u["symbol"] for u in uni.get("etfs", [])]
        + [u["symbol"] for u in uni.get("single_names", [])]
    )

    sc = cfg["strategies"]
    strat_map = {
        "vrp_straddle":   lambda: VRPStraddle(**{k: v for k, v in sc["vrp_straddle"].items() if k != "enabled"}),
        "iron_condor":    lambda: IronCondor(**{k: v for k, v in sc["iron_condor"].items() if k != "enabled"}),
        "earnings_crush": lambda: EarningsCrush(**{k: v for k, v in sc["earnings_crush"].items() if k != "enabled"}),
        "wheel":          lambda: Wheel(**{k: v for k, v in sc["wheel"].items() if k != "enabled"}),
    }

    active_strategies = [strat_map[s]() for s in strategies if s in strat_map]
    if not active_strategies:
        click.echo("No valid strategies specified.")
        sys.exit(1)

    store = ChainStore(os.getenv("DATA_DIR", "/data"))

    backtest_cfg = BacktestConfig(
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        starting_nav=nav,
        risk_free_rate=cfg["greeks"]["risk_free_rate"],
        commission_per_contract=cfg["execution"]["commission_per_contract"],
        exchange_fee_per_contract=cfg["execution"]["exchange_fee_per_contract"],
    )

    engine = Backtester(
        strategies=active_strategies,
        chain_store=store,
        config=backtest_cfg,
    )

    click.echo(f"Running backtest: {start} → {end} | NAV={nav} | strategies={list(strategies)}")
    result = engine.run(underlyings)

    # Print metrics
    click.echo("\n── Backtest Results ──────────────────────────────────")
    for k, v in result.metrics.items():
        click.echo(f"  {k:<30} {v}")

    # Save to JSON
    out = {
        "config": {
            "start": start, "end": end, "nav": nav,
            "strategies": list(strategies),
        },
        "metrics": result.metrics,
        "daily": [
            {
                "date": str(r.date),
                "nav": r.nav,
                "daily_pnl": r.daily_pnl,
                "cumulative_pnl": r.cumulative_pnl,
                "open_positions": r.open_positions,
            }
            for r in result.daily_records
        ],
    }
    with open(output, "w") as f:
        json.dump(out, f, indent=2)
    click.echo(f"\nResults saved to {output}")


if __name__ == "__main__":
    main()
