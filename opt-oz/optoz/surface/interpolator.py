"""
VolSurface: interpolated IV surface from SVI slice fits.

Given a dict of SVIParams (one per expiry), provides:
  - IV for any (strike, expiry) combination via log-linear time interpolation
  - ATM IV per expiry (for IV rank computation)
  - Term structure: list of (dte, atm_iv) pairs
  - Skew at a given expiry (25d put IV - 25d call IV)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import numpy as np

from .svi import SVIParams

log = logging.getLogger(__name__)


class VolSurface:

    def __init__(self, slices: dict[date, SVIParams], underlying_price: float, r: float = 0.0525):
        self.slices = slices          # keyed by expiry date
        self.S = underlying_price
        self.r = r
        self._sorted_expiries = sorted(slices.keys())

    def iv(self, K: float, expiry: date) -> float:
        """Smoothed IV for a given strike and expiry."""
        if expiry in self.slices:
            params = self.slices[expiry]
            F = self.S * np.exp(self.r * params.T)
            return params.implied_vol_from_strike(K, F)

        # Interpolate between nearest slices
        before = [e for e in self._sorted_expiries if e < expiry]
        after  = [e for e in self._sorted_expiries if e > expiry]

        if not before and not after:
            return 0.20
        if not before:
            p = self.slices[after[0]]
            F = self.S * np.exp(self.r * p.T)
            return p.implied_vol_from_strike(K, F)
        if not after:
            p = self.slices[before[-1]]
            F = self.S * np.exp(self.r * p.T)
            return p.implied_vol_from_strike(K, F)

        p1 = self.slices[before[-1]]
        p2 = self.slices[after[0]]
        w1 = (after[0] - expiry).days
        w2 = (expiry - before[-1]).days
        total = w1 + w2

        F1 = self.S * np.exp(self.r * p1.T)
        F2 = self.S * np.exp(self.r * p2.T)
        iv1 = p1.implied_vol_from_strike(K, F1)
        iv2 = p2.implied_vol_from_strike(K, F2)

        # Variance-linear interpolation (correct for term structure)
        var1 = iv1 ** 2 * p1.T
        var2 = iv2 ** 2 * p2.T
        T_target = (expiry - date.today()).days / 365.0
        var_interp = (w1 * var1 + w2 * var2) / total
        if T_target <= 0:
            return 0.0
        return float(np.sqrt(var_interp / T_target))

    def atm_iv(self, expiry: date) -> float:
        """ATM IV (K = S) for given expiry."""
        return self.iv(self.S, expiry)

    def term_structure(self) -> list[tuple[int, float]]:
        """List of (dte, atm_iv) pairs for all fitted expiries."""
        today = date.today()
        result = []
        for exp, params in sorted(self.slices.items()):
            dte = (exp - today).days
            atm = self.atm_iv(exp)
            result.append((dte, atm))
        return result

    def skew(self, expiry: date) -> float:
        """25-delta put IV minus 25-delta call IV (positive = normal downside skew)."""
        if expiry not in self.slices:
            return 0.0
        params = self.slices[expiry]
        F = self.S * np.exp(self.r * params.T)

        # Approximate 25-delta strikes (rough BS inversion)
        iv_atm = self.atm_iv(expiry)
        T = params.T
        if T <= 0 or iv_atm <= 0:
            return 0.0

        d1_put25 = -0.674   # N^(-1)(0.25) ≈ -0.674
        d1_call25 = 0.674

        K_put25  = F * np.exp(-d1_put25  * iv_atm * np.sqrt(T) + 0.5 * iv_atm**2 * T)
        K_call25 = F * np.exp(-d1_call25 * iv_atm * np.sqrt(T) + 0.5 * iv_atm**2 * T)

        return self.iv(K_put25, expiry) - self.iv(K_call25, expiry)

    def contango_ratio(self) -> Optional[float]:
        """
        Ratio of back-month ATM IV to front-month ATM IV.
        > 1 = contango (normal), < 1 = backwardation (elevated near-term fear).
        """
        ts = self.term_structure()
        if len(ts) < 2:
            return None
        front_iv = ts[0][1]
        back_iv  = ts[-1][1]
        if front_iv <= 0:
            return None
        return back_iv / front_iv
