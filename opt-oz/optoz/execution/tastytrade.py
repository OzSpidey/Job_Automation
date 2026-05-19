"""
Tastytrade execution via their REST API.

Tastytrade has a clean documented REST API. Capped commissions ($10 max per
leg side) make it competitive for defined-risk spreads at small size.

API docs: https://developer.tastytrade.com/

Authentication uses username/password → session token (short-lived).
Sandbox (paper) available at sandbox.tastytrade.com.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from ..models import Order, OrderStatus, Right, Side
from .base import AccountState, Broker, BrokerConfig

log = logging.getLogger(__name__)

_LIVE_BASE    = "https://api.tastytrade.com"
_SANDBOX_BASE = "https://api.cert.tastytrade.com"


class TastytradeBroker(Broker):

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        sandbox: bool = True,
        config: Optional[BrokerConfig] = None,
    ):
        super().__init__(config)
        self.username = username or os.getenv("TASTYTRADE_USERNAME", "")
        self.password = password or os.getenv("TASTYTRADE_PASSWORD", "")
        self.base = _SANDBOX_BASE if sandbox else _LIVE_BASE
        self._session_token: Optional[str] = None
        self._account_number: Optional[str] = None
        self._client = httpx.Client(timeout=30)

    # ── connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        r = self._client.post(
            f"{self.base}/sessions",
            json={"login": self.username, "password": self.password},
        )
        r.raise_for_status()
        data = r.json()["data"]
        self._session_token = data["session-token"]
        self._client.headers["Authorization"] = self._session_token

        # Fetch first account number
        accounts_r = self._client.get(f"{self.base}/customers/me/accounts")
        accounts_r.raise_for_status()
        accounts = accounts_r.json()["data"]["items"]
        self._account_number = accounts[0]["account"]["account-number"]
        log.info("Tastytrade connected: account %s", self._account_number)

    def disconnect(self) -> None:
        if self._session_token:
            try:
                self._client.delete(f"{self.base}/sessions")
            except Exception:
                pass
        log.info("Tastytrade disconnected")

    # ── account ──────────────────────────────────────────────────────────────

    def get_account_state(self) -> AccountState:
        r = self._client.get(f"{self.base}/accounts/{self._account_number}/balances")
        r.raise_for_status()
        bal = r.json()["data"]
        return AccountState(
            nav=float(bal.get("net-liquidating-value", 0)),
            cash=float(bal.get("cash-balance", 0)),
            buying_power=float(bal.get("derivative-buying-power", 0)),
            margin_used=float(bal.get("maintenance-requirement", 0)),
            positions=[],
        )

    # ── order submission ─────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> Order:
        legs_payload = []
        for leg in order.legs:
            legs_payload.append({
                "instrument-type": "Equity Option",
                "symbol": _option_symbol(leg),
                "quantity": str(leg.quantity),
                "action": "Sell to Open" if leg.side == Side.SELL else "Buy to Open",
            })

        body = {
            "order-type": "Limit",
            "time-in-force": "Day",
            "price": str(round(abs(order.limit_price), 2)),
            "price-effect": "Credit" if order.limit_price > 0 else "Debit",
            "legs": legs_payload,
        }

        r = self._client.post(
            f"{self.base}/accounts/{self._account_number}/orders",
            json=body,
        )
        r.raise_for_status()
        data = r.json()["data"]["order"]
        order.broker_order_id = str(data["id"])
        order.status = _map_status(data["status"])
        log.info("Tastytrade order submitted: id=%s status=%s", order.broker_order_id, order.status)
        return order

    def cancel_order(self, order: Order) -> None:
        if order.broker_order_id:
            r = self._client.delete(
                f"{self.base}/accounts/{self._account_number}/orders/{order.broker_order_id}"
            )
            log.info("Tastytrade order cancelled: %s status=%d", order.broker_order_id, r.status_code)

    def get_order_status(self, order: Order) -> OrderStatus:
        if not order.broker_order_id:
            return order.status
        r = self._client.get(
            f"{self.base}/accounts/{self._account_number}/orders/{order.broker_order_id}"
        )
        r.raise_for_status()
        status_str = r.json()["data"]["status"]
        return _map_status(status_str)


def _option_symbol(leg) -> str:
    """OCC option symbol: AAPL  230120C00150000"""
    exp = leg.expiry.strftime("%y%m%d")
    right = "C" if leg.right == Right.CALL else "P"
    strike_str = f"{int(leg.strike * 1000):08d}"
    return f"{leg.symbol:<6}{exp}{right}{strike_str}"


def _map_status(s: str) -> OrderStatus:
    mapping = {
        "Received":  OrderStatus.PENDING,
        "Live":      OrderStatus.PENDING,
        "Filled":    OrderStatus.FILLED,
        "Cancelled": OrderStatus.CANCELLED,
        "Rejected":  OrderStatus.REJECTED,
        "Partially Filled": OrderStatus.PARTIAL,
    }
    return mapping.get(s, OrderStatus.PENDING)
