"""Execution engine — the only place that turns intent into broker orders.

Flow:
  open  → broker.submit(buy)  → record order → open a portfolio position
  close → broker.submit(sell) → record order → close position(s) → journal entry

The reference price (for paper fills + the journal) comes from the latest
stored bar. All side-effects are recorded so the audit trail is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from atlas_shared import metrics as mx
from atlas_shared.logging import get_logger
from ingest_equities.store import latest_bars
from journal_service import log_live_close
from portfolio_service import add_position, reduce_position

from execution_service.brokers import build_broker
from execution_service.brokers.base import OrderRequest
from execution_service.store import record_order

log = get_logger("execution.engine")

Intent = Literal["open", "close"]


@dataclass
class ExecutionResult:
    ok: bool
    order_id: str | None
    status: str
    fill_price: float | None = None
    realized_pnl: float | None = None
    detail: str = ""


async def _reference_price(symbol: str, fallback: float | None) -> float | None:
    bars = await latest_bars(symbol, resolution="1m", limit=1)
    if not bars.empty:
        return float(bars["close"].iloc[-1])
    return fallback


def _default_allocs(n: int) -> list[float]:
    """Default ladder split (BLUEPRINT §10.2): 40/40/20 for 3 rungs; even
    otherwise."""
    if n == 3:
        return [0.40, 0.40, 0.20]
    if n <= 0:
        return []
    return [round(1.0 / n, 4)] * n


class ExecutionEngine:
    def __init__(self) -> None:
        self.broker = build_broker()

    async def execute(
        self,
        *,
        user_id: str,
        symbol: str,
        intent: Intent,
        quantity: float,
        limit_price: float | None = None,
        sector: str | None = None,
        signal_ref: str | None = None,
        stop: float | None = None,
        target: float | None = None,
        targets: list[float] | None = None,
        allocations: list[float] | None = None,
        atr: float | None = None,
        direction: str = "long",
        time_stop_at: str | None = None,
    ) -> ExecutionResult:
        symbol = symbol.upper()
        ref = await _reference_price(symbol, limit_price)
        side = "buy" if intent == "open" else "sell"

        fill = await self.broker.submit(
            OrderRequest(
                symbol=symbol, side=side, quantity=quantity,
                limit_price=limit_price, reference_price=ref,
            )
        )

        realized: float | None = None
        if fill.ok and intent == "close":
            # quantity drives partial vs full close (ladder take-profits).
            lot = await reduce_position(user_id, symbol, quantity, fill.fill_price or ref or 0.0)
            if lot is not None:
                realized = lot.realized_pnl
                await log_live_close(
                    symbol=symbol, side=lot.direction, quantity=lot.quantity,
                    entry_price=lot.avg_cost, exit_price=lot.exit_price,
                    realized_pnl=lot.realized_pnl, opened_at=lot.opened_at,
                    notes={"order": fill.broker_order_id, "broker": self.broker.name},
                )
            else:
                # Nothing open to close — record the order but flag it.
                fill = type(fill)(ok=False, fill_price=fill.fill_price, status="rejected",
                                  detail="no open position to close")
        elif fill.ok and intent == "open":
            entry = fill.fill_price or 0.0
            tgts = targets if targets else ([target] if target is not None else [])
            allocs = allocations if allocations else _default_allocs(len(tgts))
            monitor: dict | None = None
            if stop is not None or tgts:
                monitor = {
                    "direction": direction,
                    "entry": entry,
                    "stop_init": stop,
                    "stop": stop,
                    "atr": atr if atr is not None else (abs(entry - stop) / 1.8 if stop else None),
                    "targets": tgts,
                    "allocations": allocs,
                    "targets_hit": 0,
                    "initial_qty": quantity,
                    "hwm": entry,
                    "trail_atr_mult": 3.0,
                    "time_stop_at": time_stop_at,
                }
            await add_position(
                symbol, quantity, entry, sector=sector, user_id=user_id, monitor=monitor,
            )

        order_id = await record_order(
            user_id=user_id, symbol=symbol, side=side, intent=intent,
            quantity=quantity, limit_price=limit_price, fill_price=fill.fill_price,
            status=fill.status, broker=self.broker.name, realized_pnl=realized,
            detail=fill.detail, signal_ref=signal_ref,
        )
        mx.ORDERS_TOTAL.labels(intent=intent, status=fill.status).inc()
        log.info("execution.done", user=user_id, symbol=symbol, intent=intent,
                 status=fill.status, broker=self.broker.name)
        return ExecutionResult(
            ok=fill.ok, order_id=order_id, status=fill.status,
            fill_price=fill.fill_price, realized_pnl=realized, detail=fill.detail,
        )
