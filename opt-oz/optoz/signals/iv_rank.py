"""
IV Rank and IV Percentile computation.

IV Rank  = (current_iv - 52w_low) / (52w_high - 52w_low)  × 100
IV Pctile = % of days in lookback where IV was below current IV

Both range 0–100. IV Rank is the most common retail metric (tastytrade uses it).
IV Percentile is statistically better but harder to intuit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..data.base import DataProvider

log = logging.getLogger(__name__)


@dataclass
class IVRankResult:
    symbol: str
    current_iv: float
    iv_rank: float          # 0–100
    iv_percentile: float    # 0–100
    iv_52w_high: float
    iv_52w_low: float
    lookback_days: int
    as_of: date


class IVRankCalculator:

    def __init__(self, provider: DataProvider, lookback_days: int = 252):
        self.provider = provider
        self.lookback_days = lookback_days
        self._cache: dict[str, IVRankResult] = {}

    def compute(self, symbol: str, current_iv: Optional[float] = None) -> IVRankResult:
        history = self.provider.get_historical_iv(symbol, self.lookback_days)

        if not history:
            log.warning("No IV history for %s, using defaults", symbol)
            iv = current_iv or 0.20
            return IVRankResult(
                symbol=symbol, current_iv=iv, iv_rank=50.0,
                iv_percentile=50.0, iv_52w_high=iv * 1.5,
                iv_52w_low=iv * 0.5, lookback_days=0, as_of=date.today(),
            )

        iv_vals = [iv for _, iv in history]
        high = max(iv_vals)
        low  = min(iv_vals)
        cur  = current_iv if current_iv is not None else iv_vals[-1]

        iv_rank = ((cur - low) / (high - low) * 100) if high > low else 50.0
        iv_pctile = (sum(1 for v in iv_vals if v < cur) / len(iv_vals)) * 100.0

        result = IVRankResult(
            symbol=symbol,
            current_iv=cur,
            iv_rank=float(np.clip(iv_rank, 0, 100)),
            iv_percentile=float(np.clip(iv_pctile, 0, 100)),
            iv_52w_high=high,
            iv_52w_low=low,
            lookback_days=len(history),
            as_of=date.today(),
        )
        self._cache[symbol] = result
        return result

    def is_high_iv(self, symbol: str, threshold: float = 50.0,
                   current_iv: Optional[float] = None) -> bool:
        r = self.compute(symbol, current_iv)
        return r.iv_rank >= threshold

    def cached(self, symbol: str) -> Optional[IVRankResult]:
        return self._cache.get(symbol)


import numpy as np  # noqa: E402 — placed here to avoid circular at module load
