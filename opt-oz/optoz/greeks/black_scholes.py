"""
Analytical Black-Scholes pricing and Greeks.

All greeks are per-unit (one share of the underlying = one unit).
Per-contract values = greek × 100 (standard US multiplier).

Convention:
  delta: ∂V/∂S                             (dimensionless)
  gamma: ∂²V/∂S²                           (per $1 move in S)
  theta: ∂V/∂t (calendar day decay)        ($/day per unit)
  vega:  ∂V/∂σ per 1% change in sigma      ($ per 1% vol)
  rho:   ∂V/∂r per 1bp change in r         ($ per 1bp)
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.stats import norm


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    right: str,
    q: float = 0.0,
) -> float:
    """Black-Scholes option price. T in years."""
    if T <= 1e-10:
        if right == "C":
            return max(0.0, S - K)
        else:
            return max(0.0, K - S)

    if sigma <= 0:
        return max(0.0, S - K) if right == "C" else max(0.0, K - S)

    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if right == "C":
        return (S * np.exp(-q * T) * norm.cdf(d1)
                - K * np.exp(-r * T) * norm.cdf(d2))
    else:
        return (K * np.exp(-r * T) * norm.cdf(-d2)
                - S * np.exp(-q * T) * norm.cdf(-d1))


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    right: str,
    q: float = 0.0,
) -> dict[str, float]:
    """
    Return dict with keys: price, delta, gamma, theta, vega, rho.
    theta is per calendar day. vega is per 1% absolute vol move.
    """
    if T <= 1e-10 or sigma <= 0:
        intrinsic = max(0.0, S - K) if right == "C" else max(0.0, K - S)
        d = 1.0 if (right == "C" and S > K) else (-1.0 if (right == "P" and S < K) else 0.0)
        return {"price": intrinsic, "delta": d, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    pdf_d1 = norm.pdf(d1)
    exp_q_T = np.exp(-q * T)
    exp_r_T = np.exp(-r * T)

    price = bs_price(S, K, T, r, sigma, right, q)

    gamma = exp_q_T * pdf_d1 / (S * sigma * sqrt_T)

    if right == "C":
        delta = exp_q_T * norm.cdf(d1)
        rho = K * T * exp_r_T * norm.cdf(d2) / 100.0
        theta = (
            -(S * sigma * exp_q_T * pdf_d1) / (2 * sqrt_T)
            - r * K * exp_r_T * norm.cdf(d2)
            + q * S * exp_q_T * norm.cdf(d1)
        ) / 365.0
    else:
        delta = -exp_q_T * norm.cdf(-d1)
        rho = -K * T * exp_r_T * norm.cdf(-d2) / 100.0
        theta = (
            -(S * sigma * exp_q_T * pdf_d1) / (2 * sqrt_T)
            + r * K * exp_r_T * norm.cdf(-d2)
            - q * S * exp_q_T * norm.cdf(-d1)
        ) / 365.0

    vega = S * exp_q_T * pdf_d1 * sqrt_T / 100.0

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: str,
    q: float = 0.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> Optional[float]:
    """
    Newton-Raphson implied volatility solver.
    Returns None when the price is outside no-arbitrage bounds or convergence fails.
    """
    if T <= 1e-10 or market_price <= 0:
        return None

    # No-arbitrage bounds check
    intrinsic = max(0.0, S - K) if right == "C" else max(0.0, K - S)
    upper = S if right == "C" else K * np.exp(-r * T)
    if market_price < intrinsic * 0.999 or market_price > upper:
        return None

    # Brenner-Subrahmanyam initial guess
    sigma = np.sqrt(2 * np.pi / T) * market_price / S
    sigma = np.clip(sigma, 0.01, 5.0)

    for _ in range(max_iter):
        g = bs_greeks(S, K, T, r, sigma, right, q)
        price_err = g["price"] - market_price
        vega_raw = g["vega"] * 100.0  # convert back from per-1% to per-unit

        if abs(vega_raw) < 1e-10:
            return None

        step = price_err / vega_raw
        sigma_new = np.clip(sigma - step, 0.001, 10.0)

        if abs(sigma_new - sigma) < tol:
            return float(sigma_new)
        sigma = sigma_new

    return None
