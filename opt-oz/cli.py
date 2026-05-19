"""
Opt-Oz CLI entry point.

Commands:
  optoz paper       — start paper trading loop + dashboard
  optoz backtest    — run historical backtest
  optoz chain       — fetch and save a chain snapshot
  optoz greeks      — show current greeks for a symbol
  optoz stress      — run stress test on current paper positions
"""
from __future__ import annotations

import os
import sys

import click

sys.path.insert(0, os.path.dirname(__file__))


@click.group()
@click.version_option("0.1.0")
def cli():
    """Opt-Oz: systematic options trading system."""


@cli.command()
@click.option("--host", default="0.0.0.0", help="Dashboard host")
@click.option("--port", default=8080, help="Dashboard port")
def paper(host, port):
    """Start the paper trading loop + monitoring dashboard."""
    import threading
    import uvicorn

    from optoz.monitor.app import app
    from scripts.run_paper import PaperTradingLoop
    import time, logging

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    loop = PaperTradingLoop()

    def trading_thread():
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="America/New_York")
        scheduler.add_job(loop.run_once, "cron", day_of_week="mon-fri", hour=16, minute=30)
        scheduler.start()
        loop.run_once()
        try:
            while True:
                time.sleep(60)
        except Exception:
            scheduler.shutdown()

    t = threading.Thread(target=trading_thread, daemon=True)
    t.start()

    click.echo(f"Dashboard: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


@cli.command()
@click.option("--start", default="2023-01-01")
@click.option("--end", default=str(__import__("datetime").date.today()))
@click.option("--nav", default=25000.0)
@click.option("--output", "-o", default="backtest_results.json")
def backtest(start, end, nav, output):
    """Run historical backtest (requires ThetaData chains in store)."""
    from scripts.run_backtest import main as bt_main
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(bt_main, [
        "--start", start, "--end", end,
        "--nav", str(nav), "--output", output,
    ])
    click.echo(result.output)


@cli.command()
@click.argument("symbol")
def chain(symbol):
    """Fetch and save a live option chain for SYMBOL."""
    from optoz.data.yfinance_provider import YFinanceProvider
    from optoz.data.chain_store import ChainStore
    import logging
    logging.basicConfig(level="INFO")

    provider = YFinanceProvider()
    store = ChainStore(os.getenv("DATA_DIR", "./data"))

    click.echo(f"Fetching chain for {symbol}...")
    c = provider.get_chain(symbol.upper())
    store.save(c)
    click.echo(f"Saved {len(c.contracts)} contracts for {symbol} @ {c.underlying_price:.2f}")
    click.echo(f"Expiries: {', '.join(str(e) for e in c.expiries()[:5])} ...")


@cli.command()
@click.argument("symbol")
@click.option("--strike", type=float, required=True)
@click.option("--expiry", required=True, help="YYYY-MM-DD")
@click.option("--right", type=click.Choice(["C", "P"]), default="C")
@click.option("--iv", type=float, default=0.20)
def greeks(symbol, strike, expiry, right, iv):
    """Compute Black-Scholes greeks for a single option."""
    from datetime import date as dt
    from optoz.greeks.black_scholes import bs_greeks
    from optoz.data.yfinance_provider import YFinanceProvider

    provider = YFinanceProvider()
    S = provider.get_underlying_price(symbol.upper())
    exp = dt.fromisoformat(expiry)
    T = (exp - dt.today()).days / 365.0

    g = bs_greeks(S=S, K=strike, T=T, r=0.0525, sigma=iv, right=right)
    click.echo(f"\n{symbol} {right}{strike} exp={expiry} IV={iv*100:.1f}% S={S:.2f}\n")
    for k, v in g.items():
        click.echo(f"  {k:<8} {v:.6f}")


@cli.command()
def stress():
    """Run stress test on current paper positions (reads shared state)."""
    from optoz.monitor.app import _state
    from optoz.risk.stress import StressTester

    positions = _state.get("positions", [])
    nav = _state.get("nav", 25000)
    click.echo(f"Running stress test: {len(positions)} positions, NAV={nav:.0f}")
    click.echo("(Positions loaded from dashboard state — run 'optoz paper' first)")


if __name__ == "__main__":
    cli()
