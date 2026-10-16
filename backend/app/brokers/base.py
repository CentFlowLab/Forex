"""BrokerAdapter interface. Concrete brokers must implement every method."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


@dataclass
class ConnectionStatus:
    connected: bool
    adapter: str
    environment: str
    message: str
    is_simulated: bool
    last_heartbeat: datetime | None = None


@dataclass
class AccountInfo:
    balance: float
    equity: float
    currency: str
    unrealized_pnl: float
    margin_used: float
    is_simulated: bool


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    spread_pips: float
    ts: datetime
    is_synthetic: bool
    source: str


@dataclass
class OrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    volume_lots: float
    stop_loss: float
    take_profit: float
    order_type: OrderType = "MARKET"
    limit_price: float | None = None


@dataclass
class OrderResult:
    client_order_id: str
    broker_order_id: str
    status: str
    filled_price: float
    message: str
    accepted: bool


@dataclass
class PositionView:
    symbol: str
    side: str
    volume_lots: float
    entry_price: float
    stop_loss: float
    take_profit: float
    current_price: float
    unrealized_pnl: float
    client_order_id: str


class BrokerAdapter(ABC):
    name: str
    is_simulated: bool

    @abstractmethod
    def connect(self) -> ConnectionStatus: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def status(self) -> ConnectionStatus: ...

    @abstractmethod
    def account(self) -> AccountInfo: ...

    @abstractmethod
    def instrument(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]: ...

    @abstractmethod
    def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def order_status(self, client_order_id: str) -> OrderResult: ...

    @abstractmethod
    def positions(self) -> list[PositionView]: ...

    @abstractmethod
    def history(self, limit: int = 100) -> list[dict[str, Any]]: ...
