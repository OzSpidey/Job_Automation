"""
Backtest performance metrics.

Implements:
  - Sharpe ratio (annualised)
  - Deflated Sharpe ratio (Bailey & López de Prado, 2014)
  - Sortino ratio
  - Max drawdown + recovery
  - Win rate, average win/loss
  - Calmar ratio
  - Regime analysis (low vol, normal, high vol periods)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .engine import BacktestConfig, DayRecord


class BacktestMetrics:

    @staticmethod
    def compute(records: list[DayRecord], config: BacktestConfig) -> dict:
        if not records:
            return {}

        returns = np.array([r.daily_pnl / max(r.nav - r.daily_pnl, 1)
                            for r in records])
        navs = np.array([r.nav for r in records])

        sharpe = BacktestMetrics.sharpe(returns)
        sortino = BacktestMetrics.sortino(returns)
        max_dd, max_dd_pct = BacktestMetrics.max_drawdown(navs)
        calmar = BacktestMetrics.calmar(returns, navs)
        deflated_sr = BacktestMetrics.deflated_sharpe(returns)

        total_pnl = navs[-1] - config.starting_nav
        total_return_pct = total_pnl / config.starting_nav * 100
        n_days = len(records)
        ann_return = (1 + total_pnl / config.starting_nav) ** (252 / max(n_days, 1)) - 1

        return {
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "annualized_return_pct": round(ann_return * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "deflated_sharpe": round(deflated_sr, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": round(calmar, 3),
            "max_drawdown_dollars": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct * 100, 2),
            "win_days": int(np.sum(returns > 0)),
            "loss_days": int(np.sum(returns < 0)),
            "win_rate_pct": round(np.sum(returns > 0) / max(len(returns), 1) * 100, 1),
            "avg_daily_return_pct": round(np.mean(returns) * 100, 3),
            "volatility_pct": round(np.std(returns) * np.sqrt(252) * 100, 2),
            "n_trading_days": n_days,
        }

    @staticmethod
    def sharpe(returns: np.ndarray, rf_daily: float = 0.0525/252) -> float:
        excess = returns - rf_daily
        if np.std(excess) < 1e-10:
            return 0.0
        return float(np.mean(excess) / np.std(excess) * np.sqrt(252))

    @staticmethod
    def sortino(returns: np.ndarray, rf_daily: float = 0.0525/252) -> float:
        excess = returns - rf_daily
        downside = excess[excess < 0]
        if len(downside) < 2 or np.std(downside) < 1e-10:
            return 0.0
        return float(np.mean(excess) / np.std(downside) * np.sqrt(252))

    @staticmethod
    def max_drawdown(navs: np.ndarray) -> tuple[float, float]:
        peak = np.maximum.accumulate(navs)
        dd = peak - navs
        dd_pct = dd / np.maximum(peak, 1)
        max_dd = float(np.max(dd))
        max_dd_pct = float(np.max(dd_pct))
        return max_dd, max_dd_pct

    @staticmethod
    def calmar(returns: np.ndarray, navs: np.ndarray) -> float:
        ann_return = np.mean(returns) * 252
        _, max_dd_pct = BacktestMetrics.max_drawdown(navs)
        if max_dd_pct < 1e-10:
            return 0.0
        return float(ann_return / max_dd_pct)

    @staticmethod
    def deflated_sharpe(
        returns: np.ndarray,
        n_trials: int = 1,
        sr_benchmark: float = 0.0,
    ) -> float:
        """
        Deflated Sharpe Ratio (López de Prado, 2014).

        Adjusts Sharpe for multiple testing, skewness, and kurtosis.
        A DSR < 0 means the observed Sharpe is likely due to luck at this
        number of trials.
        """
        T = len(returns)
        if T < 5:
            return 0.0

        sr = BacktestMetrics.sharpe(returns)
        skew = float(pd.Series(returns).skew())
        kurt = float(pd.Series(returns).kurt())  # excess kurtosis

        # Expected maximum Sharpe under multiple testing
        gamma = 0.5772156649  # Euler-Mascheroni constant
        sr_max = ((1 - gamma) * math.sqrt(2 * math.log(n_trials))
                  + gamma / math.sqrt(2 * math.log(max(n_trials, 2))))

        # Variance of SR estimate
        var_sr = (1 + 0.5 * sr**2 - skew * sr + (kurt / 4) * sr**2) / max(T - 1, 1)

        from scipy.stats import norm
        z = (sr - sr_max) / math.sqrt(max(var_sr, 1e-10))
        return float(norm.cdf(z))

    @staticmethod
    def regime_analysis(records: list[DayRecord]) -> dict:
        """Bucket performance by VIX proxy (using daily return volatility)."""
        returns = np.array([r.daily_pnl / max(r.nav - r.daily_pnl, 1) for r in records])

        rolling_vol = pd.Series(returns).rolling(21).std() * np.sqrt(252)
        low_vol   = rolling_vol < 0.15
        high_vol  = rolling_vol > 0.25
        normal    = ~low_vol & ~high_vol

        def stats(mask):
            r = returns[mask.values]
            if len(r) == 0:
                return {"n": 0, "mean_pct": 0, "sharpe": 0}
            return {
                "n": len(r),
                "mean_daily_return_pct": round(np.mean(r) * 100, 3),
                "sharpe": round(BacktestMetrics.sharpe(r), 2),
            }

        return {
            "low_vol_regime":    stats(low_vol),
            "normal_regime":     stats(normal),
            "high_vol_regime":   stats(high_vol),
        }
