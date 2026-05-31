"""Broker interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    limit_price: float | None = None    # None = market
    reference_price: float | None = None  # last known price, for paper fills


@dataclass(frozen=True)
class Fill:
    ok: bool
    fill_price: float | None
    status: str          # filled | rejected
    detail: str = ""
    broker_order_id: str | None = None


class Broker(Protocol):
    name: str

    @property
    def available(self) -> bool:
        ...

    async def submit(self, order: OrderRequest) -> Fill:
        ...
