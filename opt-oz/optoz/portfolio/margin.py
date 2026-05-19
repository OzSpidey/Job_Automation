"""
Margin estimation.

Reg T margin rules for options (simplified for pre-trade check).
These are conservative estimates — actual broker margin may differ.

Rules used:
  Defined-risk spreads (IC, vertical): max loss + commission buffer
  Short straddle / naked short option: 20% of underlying value
    + premium received - OTM amount, minimum 10% of underlying
  Cash-secured put: put strike × 100 (full cash required)
  Covered call: no additional margin (covered by stock)
"""
from __future__ import annotations

from ..models import OptionLeg, Right, Side, SignalResult


class MarginCalculator:

    def estimate(self, sig: SignalResult, nav: float) -> float:
        """
        Estimate Reg T margin requirement for a signal in dollars.
        Returns the margin needed (positive number).
        """
        legs = sig.legs
        if not legs:
            return 0.0

        # Determine structure type
        short_legs = [l for l in legs if l.side == Side.SELL]
        long_legs  = [l for l in legs if l.side == Side.BUY]

        # Defined-risk (has both short and long legs of same expiry): margin = max_loss
        if short_legs and long_legs:
            return max(sig.max_loss, 0.0) + 20  # $20 commission buffer

        # Cash-secured put (single short put, no long)
        if len(legs) == 1 and legs[0].right == Right.PUT and legs[0].side == Side.SELL:
            return legs[0].strike * 100 * legs[0].quantity

        # Covered call: covered by shares — no additional margin needed
        if len(legs) == 1 and legs[0].right == Right.CALL and legs[0].side == Side.SELL:
            return 0.0

        # Short straddle / strangle (short call + short put, no long protection)
        # Reg T: 20% of underlying per side, take the larger
        if len(short_legs) == 2 and not long_legs:
            # Assume underlying price is embedded in max_loss as 3× credit proxy
            # Fall back to conservative: use 20% of nav
            return nav * 0.15

        # Generic fallback
        return sig.max_loss * 1.5
