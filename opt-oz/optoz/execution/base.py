"""Abstract broker interface for Opt-Oz."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..models import Order, OrderStatus, Position


@dataclass
class BrokerConfig:
    commission_per_contract: float = 0.65
    exchange_fee_per_contract: float = 0.02

    # Limit order improvement schedule:
    # After N seconds with no fill, move price by this fraction of the spread
    improvement_schedule: dict[int, float] = None

    def __post_init__(self):
        if self.improvement_schedule is None:
            self.improvement_schedule = {0: 0.0, 30: 0.15, 60: 0.30, 120: 0.50}

    def total_commission(self, n_contracts: int) -> float:
        return (self.commission_per_contract + self.exchange_fee_per_contract) * n_contracts


@dataclass
class AccountState:
    nav: float
    cash: float
    buying_power: float
    margin_used: float
    positions: list[Position]


class Broker(ABC):

    def __init__(self, config: Optional[BrokerConfig] = None):
        self.config = config or BrokerConfig()

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to broker."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down connection."""

    @abstractmethod
    def get_account_state(self) -> AccountState:
        """Return current account balance and positions."""

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """
        Submit a combo/spread order as a single multi-leg order.
        NEVER leg into spreads — always submit as combo.
        Returns the order with updated status and broker_order_id.
        """

    @abstractmethod
    def cancel_order(self, order: Order) -> None:
        """Cancel a pending order."""

    @abstractmethod
    def get_order_status(self, order: Order) -> OrderStatus:
        """Poll for current order status."""

    def total_commission(self, n_contracts: int) -> float:
        return self.config.total_commission(n_contracts)
