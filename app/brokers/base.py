"""Pluggable broker adapter interface.

Safety rules enforced here:
- Every entry order (`buy` / `sell`) MUST carry a stop-loss (`stop` not None). Naked entries raise `NakedEntryError`.
- Every order uses a deterministic `client_order_id` derived from `signal_id` so retries after timeouts
  never create duplicate orders at the broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class BrokerError(RuntimeError):
    pass


class NakedEntryError(BrokerError):
    pass


@dataclass
class BrokerOrderResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    fill_price: Decimal
    slippage: Decimal
    fee: Decimal
    realized_pnl: Decimal
    status: str  # filled | submitted | cancelled
    stop: Decimal | None
    target: Decimal | None
    filled_at: datetime
    broker_fill_id: str


@dataclass
class BrokerPosition:
    symbol: str
    direction: str  # long | short
    qty: Decimal
    avg_price: Decimal
    stop: Decimal | None
    target: Decimal | None
    unrealized_pnl: Decimal


@dataclass
class BrokerBalance:
    currency: str
    cash_balance: Decimal
    equity: Decimal
    unrealized_pnl: Decimal


@dataclass
class Quote:
    symbol: str
    price: Decimal
    ts: datetime
    source: str
    stale: bool = False


class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price_hint: Decimal,
        stop: Decimal | None,
        target: Decimal | None,
    ) -> BrokerOrderResult:
        ...

    @abstractmethod
    def cancel_all_orders(self, symbol: str | None = None) -> int:
        ...

    @abstractmethod
    def flatten_all_positions(self, price_hints: dict[str, Decimal] | None = None) -> list[BrokerOrderResult]:
        ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]:
        ...

    @abstractmethod
    def balances(self) -> BrokerBalance:
        ...

    @abstractmethod
    def quote(self, symbol: str, fallback_price: Decimal | None = None) -> Quote:
        ...
