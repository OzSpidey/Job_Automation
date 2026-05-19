"""Portfolio-level Greeks aggregation and refresh."""
from __future__ import annotations

import logging
from datetime import date

from ..models import Greeks, OptionChain, Position, PortfolioGreeks
from .black_scholes import bs_greeks

log = logging.getLogger(__name__)


class PortfolioGreeksEngine:

    def refresh_position(
        self,
        position: Position,
        chain: OptionChain,
        r: float = 0.0525,
        q: float = 0.0,
    ) -> None:
        """Update current_price and greeks on every leg from live chain data."""
        today = date.today()
        for leg in position.legs:
            dte = max((leg.expiry - today).days, 0)
            T = dte / 365.0

            contract = _find_contract(chain, leg.strike, leg.right, leg.expiry)
            if contract:
                leg.current_price = contract.mid
                iv = contract.iv if contract.iv > 0.001 else 0.20
            else:
                # Fall back to BS at last known IV
                iv = leg.greeks.iv if leg.greeks.iv > 0.001 else 0.20
                leg.current_price = leg.greeks.price

            g = bs_greeks(
                S=chain.underlying_price,
                K=leg.strike,
                T=T,
                r=r,
                sigma=iv,
                right=leg.right.value,
                q=q,
            )
            leg.greeks = Greeks(
                delta=g["delta"],
                gamma=g["gamma"],
                theta=g["theta"],
                vega=g["vega"],
                rho=g["rho"],
                iv=iv,
                price=g["price"],
            )

    def aggregate(self, positions: list[Position]) -> PortfolioGreeks:
        return PortfolioGreeks.from_positions(positions)


def _find_contract(chain: OptionChain, strike: float, right, expiry: date):
    for c in chain.contracts:
        if (c.expiry == expiry
                and c.right == right
                and abs(c.strike - strike) < 0.01):
            return c
    return None
