"""Alpaca paper-trading broker. Thin REST client over the orders endpoint.

Enabled when ALPACA_API_KEY / ALPACA_API_SECRET are set; otherwise reports
unavailable and the registry falls back to the paper broker. Defaults to the
paper base URL — live trading is a deliberate env change, never a default.
"""

from __future__ import annotations

import os

import httpx
from atlas_shared.logging import get_logger

from execution_service.brokers.base import Fill, OrderRequest

log = get_logger("broker.alpaca")

PAPER_BASE = "https://paper-api.alpaca.markets/v2"


class AlpacaBroker:
    name = "alpaca"

    def __init__(self) -> None:
        self.key = os.environ.get("ALPACA_API_KEY")
        self.secret = os.environ.get("ALPACA_API_SECRET")
        self.base = os.environ.get("ALPACA_BASE_URL", PAPER_BASE)

    @property
    def available(self) -> bool:
        return bool(self.key and self.secret)

    async def submit(self, order: OrderRequest) -> Fill:
        if not self.available:
            return Fill(ok=False, fill_price=None, status="rejected", detail="alpaca not configured")
        headers = {
            "APCA-API-KEY-ID": self.key or "",
            "APCA-API-SECRET-KEY": self.secret or "",
        }
        body: dict = {
            "symbol": order.symbol,
            "qty": str(order.quantity),
            "side": order.side,
            "type": "limit" if order.limit_price else "market",
            "time_in_force": "day",
        }
        if order.limit_price:
            body["limit_price"] = str(order.limit_price)
        try:
            async with httpx.AsyncClient(timeout=15.0, base_url=self.base) as client:
                r = await client.post("/v2/orders", json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
            # Alpaca fills asynchronously; for the MVP we treat acceptance as a
            # fill at the limit/last price and reconcile later (Phase 3 adds a
            # fill-poller / websocket trade-updates stream).
            fill_price = order.limit_price or order.reference_price
            return Fill(
                ok=True,
                fill_price=float(fill_price) if fill_price else None,
                status="filled",
                detail=f"alpaca accepted {data.get('id', '')}",
                broker_order_id=data.get("id"),
            )
        except Exception as e:
            log.warning("broker.alpaca.failed", error=str(e))
            return Fill(ok=False, fill_price=None, status="rejected", detail=str(e)[:200])
