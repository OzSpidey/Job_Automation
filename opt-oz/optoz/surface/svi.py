"""
SVI (Stochastic Volatility Inspired) vol surface fitting.

Gatheral's raw SVI parameterization per expiry slice:

  w(k) = a + b * (ρ(k - m) + sqrt((k - m)² + σ²))

where:
  w  = total implied variance = IV² × T
  k  = log(K / F) log-moneyness (F = forward price)
  a  = level of variance
  b  = slope (angle of the wings)
  ρ  = rotation (skew), |ρ| < 1
  m  = shift (ATM level in log-moneyness space)
  σ  = smoothness (curvature), σ > 0

No-arbitrage butterfly condition enforced via constraints.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
from scipy.optimize import minimize

from ..models import OptionChain

log = logging.getLogger(__name__)


@dataclass
class SVIParams:
    expiry: date
    T: float          # time to expiry in years
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    fit_error: float = 0.0
    n_points: int = 0

    def total_variance(self, k: float) -> float:
        """w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))"""
        inner = np.sqrt((k - self.m) ** 2 + self.sigma ** 2)
        return self.a + self.b * (self.rho * (k - self.m) + inner)

    def implied_vol(self, k: float) -> float:
        w = self.total_variance(k)
        if w < 0 or self.T <= 0:
            return 0.0
        return float(np.sqrt(max(w, 0.0) / self.T))

    def implied_vol_from_strike(self, K: float, F: float) -> float:
        if F <= 0 or K <= 0:
            return 0.0
        k = np.log(K / F)
        return self.implied_vol(k)


class SVIFitter:

    def __init__(self, min_points: int = 5, max_iv: float = 5.0, min_iv: float = 0.01):
        self.min_points = min_points
        self.max_iv = max_iv
        self.min_iv = min_iv

    def fit_chain(self, chain: OptionChain, r: float = 0.0525) -> dict[date, SVIParams]:
        """Fit one SVI slice per expiry. Returns dict keyed by expiry date."""
        results = {}
        today = chain.snapshot_date
        F_approx = chain.underlying_price  # simplification: ignore carry for now

        for expiry in chain.expiries():
            dte = (expiry - today).days
            if dte <= 0:
                continue
            T = dte / 365.0
            F = F_approx * np.exp(r * T)

            slice_contracts = chain.by_expiry(expiry)
            params = self._fit_slice(slice_contracts, T, F, expiry)
            if params:
                results[expiry] = params

        return results

    def _fit_slice(self, contracts, T: float, F: float, expiry: date) -> Optional[SVIParams]:
        """Fit SVI to a single expiry slice."""
        points = []
        for c in contracts:
            iv = c.iv
            if iv < self.min_iv or iv > self.max_iv:
                continue
            if c.bid <= 0 and c.ask <= 0:
                continue
            if c.open_interest < 10:
                continue
            k = np.log(c.strike / F)
            w_obs = iv ** 2 * T
            points.append((k, w_obs))

        if len(points) < self.min_points:
            return None

        ks = np.array([p[0] for p in points])
        ws = np.array([p[1] for p in points])

        # Initial guess: flat at observed ATM variance
        atm_idx = np.argmin(np.abs(ks))
        w_atm = ws[atm_idx]
        x0 = [w_atm * 0.5, 0.1, -0.3, 0.0, 0.2]

        def objective(x):
            a, b, rho, m, s = x
            inner = np.sqrt((ks - m) ** 2 + s ** 2)
            w_fit = a + b * (rho * (ks - m) + inner)
            residuals = w_fit - ws
            return float(np.sum(residuals ** 2))

        constraints = [
            # butterfly no-arbitrage: a + b*s*sqrt(1-rho^2) >= 0
            {"type": "ineq", "fun": lambda x: x[0] + x[1] * x[4] * np.sqrt(1 - x[2] ** 2)},
        ]
        bounds = [
            (0.0, None),      # a >= 0
            (1e-4, 2.0),      # b > 0
            (-0.999, 0.999),  # |rho| < 1
            (-1.0, 1.0),      # m
            (1e-4, 2.0),      # sigma > 0
        ]

        try:
            result = minimize(
                objective, x0, method="SLSQP",
                bounds=bounds, constraints=constraints,
                options={"ftol": 1e-8, "maxiter": 500},
            )
            if not result.success:
                log.debug("SVI fit non-convergent for %s: %s", expiry, result.message)

            a, b, rho, m, s = result.x
            fit_err = float(np.sqrt(result.fun / len(ws)))

            return SVIParams(
                expiry=expiry, T=T,
                a=a, b=b, rho=rho, m=m, sigma=s,
                fit_error=fit_err,
                n_points=len(points),
            )
        except Exception as exc:
            log.warning("SVI fit exception for %s: %s", expiry, exc)
            return None
