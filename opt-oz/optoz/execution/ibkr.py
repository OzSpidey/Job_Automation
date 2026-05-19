"""
Interactive Brokers execution via ib_insync.

TWS or IB Gateway must be running and accepting connections.
Use port 7497 for paper trading, 4001 for live Gateway.

ib_insync wraps ibapi in a cleaner sync/async API.
Multi-leg spread orders are submitted as BagCombo orders — the correct way
to send spreads to IBKR. Never leg in separately.

Install: pip install ib_insync
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..models import OptionLeg, Order, OrderStatus, Position, Right, Side
from .base import AccountState, Broker, BrokerConfig

log = logging.getLogger(__name__)


class IBKRBroker(Broker):

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        config: Optional[BrokerConfig] = None,
    ):
        super().__init__(config)
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib = None

    # ── connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            from ib_insync import IB
            self._ib = IB()
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=10)
            log.info("IBKR connected: %s:%d clientId=%d", self.host, self.port, self.client_id)
        except Exception as exc:
            log.error("IBKR connection failed: %s", exc)
            raise

    def disconnect(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            log.info("IBKR disconnected")

    def _require_connection(self):
        if not self._ib or not self._ib.isConnected():
            raise RuntimeError("IBKR not connected — call connect() first")

    # ── account ──────────────────────────────────────────────────────────────

    def get_account_state(self) -> AccountState:
        self._require_connection()
        from ib_insync import util

        summary = {s.tag: s.value for s in self._ib.accountSummary()}
        nav = float(summary.get("NetLiquidation", 0))
        cash = float(summary.get("TotalCashValue", 0))
        buying_power = float(summary.get("BuyingPower", 0))
        margin_used = float(summary.get("MaintMarginReq", 0))

        return AccountState(
            nav=nav,
            cash=cash,
            buying_power=buying_power,
            margin_used=margin_used,
            positions=[],  # positions managed by opt-oz internal state
        )

    # ── order submission ─────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> Order:
        self._require_connection()
        from ib_insync import Bag, ComboLeg, Contract, LimitOrder

        combo = Bag()
        combo.symbol = order.legs[0].symbol
        combo.currency = "USD"
        combo.exchange = "SMART"
        combo.comboLegs = []

        for leg in order.legs:
            contract = self._option_contract(leg)
            details = self._ib.reqContractDetails(contract)
            if not details:
                raise RuntimeError(f"No contract details for {leg}")
            con_id = details[0].contract.conId
            action = "SELL" if leg.side == Side.SELL else "BUY"
            combo_leg = ComboLeg()
            combo_leg.conId = con_id
            combo_leg.ratio = leg.quantity
            combo_leg.action = action
            combo_leg.exchange = "SMART"
            combo.comboLegs.append(combo_leg)

        limit_price = round(abs(order.limit_price), 2)
        action = "SELL" if order.limit_price > 0 else "BUY"  # selling credit / buying debit
        ibkr_order = LimitOrder(action, 1, limit_price)
        ibkr_order.tif = "DAY"
        ibkr_order.transmit = True

        trade = self._ib.placeOrder(combo, ibkr_order)
        self._ib.sleep(1)  # allow order to register

        order.broker_order_id = str(trade.order.orderId)
        order.status = _map_status(trade.orderStatus.status)
        log.info("IBKR order submitted: %s %s @ %.2f | ibkr_id=%s",
                 action, combo.symbol, limit_price, order.broker_order_id)
        return order

    def cancel_order(self, order: Order) -> None:
        self._require_connection()
        from ib_insync import Order as IBOrder
        if order.broker_order_id:
            ib_order = IBOrder()
            ib_order.orderId = int(order.broker_order_id)
            self._ib.cancelOrder(ib_order)
            log.info("IBKR order cancelled: %s", order.broker_order_id)

    def get_order_status(self, order: Order) -> OrderStatus:
        self._require_connection()
        trades = self._ib.trades()
        for t in trades:
            if str(t.order.orderId) == order.broker_order_id:
                return _map_status(t.orderStatus.status)
        return order.status

    # ── helpers ──────────────────────────────────────────────────────────────

    def _option_contract(self, leg: OptionLeg):
        from ib_insync import Option
        return Option(
            symbol=leg.symbol,
            lastTradeDateOrContractMonth=leg.expiry.strftime("%Y%m%d"),
            strike=leg.strike,
            right=leg.right.value,
            exchange="SMART",
            currency="USD",
            multiplier="100",
        )


def _map_status(ib_status: str) -> OrderStatus:
    mapping = {
        "Submitted":     OrderStatus.PENDING,
        "PreSubmitted":  OrderStatus.PENDING,
        "Filled":        OrderStatus.FILLED,
        "Cancelled":     OrderStatus.CANCELLED,
        "Inactive":      OrderStatus.CANCELLED,
        "PartiallyFilled": OrderStatus.PARTIAL,
    }
    return mapping.get(ib_status, OrderStatus.PENDING)
