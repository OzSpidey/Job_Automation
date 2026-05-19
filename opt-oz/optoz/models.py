"""
Shared domain models for Opt-Oz.

Every options position in the system carries its full leg structure and
computed max_loss as first-class fields. No Position object exists without
knowing its own worst case.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIAL = "PARTIAL"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


# ── Greeks ──────────────────────────────────────────────────────────────────

@dataclass
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0   # per calendar day
    vega: float = 0.0    # per 1% change in vol
    rho: float = 0.0
    iv: float = 0.0
    price: float = 0.0

    def __add__(self, other: Greeks) -> Greeks:
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
            rho=self.rho + other.rho,
        )

    def scale(self, factor: float) -> Greeks:
        return Greeks(
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            theta=self.theta * factor,
            vega=self.vega * factor,
            rho=self.rho * factor,
        )


# ── Option leg (order-level, no market data) ─────────────────────────────────

@dataclass
class OptionLeg:
    symbol: str       # underlying ticker
    expiry: date
    strike: float
    right: Right
    side: Side
    quantity: int     # number of contracts (always positive)

    MULTIPLIER: int = field(default=100, init=False, repr=False)

    @property
    def signed_quantity(self) -> int:
        """Positive for long, negative for short."""
        return self.quantity if self.side == Side.BUY else -self.quantity


# ── Position leg (includes live market data) ─────────────────────────────────

@dataclass
class PositionLeg:
    symbol: str
    expiry: date
    strike: float
    right: Right
    side: Side
    quantity: int
    entry_price: float = 0.0
    current_price: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)

    MULTIPLIER: int = field(default=100, init=False, repr=False)

    @property
    def signed_quantity(self) -> int:
        return self.quantity if self.side == Side.BUY else -self.quantity

    @property
    def leg_pnl(self) -> float:
        return self.signed_quantity * (self.current_price - self.entry_price) * self.MULTIPLIER

    def to_option_leg(self) -> OptionLeg:
        return OptionLeg(
            symbol=self.symbol,
            expiry=self.expiry,
            strike=self.strike,
            right=self.right,
            side=self.side,
            quantity=self.quantity,
        )


# ── Position (full trade record) ─────────────────────────────────────────────

@dataclass
class Position:
    strategy: str
    underlying: str
    legs: list[PositionLeg]
    entry_date: date
    max_loss: float        # worst-case loss in $; always computed at entry
    entry_credit: float    # positive = net credit received; negative = debit paid
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: PositionStatus = PositionStatus.OPEN
    exit_date: Optional[date] = None
    realized_pnl: float = 0.0
    notes: str = ""

    @property
    def dte(self) -> int:
        """Calendar days to nearest expiry leg."""
        today = date.today()
        return min((leg.expiry - today).days for leg in self.legs)

    @property
    def unrealized_pnl(self) -> float:
        return sum(leg.leg_pnl for leg in self.legs)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def net_greeks(self) -> Greeks:
        g = Greeks()
        for leg in self.legs:
            sq = leg.signed_quantity
            lg = leg.greeks
            g.delta += sq * lg.delta * leg.MULTIPLIER
            g.gamma += sq * lg.gamma * leg.MULTIPLIER
            g.theta += sq * lg.theta * leg.MULTIPLIER
            g.vega  += sq * lg.vega  * leg.MULTIPLIER
            g.rho   += sq * lg.rho   * leg.MULTIPLIER
        return g

    @property
    def profit_target_credit(self) -> float:
        """Dollar value of 50% profit target (default)."""
        return self.entry_credit * 0.50

    def is_near_expiry(self, dte_threshold: int = 2) -> bool:
        return self.dte <= dte_threshold


# ── Portfolio aggregate greeks ────────────────────────────────────────────────

@dataclass
class PortfolioGreeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0

    @classmethod
    def from_positions(cls, positions: list[Position]) -> PortfolioGreeks:
        pg = cls()
        for pos in positions:
            g = pos.net_greeks
            pg.delta += g.delta
            pg.gamma += g.gamma
            pg.theta += g.theta
            pg.vega  += g.vega
            pg.rho   += g.rho
        return pg


# ── Option contract (from chain snapshot) ────────────────────────────────────

@dataclass
class OptionContract:
    symbol: str
    expiry: date
    strike: float
    right: Right
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    underlying_price: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        if self.mid < 0.01:
            return 1.0
        return self.spread / self.mid

    @property
    def greeks(self) -> Greeks:
        return Greeks(
            delta=self.delta,
            gamma=self.gamma,
            theta=self.theta,
            vega=self.vega,
            iv=self.iv,
            price=self.mid,
        )


# ── Option chain (full snapshot for one underlying) ──────────────────────────

@dataclass
class OptionChain:
    symbol: str
    snapshot_date: date
    underlying_price: float
    contracts: list[OptionContract]

    def expiries(self) -> list[date]:
        return sorted({c.expiry for c in self.contracts})

    def by_expiry(self, expiry: date) -> list[OptionContract]:
        return [c for c in self.contracts if c.expiry == expiry]

    def near_delta(
        self,
        target_delta: float,
        right: Right,
        expiry: date,
        tolerance: float = 0.07,
    ) -> Optional[OptionContract]:
        """Return contract whose |delta| is closest to target_delta."""
        candidates = [
            c for c in self.by_expiry(expiry)
            if c.right == right and abs(abs(c.delta) - abs(target_delta)) < tolerance
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(abs(c.delta) - abs(target_delta)))

    def atm(self, right: Right, expiry: date) -> Optional[OptionContract]:
        return self.near_delta(0.50, right, expiry, tolerance=0.10)

    def nearest_expiry_by_dte(self, min_dte: int, max_dte: int) -> Optional[date]:
        today = date.today()
        valid = [
            e for e in self.expiries()
            if min_dte <= (e - today).days <= max_dte
        ]
        return valid[0] if valid else None


# ── Signal result (what a strategy wants to trade) ───────────────────────────

@dataclass
class SignalResult:
    strategy: str
    underlying: str
    legs: list[OptionLeg]
    entry_credit: float     # net credit (+) or debit (-)
    max_loss: float         # worst-case dollar loss (always positive)
    rationale: str
    entry_date: date
    target_exit_dte: int = 21
    profit_target_pct: float = 0.50


# ── Orders ────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    legs: list[OptionLeg]
    limit_price: float          # net credit (+) or debit (-) for combo
    position_id: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_at: Optional[datetime] = None
    commission: float = 0.0
    broker_order_id: Optional[str] = None


# ── Trade record (filled order) ───────────────────────────────────────────────

@dataclass
class Trade:
    position_id: str
    order_id: str
    strategy: str
    underlying: str
    legs: list[OptionLeg]
    fill_price: float
    commission: float
    timestamp: datetime
    is_opening: bool = True     # True = opening trade, False = closing/roll
