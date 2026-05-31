"""Execution endpoint — paper/broker order placement, tier-gated.

Only tiers with `broker_autotrade` may execute. Open buys a position; close
sells the open position(s), realizes PnL, and writes a journal entry.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from atlas_shared import AuthContext
from execution_service import ExecutionEngine, list_orders
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from signal_service.auth_dep import current_user

router = APIRouter(prefix="/v1")

# One engine per process (holds the broker registry).
_engine = ExecutionEngine()


class ExecuteBody(BaseModel):
    symbol: str
    intent: Literal["open", "close"] = "open"
    quantity: float = Field(gt=0)
    limit_price: float | None = None
    sector: str | None = None
    signal_ref: str | None = None
    # Protective levels — stored with the position so the monitor can auto-exit.
    stop: float | None = None
    target: float | None = None
    targets: list[float] | None = None
    allocations: list[float] | None = None
    atr: float | None = None
    direction: str = "long"
    time_stop_at: str | None = None


@router.post("/execute")
async def execute(body: ExecuteBody, user: AuthContext = Depends(current_user)) -> dict:
    if not user.entitlements.broker_autotrade:
        raise HTTPException(
            403,
            f"broker execution requires Elite tier or above; '{user.tier}' cannot execute.",
        )
    try:
        result = await _engine.execute(
            user_id=user.user_id,
            symbol=body.symbol,
            intent=body.intent,
            quantity=body.quantity,
            limit_price=body.limit_price,
            sector=body.sector,
            signal_ref=body.signal_ref,
            stop=body.stop,
            target=body.target,
            targets=body.targets,
            allocations=body.allocations,
            atr=body.atr,
            direction=body.direction,
            time_stop_at=body.time_stop_at,
        )
    except Exception as e:
        raise HTTPException(503, "execution store unavailable") from e
    return asdict(result)


@router.get("/orders")
async def orders(limit: int = 100, user: AuthContext = Depends(current_user)) -> dict:
    try:
        return {"orders": await list_orders(user.user_id, limit=limit)}
    except Exception as e:
        raise HTTPException(503, "execution store unavailable") from e
