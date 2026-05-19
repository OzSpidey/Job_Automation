"""
Realistic options fill model for backtesting.

Key assumptions (based on actual retail options execution experience):

1. Fill price: mid ± 30% of half-spread toward the unfavorable side.
   Selling: filled at mid - 0.30 × (ask - mid)  → slightly worse than mid
   Buying:  filled at mid + 0.30 × (ask - mid)

2. Liquidity filter: contracts with spread > 25% of mid are skipped
   (impossible to get a fair fill at retail on super-wide markets).

3. Commission: $0.65/contract + $0.02 exchange fee per leg.

4. Minimum tick: $0.05 per contract (enforced by rounding fills).

5. Early assignment: not explicitly modeled in the fill; the engine handles
   assignment by closing the short leg at intrinsic + $0.01.
"""
from __future__ import annotations

from ..models import OptionContract, OptionLeg, Side


class FillModel:

    def __init__(
        self,
        adverse_fraction: float = 0.30,    # how far from mid toward adverse side
        max_spread_pct: float = 0.25,      # reject if spread > 25% of mid
        commission_per_contract: float = 0.65,
        exchange_fee_per_contract: float = 0.02,
        min_tick: float = 0.05,
    ):
        self.adverse_fraction = adverse_fraction
        self.max_spread_pct = max_spread_pct
        self.commission_per_contract = commission_per_contract
        self.exchange_fee_per_contract = exchange_fee_per_contract
        self.min_tick = min_tick

    def fill_price(self, contract: OptionContract, side: Side) -> float:
        """
        Simulated fill price for a single leg.
        Returns None if the contract is untradeable (spread too wide).
        """
        mid = contract.mid
        if mid < 0.01:
            return None

        spread_pct = contract.spread / mid
        if spread_pct > self.max_spread_pct:
            return None

        half_spread = contract.spread / 2.0
        if side == Side.SELL:
            # Selling: we receive less than mid
            raw = mid - self.adverse_fraction * half_spread
        else:
            # Buying: we pay more than mid
            raw = mid + self.adverse_fraction * half_spread

        # Round to minimum tick
        ticked = round(raw / self.min_tick) * self.min_tick
        return max(self.min_tick, ticked)

    def combo_fill_price(
        self,
        legs: list[tuple[OptionContract, OptionLeg]],
    ) -> tuple[float, float]:
        """
        Return (net_credit_or_debit, total_commission) for a multi-leg combo.
        net > 0 = credit received, net < 0 = debit paid.
        """
        net = 0.0
        total_contracts = 0

        for contract, leg in legs:
            price = self.fill_price(contract, leg.side)
            if price is None:
                raise ValueError(
                    f"Cannot fill {leg.symbol} {leg.right.value}{leg.strike} "
                    f"spread={contract.spread:.2f} mid={contract.mid:.2f} (too wide)"
                )
            sign = -1 if leg.side == Side.SELL else 1
            net += sign * price * leg.quantity
            total_contracts += leg.quantity

        commission = self.commission(total_contracts * 2)  # both legs of each contract
        return net, commission  # net positive = credit

    def commission(self, n_contracts: int) -> float:
        return (self.commission_per_contract + self.exchange_fee_per_contract) * n_contracts
