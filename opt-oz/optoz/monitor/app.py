"""
Opt-Oz monitoring dashboard (FastAPI).

Endpoints:
  GET  /             → dashboard HTML
  GET  /api/status   → system status + risk snapshot
  GET  /api/positions → open positions with greeks + PnL
  GET  /api/greeks    → portfolio greeks summary
  GET  /api/stress    → latest stress test results
  GET  /api/surface/{symbol} → vol surface term structure
  GET  /api/trades    → recent trade history
  POST /api/pause     → pause new signal generation
  POST /api/resume    → resume signal generation
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Shared state (populated by the paper/live trader loop)
_state: dict = {
    "mode": os.getenv("OPTOZ_MODE", "paper"),
    "paused": False,
    "nav": float(os.getenv("PAPER_NAV", 25000)),
    "positions": [],
    "portfolio_greeks": {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "rho": 0},
    "stress": None,
    "recent_trades": [],
    "last_update": None,
    "risk_violations": [],
    "surfaces": {},
}

app = FastAPI(title="Opt-Oz", version="0.1.0", docs_url="/api/docs")

_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = _static / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/status")
async def status():
    return {
        "mode": _state["mode"],
        "paused": _state["paused"],
        "nav": _state["nav"],
        "last_update": _state["last_update"],
        "open_positions": len(_state["positions"]),
        "risk_violations": len(_state["risk_violations"]),
        "blocks_new_trades": any(
            v.get("severity") == "BLOCK"
            for v in _state["risk_violations"]
        ),
    }


@app.get("/api/positions")
async def positions():
    out = []
    for p in _state["positions"]:
        legs_out = []
        for leg in p.get("legs", []):
            legs_out.append({
                "symbol": leg.get("symbol"),
                "expiry": str(leg.get("expiry", "")),
                "strike": leg.get("strike"),
                "right": leg.get("right"),
                "side": leg.get("side"),
                "quantity": leg.get("quantity"),
                "entry_price": leg.get("entry_price"),
                "current_price": leg.get("current_price"),
                "delta": leg.get("delta", 0),
                "theta": leg.get("theta", 0),
                "vega": leg.get("vega", 0),
            })
        out.append({
            "id": p.get("id"),
            "strategy": p.get("strategy"),
            "underlying": p.get("underlying"),
            "entry_date": str(p.get("entry_date", "")),
            "dte": p.get("dte"),
            "entry_credit": p.get("entry_credit"),
            "unrealized_pnl": p.get("unrealized_pnl"),
            "max_loss": p.get("max_loss"),
            "legs": legs_out,
        })
    return out


@app.get("/api/greeks")
async def greeks():
    return _state["portfolio_greeks"]


@app.get("/api/stress")
async def stress():
    s = _state.get("stress")
    if not s:
        return {"message": "No stress test run yet"}
    return s


@app.get("/api/surface/{symbol}")
async def surface(symbol: str):
    surf = _state["surfaces"].get(symbol.upper())
    if not surf:
        return JSONResponse({"error": f"No surface for {symbol}"}, status_code=404)
    return surf


@app.get("/api/trades")
async def trades():
    return _state["recent_trades"][-50:]


@app.post("/api/pause")
async def pause():
    _state["paused"] = True
    return {"paused": True}


@app.post("/api/resume")
async def resume():
    _state["paused"] = False
    return {"paused": False}


def update_state(
    positions,
    portfolio_greeks,
    nav: float,
    stress=None,
    recent_trades=None,
    risk_violations=None,
    surfaces: Optional[dict] = None,
) -> None:
    """Called by the trading loop to push state into the dashboard."""
    from ..models import Position

    _state["nav"] = nav
    _state["last_update"] = datetime.now().isoformat()

    pos_out = []
    for p in positions:
        if isinstance(p, Position):
            pos_out.append({
                "id": p.id,
                "strategy": p.strategy,
                "underlying": p.underlying,
                "entry_date": p.entry_date,
                "dte": p.dte,
                "entry_credit": p.entry_credit,
                "unrealized_pnl": p.unrealized_pnl,
                "max_loss": p.max_loss,
                "legs": [
                    {
                        "symbol": leg.symbol,
                        "expiry": leg.expiry,
                        "strike": leg.strike,
                        "right": leg.right.value,
                        "side": leg.side.value,
                        "quantity": leg.quantity,
                        "entry_price": leg.entry_price,
                        "current_price": leg.current_price,
                        "delta": leg.greeks.delta,
                        "theta": leg.greeks.theta,
                        "vega": leg.greeks.vega,
                    }
                    for leg in p.legs
                ],
            })
    _state["positions"] = pos_out

    if portfolio_greeks:
        from ..models import PortfolioGreeks
        if isinstance(portfolio_greeks, PortfolioGreeks):
            _state["portfolio_greeks"] = {
                "delta": portfolio_greeks.delta,
                "gamma": portfolio_greeks.gamma,
                "theta": portfolio_greeks.theta,
                "vega": portfolio_greeks.vega,
                "rho": portfolio_greeks.rho,
            }

    if stress:
        from ..risk.stress import StressResult
        if isinstance(stress, StressResult):
            _state["stress"] = {
                "worst_case_pnl": stress.worst_case_pnl,
                "worst_case_pct": stress.worst_case_pct,
                "blocks_new_trades": stress.blocks_new_trades,
                "scenarios": [
                    {
                        "name": s.name,
                        "underlying_chg_pct": s.underlying_chg_pct,
                        "vol_multiplier": s.vol_multiplier,
                        "portfolio_pnl": s.portfolio_pnl,
                        "pnl_pct_nav": s.pnl_pct_nav,
                        "breaches_limit": s.breaches_limit,
                    }
                    for s in stress.scenarios
                ],
            }

    if risk_violations:
        _state["risk_violations"] = [
            {"check": v.check, "message": v.message, "severity": v.severity}
            for v in risk_violations
        ]

    if surfaces:
        surf_out = {}
        for sym, surf in surfaces.items():
            ts = surf.term_structure()
            surf_out[sym] = {"term_structure": [{"dte": t, "iv": v} for t, v in ts]}
        _state["surfaces"] = surf_out

    if recent_trades is not None:
        _state["recent_trades"] = recent_trades
