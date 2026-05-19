"""
Earnings calendar.

Sources (in priority order):
  1. yfinance Ticker.calendar (most reliable for near-term dates)
  2. Fallback: cached results from a prior fetch

For the earnings vol crush strategy, we need:
  - Earnings date (confirmed or estimate)
  - Whether the date is confirmed by the company
  - Historical implied vs realised move data (rough approximation)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

log = logging.getLogger(__name__)


@dataclass
class EarningsEvent:
    symbol: str
    earnings_date: date
    confirmed: bool = False
    time_of_day: str = "after_close"   # before_open | after_close | unknown

    @property
    def exit_date(self) -> date:
        """Morning after earnings."""
        return self.earnings_date + timedelta(days=1)


class EarningsCalendar:

    def __init__(self):
        self._cache: dict[str, Optional[EarningsEvent]] = {}

    def next_earnings(self, symbol: str) -> Optional[EarningsEvent]:
        if symbol in self._cache:
            cached = self._cache[symbol]
            if cached and cached.earnings_date >= date.today():
                return cached

        try:
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            if cal is None or cal.empty:
                self._cache[symbol] = None
                return None

            # yfinance returns a DataFrame with dates as columns
            if hasattr(cal, "columns"):
                dates_raw = cal.loc["Earnings Date"] if "Earnings Date" in cal.index else None
                if dates_raw is not None:
                    earnings_dt = _extract_date(dates_raw)
                    if earnings_dt:
                        event = EarningsEvent(symbol=symbol, earnings_date=earnings_dt)
                        self._cache[symbol] = event
                        return event

            self._cache[symbol] = None
            return None
        except Exception as exc:
            log.debug("Earnings fetch failed for %s: %s", symbol, exc)
            self._cache[symbol] = None
            return None

    def days_to_earnings(self, symbol: str) -> Optional[int]:
        event = self.next_earnings(symbol)
        if not event:
            return None
        return (event.earnings_date - date.today()).days

    def has_earnings_within(self, symbol: str, days: int) -> bool:
        dte = self.days_to_earnings(symbol)
        if dte is None:
            return False
        return 0 <= dte <= days

    def symbols_with_earnings_on(self, symbols: list[str], on_date: date) -> list[str]:
        result = []
        for sym in symbols:
            event = self.next_earnings(sym)
            if event and event.earnings_date == on_date:
                result.append(sym)
        return result

    def historical_move(self, symbol: str, lookback_quarters: int = 8) -> float:
        """
        Average absolute 1-day move around earnings as a fraction of pre-earnings price.
        Used to compare against the implied move to assess edge.
        """
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="3y")
            if hist.empty:
                return 0.05  # 5% default
            log_ret = (hist["Close"] / hist["Close"].shift(1) - 1).dropna()
            # Take top N absolute moves as proxy for earnings moves
            top_moves = log_ret.abs().nlargest(lookback_quarters)
            return float(top_moves.mean())
        except Exception:
            return 0.05


def _extract_date(val) -> Optional[date]:
    """Extract a date from various yfinance calendar formats."""
    import pandas as pd
    try:
        if isinstance(val, pd.Series):
            val = val.iloc[0]
        if hasattr(val, "date"):
            return val.date()
        if isinstance(val, str):
            return date.fromisoformat(val[:10])
        if isinstance(val, date):
            return val
    except Exception:
        pass
    return None
