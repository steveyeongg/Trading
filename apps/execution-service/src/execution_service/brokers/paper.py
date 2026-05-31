"""In-process paper broker. Fills immediately at the limit price if given,
else the reference (last) price. No credentials, always available — makes the
whole execution flow demonstrable offline."""

from __future__ import annotations

import uuid

from atlas_shared.logging import get_logger

from execution_service.brokers.base import Fill, OrderRequest

log = get_logger("broker.paper")


class PaperBroker:
    name = "paper"

    @property
    def available(self) -> bool:
        return True

    async def submit(self, order: OrderRequest) -> Fill:
        price = order.limit_price or order.reference_price
        if price is None or price <= 0:
            return Fill(ok=False, fill_price=None, status="rejected", detail="no price available")
        if order.quantity <= 0:
            return Fill(ok=False, fill_price=None, status="rejected", detail="non-positive quantity")
        log.info("broker.paper.fill", symbol=order.symbol, side=order.side, qty=order.quantity, price=price)
        return Fill(
            ok=True,
            fill_price=float(price),
            status="filled",
            detail="paper fill",
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
