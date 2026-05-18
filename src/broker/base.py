"""Base broker interface - abstract class for all broker implementations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict
from enum import Enum


@dataclass
class TradeCloseResult:
    """Result of a broker close_trade() call."""
    success: bool
    realized_pnl: float = 0.0
    close_price: float = 0.0

    def __bool__(self) -> bool:
        return self.success


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Trade:
    trade_id: str
    pair: str
    side: OrderSide
    units: int
    entry_price: float
    current_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0
    open_time: Optional[datetime] = None

    @property
    def is_long(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_short(self) -> bool:
        return self.side == OrderSide.SELL


@dataclass
class Position:
    pair: str
    net_units: int  # Positive = long, negative = short
    average_price: float
    unrealized_pnl: float
    trades: List[Trade]

    @property
    def is_long(self) -> bool:
        return self.net_units > 0

    @property
    def is_short(self) -> bool:
        return self.net_units < 0

    @property
    def is_flat(self) -> bool:
        return self.net_units == 0


@dataclass
class AccountInfo:
    account_id: str
    balance: float
    nav: float
    margin_used: float
    margin_available: float
    unrealized_pnl: float
    open_trade_count: int
    currency: str = "USD"


class BaseBroker(ABC):
    """Abstract base class for broker implementations."""

    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def get_account_info(self) -> Optional[AccountInfo]:
        pass

    @abstractmethod
    def get_current_price(self, pair: str) -> Optional[Dict[str, float]]:
        pass

    @abstractmethod
    def get_open_trades(self) -> List[Trade]:
        pass

    @abstractmethod
    def get_positions(self) -> List[Position]:
        pass

    @abstractmethod
    def get_position(self, pair: str) -> Optional[Position]:
        pass

    @abstractmethod
    def place_market_order(
        self,
        pair: str,
        side: OrderSide,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> Optional[str]:
        pass

    @abstractmethod
    def close_trade(self, trade_id: str) -> 'TradeCloseResult':
        pass

    @abstractmethod
    def get_closed_trade_info(self, trade_id: str) -> dict:
        pass

    @abstractmethod
    def close_position(self, pair: str) -> bool:
        pass

    @abstractmethod
    def modify_trade(
        self,
        trade_id: str,
        pair: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> bool:
        pass

    def partial_close_trade(self, trade_id: str, units: int) -> bool:
        raise NotImplementedError

    def place_limit_order(
        self,
        pair: str,
        side: OrderSide,
        units: int,
        price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[str]:
        raise NotImplementedError

    def has_open_position(self, pair: str) -> bool:
        position = self.get_position(pair)
        return position is not None and not position.is_flat
