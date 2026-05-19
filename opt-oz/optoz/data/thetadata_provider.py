"""
ThetaData provider — historical EOD option chain snapshots.

Requires THETADATA_API_KEY in environment. Free tier gives limited lookback;
Value plan (~$40/mo) gives full chain history with greeks going back years.

API docs: https://http.thetadata.us/docs
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

import httpx

from ..models import OptionChain, OptionContract, Right
from .base import DataProvider

log = logging.getLogger(__name__)

_BASE = "http://127.0.0.1:25510"   # ThetaData Terminal runs locally


class ThetaDataProvider(DataProvider):
    """
    ThetaData requires their Terminal application to be running locally
    (or inside the container). The Terminal is a small Java process that
    handles auth and proxies requests to their servers.

    Install: https://thetadata.net/terminal
    """

    def __init__(self, base_url: str = _BASE):
        self.base = base_url
        self.client = httpx.Client(timeout=30)

    # ── underlying price ────────────────────────────────────────────────────

    def get_underlying_price(self, symbol: str) -> float:
        try:
            r = self.client.get(f"{self.base}/v2/snapshot/stock/quote",
                                params={"root": symbol})
            r.raise_for_status()
            data = r.json()
            rows = data.get("response", [])
            if rows:
                return float(rows[0].get("ms_of_day2", rows[0].get("ask", 0)))
        except Exception as exc:
            log.warning("ThetaData underlying price failed for %s: %s", symbol, exc)
        # fallback to yfinance
        import yfinance as yf
        info = yf.Ticker(symbol).fast_info
        return float(getattr(info, "last_price", 100.0))

    # ── option chain ────────────────────────────────────────────────────────

    def get_chain(self, symbol: str, snapshot_date: Optional[date] = None) -> OptionChain:
        snap = snapshot_date or date.today()
        snap_str = snap.strftime("%Y%m%d")

        underlying_price = self.get_underlying_price(symbol)

        contracts: list[OptionContract] = []
        for right in [Right.CALL, Right.PUT]:
            try:
                r = self.client.get(
                    f"{self.base}/v2/bulk_snapshot/option/quote",
                    params={
                        "root": symbol,
                        "exp": 0,          # all expirations
                        "right": right.value,
                        "date": snap_str,
                    },
                )
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                log.error("ThetaData chain fetch failed for %s %s: %s", symbol, right, exc)
                continue

            today = date.today()
            for row in data.get("response", []):
                try:
                    exp_int = row.get("expiration", 0)
                    exp_date = _int_to_date(exp_int)
                    strike = float(row.get("strike", 0)) / 1000.0
                    bid = float(row.get("bid", 0))
                    ask = float(row.get("ask", 0))
                    iv = float(row.get("iv", 0))
                    delta = float(row.get("delta", 0))
                    gamma = float(row.get("gamma", 0))
                    theta = float(row.get("theta", 0)) / 365.0
                    vega = float(row.get("vega", 0)) / 100.0
                    volume = int(row.get("volume", 0))
                    oi = int(row.get("open_interest", 0))
                    dte = (exp_date - today).days

                    if bid <= 0 and ask <= 0:
                        continue
                    if dte < 0 or dte > 90:
                        continue

                    contracts.append(OptionContract(
                        symbol=symbol,
                        expiry=exp_date,
                        strike=strike,
                        right=right,
                        bid=bid,
                        ask=ask,
                        last=0.0,
                        volume=volume,
                        open_interest=oi,
                        iv=iv,
                        delta=delta,
                        gamma=gamma,
                        theta=theta,
                        vega=vega,
                        underlying_price=underlying_price,
                    ))
                except Exception as exc:
                    log.debug("Skipping ThetaData row: %s", exc)

        log.info("ThetaData: %s %s — %d contracts", symbol, snap_str, len(contracts))
        return OptionChain(
            symbol=symbol,
            snapshot_date=snap,
            underlying_price=underlying_price,
            contracts=contracts,
        )

    # ── historical ATM IV ───────────────────────────────────────────────────

    def get_historical_iv(self, symbol: str, lookback_days: int = 252) -> list[tuple[date, float]]:
        end = date.today()
        start = end - timedelta(days=lookback_days + 30)

        try:
            r = self.client.get(
                f"{self.base}/v2/hist/option/eod",
                params={
                    "root": symbol,
                    "start_date": start.strftime("%Y%m%d"),
                    "end_date": end.strftime("%Y%m%d"),
                    "ivl": 3600000,   # 1h snapshot
                },
            )
            r.raise_for_status()
            # Parse ATM IV from response — simplified
            rows = r.json().get("response", [])
            result = []
            for row in rows:
                d = _int_to_date(row.get("date", 0))
                iv = float(row.get("iv", 0))
                if iv > 0:
                    result.append((d, iv))
            return sorted(result)[-lookback_days:]
        except Exception as exc:
            log.warning("ThetaData historical IV failed for %s: %s", symbol, exc)
            return []


def _int_to_date(val: int) -> date:
    s = str(val)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
