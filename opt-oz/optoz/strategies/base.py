"""Base class for all Opt-Oz strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..models import OptionChain, Position, SignalResult
from ..surface.interpolator import VolSurface


class ExitReason(str, Enum):
    PROFIT_TARGET   = "profit_target"
    STOP_LOSS       = "stop_loss"
    DTE_EXPIRY      = "dte_expiry"
    ROLL            = "roll"
    RISK_BREACH     = "risk_breach"
    EARNINGS_WINDOW = "earnings_window"


@dataclass
class ExitSignal:
    position_id: str
    reason: ExitReason
    message: str


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signals(
        self,
        chain: OptionChain,
        surface: Optional[VolSurface],
        open_positions: list[Position],
        nav: float,
    ) -> list[SignalResult]:
        """Return list of target trades to open."""

    @abstractmethod
    def should_exit(
        self,
        position: Position,
        chain: OptionChain,
    ) -> Optional[ExitSignal]:
        """Return ExitSignal if position should be closed now, else None."""

    def manages(self, position: Position) -> bool:
        """True if this strategy owns the given position."""
        return position.strategy == self.name
