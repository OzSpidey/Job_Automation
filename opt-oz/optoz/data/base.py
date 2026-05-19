"""Abstract data provider interface. All providers must implement this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from ..models import OptionChain


class DataProvider(ABC):

    @abstractmethod
    def get_chain(self, symbol: str, snapshot_date: Optional[date] = None) -> OptionChain:
        """
        Return a full option chain snapshot.
        snapshot_date=None means live/current data.
        """

    @abstractmethod
    def get_underlying_price(self, symbol: str) -> float:
        """Return the current (or last close) price of the underlying."""

    @abstractmethod
    def get_historical_iv(self, symbol: str, lookback_days: int = 252) -> list[tuple[date, float]]:
        """
        Return list of (date, atm_iv) for IV rank calculation.
        atm_iv is the 30d ATM implied vol for each trading day.
        """

    def get_realized_vol(self, symbol: str, window: int = 30) -> float:
        """
        30-day realized vol from close returns. Providers may override
        with a faster source; default uses yfinance.
        """
        import yfinance as yf
        import numpy as np
        import pandas as pd

        hist = yf.Ticker(symbol).history(period="90d")
        if hist.empty or len(hist) < window + 1:
            return 0.20  # fallback
        log_ret = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
        return float(log_ret.tail(window).std() * np.sqrt(252))
