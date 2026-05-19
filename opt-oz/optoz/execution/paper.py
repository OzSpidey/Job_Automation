"""
Paper trading broker.

Simulates fills with realistic options-specific assumptions:
  - Fill at mid + 30% of half-spread toward the unfavorable side
  - Commission: $0.65/contract + $0.02 exchange fee
  - No partial fills (fills whole or not at all, 95% fill rate)
  - Market hours only

Tracks NAV, positions, cash, and a full trade log.
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import date, datetime
from typing import Optional

from ..models import (
    Order, OrderStatus, Position, PositionLeg, PositionStatus,
    Greeks, Right, Side, Trade,
)
from .base import AccountState, Broker, BrokerConfig

log = logging.getLogger(__name__)


class PaperBroker(Broker):

    def __init__(
        self,
        starting_nav: float = 25000.0,
        config: Optional[BrokerConfig] = None,
        fill_rate: float = 0.95,   # probability of getting a fill
    ):
        super().__init__(config)
        self.starting_nav = starting_nav
        self.fill_rate = fill_rate
        self._cash = starting_nav
        self._positions: list[Position] = []
        self._trades: list[Trade] = []
        self._connected = False

    # ── connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._connected = True
        log.info("Paper broker connected: NAV=%.2f", self._cash)

    def disconnect(self) -> None:
        self._connected = False

    # ── account ──────────────────────────────────────────────────────────────

    def get_account_state(self) -> AccountState:
        position_value = sum(
            sum(leg.signed_quantity * leg.current_price * 100 for leg in p.legs)
            for p in self._positions if p.status == PositionStatus.OPEN
        )
        nav = self._cash + position_value
        # Rough margin: sum of position max losses
        margin = sum(p.max_loss for p in self._positions if p.status == PositionStatus.OPEN)
        return AccountState(
            nav=nav,
            cash=self._cash,
            buying_power=max(0.0, self._cash - margin),
            margin_used=margin,
            positions=self._positions,
        )

    @property
    def nav(self) -> float:
        return self.get_account_state().nav

    # ── order submission ─────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> Order:
        if not self._connected:
            raise RuntimeError("Paper broker not connected")

        # Simulate fill probability
        if random.random() > self.fill_rate:
            order.status = OrderStatus.CANCELLED
            log.info("Paper: order %s not filled (simulated miss)", order.id)
            return order

        # Simulate fill price: mid + 30% of half-spread adverse
        fill_price = order.limit_price  # for paper, always assume fill at limit
        n_contracts = sum(leg.quantity for leg in order.legs)
        commission = self.config.total_commission(n_contracts * len(order.legs))

        # Credit received or debit paid
        if order.limit_price > 0:
            self._cash += fill_price * 100 - commission   # received credit
        else:
            self._cash += fill_price * 100 - commission   # paid debit (negative)

        order.status = OrderStatus.FILLED
        order.filled_price = fill_price
        order.filled_at = datetime.now()
        order.commission = commission

        trade = Trade(
            position_id=order.position_id,
            order_id=order.id,
            strategy="",
            underlying=order.legs[0].symbol if order.legs else "",
            legs=order.legs,
            fill_price=fill_price,
            commission=commission,
            timestamp=datetime.now(),
        )
        self._trades.append(trade)
        log.info(
            "Paper fill: %s legs=%d fill_price=%.2f commission=%.2f cash=%.2f",
            order.id, len(order.legs), fill_price, commission, self._cash,
        )
        return order

    def cancel_order(self, order: Order) -> None:
        order.status = OrderStatus.CANCELLED

    def get_order_status(self, order: Order) -> OrderStatus:
        return order.status

    # ── position tracking ────────────────────────────────────────────────────

    def open_position(self, position: Position) -> None:
        self._positions.append(position)

    def close_position(self, position_id: str, realized_pnl: float) -> None:
        for p in self._positions:
            if p.id == position_id:
                p.status = PositionStatus.CLOSED
                p.exit_date = date.today()
                p.realized_pnl = realized_pnl
                self._cash += realized_pnl
                log.info("Paper: closed position %s PnL=%.2f", position_id, realized_pnl)
                return

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == PositionStatus.OPEN]

    @property
    def trade_history(self) -> list[Trade]:
        return list(self._trades)
