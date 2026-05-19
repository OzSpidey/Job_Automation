"""
VRP Straddle strategy.

Entry: sell ATM straddle on SPY/QQQ/IWM when 30d ATM IV > 30d realized vol
       by >= iv_rv_threshold vol points.
Delta hedge: re-hedge daily (or when |net delta| > threshold × 100).
Exit: 50% profit target, 21 DTE, or 200% stop on initial premium.

Capital use: straddle margin is roughly the naked option margin on the larger
side. At $25k NAV this limits us to SPY near-term at comfortable size.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .base import ExitReason, ExitSignal, Strategy
from ..models import OptionChain, OptionLeg, Position, Right, Side, SignalResult
from ..surface.interpolator import VolSurface

log = logging.getLogger(__name__)

_STRAT = "vrp_straddle"


class VRPStraddle(Strategy):
    name = _STRAT

    def __init__(
        self,
        underlyings: list[str] = None,
        target_dte: int = 30,
        iv_rv_threshold: float = 3.0,   # vol points (pct)
        delta_hedge_threshold: int = 5,
        profit_target_pct: float = 0.50,
        stop_loss_pct: float = 2.00,
        max_dte_exit: int = 21,
    ):
        self.underlyings = underlyings or ["SPY", "QQQ", "IWM"]
        self.target_dte = target_dte
        self.iv_rv_threshold = iv_rv_threshold
        self.delta_hedge_threshold = delta_hedge_threshold
        self.profit_target_pct = profit_target_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_dte_exit = max_dte_exit

    # ── signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        chain: OptionChain,
        surface: Optional[VolSurface],
        open_positions: list[Position],
        nav: float,
    ) -> list[SignalResult]:
        if chain.symbol not in self.underlyings:
            return []

        # One straddle per underlying at a time
        already_on = any(p.underlying == chain.symbol and p.strategy == self.name
                         for p in open_positions if p.status.value == "OPEN")
        if already_on:
            return []

        expiry = chain.nearest_expiry_by_dte(self.target_dte - 5, self.target_dte + 10)
        if expiry is None:
            return []

        # Fetch greeks-enriched ATM contracts
        call = chain.atm(Right.CALL, expiry)
        put  = chain.atm(Right.PUT,  expiry)
        if not call or not put:
            return []

        # Check entry condition: IV vs RV spread
        atm_iv = (call.iv + put.iv) / 2.0
        rv = self._get_realized_vol(chain)
        iv_rv_spread = (atm_iv - rv) * 100  # convert to vol points

        if iv_rv_spread < self.iv_rv_threshold:
            log.debug(
                "%s VRP insufficient: IV=%.1f%% RV=%.1f%% spread=%.2f vpts < %.2f threshold",
                chain.symbol, atm_iv * 100, rv * 100, iv_rv_spread, self.iv_rv_threshold,
            )
            return []

        # Use 1 contract — size managed by portfolio constructor
        qty = 1
        entry_credit = (call.mid + put.mid) * qty * 100
        # Max loss for a short straddle is theoretically unlimited; cap at
        # 3× the premium collected (used as risk budget input, not hard limit).
        max_loss = entry_credit * 3.0

        legs = [
            OptionLeg(chain.symbol, expiry, call.strike, Right.CALL, Side.SELL, qty),
            OptionLeg(chain.symbol, expiry, put.strike,  Right.PUT,  Side.SELL, qty),
        ]
        dte = (expiry - date.today()).days

        log.info(
            "VRP signal: SELL %s %s straddle K=%.0f IV=%.1f%% RV=%.1f%% spread=%.2f vpts",
            chain.symbol, expiry, call.strike, atm_iv * 100, rv * 100, iv_rv_spread,
        )
        return [SignalResult(
            strategy=self.name,
            underlying=chain.symbol,
            legs=legs,
            entry_credit=entry_credit,
            max_loss=max_loss,
            rationale=(
                f"IV={atm_iv*100:.1f}% RV={rv*100:.1f}% "
                f"spread={iv_rv_spread:.2f}vpts ≥ {self.iv_rv_threshold}vpt threshold"
            ),
            entry_date=date.today(),
            target_exit_dte=self.max_dte_exit,
            profit_target_pct=self.profit_target_pct,
        )]

    # ── exit rules ───────────────────────────────────────────────────────────

    def should_exit(self, position: Position, chain: OptionChain) -> Optional[ExitSignal]:
        if position.strategy != self.name:
            return None

        pnl = position.unrealized_pnl
        credit = position.entry_credit

        # Profit target: 50% of initial credit
        if pnl >= credit * self.profit_target_pct:
            return ExitSignal(position.id, ExitReason.PROFIT_TARGET,
                              f"PnL {pnl:.0f} ≥ {credit * self.profit_target_pct:.0f} target")

        # Stop loss: lost 2× initial credit
        if pnl <= -credit * self.stop_loss_pct:
            return ExitSignal(position.id, ExitReason.STOP_LOSS,
                              f"PnL {pnl:.0f} ≤ −{credit * self.stop_loss_pct:.0f} stop")

        # DTE exit
        if position.dte <= self.max_dte_exit:
            return ExitSignal(position.id, ExitReason.DTE_EXPIRY,
                              f"DTE {position.dte} ≤ {self.max_dte_exit}")

        return None

    def _get_realized_vol(self, chain: OptionChain) -> float:
        """30d realized vol — pulled from chain provider or computed from price history."""
        from ..data.yfinance_provider import YFinanceProvider
        try:
            return YFinanceProvider().get_realized_vol(chain.symbol, window=30)
        except Exception:
            return 0.15
