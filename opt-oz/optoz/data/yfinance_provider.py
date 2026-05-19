"""
yfinance data provider.

Gives live (15-min delayed) option chains. No meaningful historical chain
data — suitable for paper trading signal generation and live trading.
Greeks are computed from Black-Scholes using the chain's reported IVs.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import numpy as np
import yfinance as yf

from ..models import OptionChain, OptionContract, Right
from .base import DataProvider

log = logging.getLogger(__name__)


class YFinanceProvider(DataProvider):

    def get_underlying_price(self, symbol: str) -> float:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        if not price:
            hist = ticker.history(period="2d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
        return float(price)

    def get_chain(self, symbol: str, snapshot_date: Optional[date] = None) -> OptionChain:
        if snapshot_date and snapshot_date != date.today():
            raise ValueError(
                "YFinanceProvider does not support historical chains. "
                "Use ThetaDataProvider for historical data."
            )

        ticker = yf.Ticker(symbol)
        underlying_price = self.get_underlying_price(symbol)

        expirations = ticker.options
        if not expirations:
            log.warning("No options expirations for %s", symbol)
            return OptionChain(symbol=symbol, snapshot_date=date.today(),
                               underlying_price=underlying_price, contracts=[])

        contracts: list[OptionContract] = []
        today = date.today()

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            # Limit to ≤ 90 DTE for performance
            if dte < 0 or dte > 90:
                continue

            try:
                chain = ticker.option_chain(exp_str)
            except Exception as exc:
                log.debug("Failed to fetch chain for %s %s: %s", symbol, exp_str, exc)
                continue

            for df, right in [(chain.calls, Right.CALL), (chain.puts, Right.PUT)]:
                for _, row in df.iterrows():
                    try:
                        iv = float(row.get("impliedVolatility", 0) or 0)
                        bid = float(row.get("bid", 0) or 0)
                        ask = float(row.get("ask", 0) or 0)
                        strike = float(row["strike"])

                        if bid <= 0 and ask <= 0:
                            continue

                        greeks = _compute_greeks(
                            S=underlying_price,
                            K=strike,
                            T=max(dte, 1) / 365.0,
                            iv=iv if iv > 0 else 0.20,
                            right=right,
                        )

                        contracts.append(OptionContract(
                            symbol=symbol,
                            expiry=exp_date,
                            strike=strike,
                            right=right,
                            bid=bid,
                            ask=ask,
                            last=float(row.get("lastPrice", 0) or 0),
                            volume=int(row.get("volume", 0) or 0),
                            open_interest=int(row.get("openInterest", 0) or 0),
                            iv=iv,
                            delta=greeks["delta"],
                            gamma=greeks["gamma"],
                            theta=greeks["theta"],
                            vega=greeks["vega"],
                            underlying_price=underlying_price,
                        ))
                    except Exception as exc:
                        log.debug("Skipping contract: %s", exc)

        log.info("YFinance: %s — %d contracts loaded", symbol, len(contracts))
        return OptionChain(
            symbol=symbol,
            snapshot_date=today,
            underlying_price=underlying_price,
            contracts=contracts,
        )

    def get_historical_iv(self, symbol: str, lookback_days: int = 252) -> list[tuple[date, float]]:
        """
        Approximate historical ATM IV from VIX proxy or fallback to a
        static 252-day window of the underlying's own HV as a rough IV estimate.
        For proper IV rank, ThetaDataProvider must be used.
        """
        import pandas as pd
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{lookback_days + 30}d")
        if hist.empty:
            return []

        log_ret = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
        rv_series = log_ret.rolling(21).std() * np.sqrt(252)
        iv_series = rv_series * 1.10  # rough IV ≈ RV + 10% VRP premium

        result = []
        for idx, val in iv_series.dropna().items():
            d = idx.date() if hasattr(idx, "date") else idx
            result.append((d, float(val)))
        return result[-lookback_days:]


def _compute_greeks(S: float, K: float, T: float, iv: float, right: Right) -> dict:
    """Fast BS greeks for chain hydration. r and q defaulted to typical values."""
    from ..greeks.black_scholes import bs_greeks
    return bs_greeks(S=S, K=K, T=T, r=0.0525, sigma=iv, right=right.value, q=0.0)
