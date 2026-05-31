"""Alert rule CRUD + delivery history."""

from __future__ import annotations

from alert_service import create_rule, delete_rule, list_deliveries, list_rules
from atlas_shared import AuthContext, within_limit
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from signal_service.auth_dep import current_user

router = APIRouter(prefix="/v1")


class RuleBody(BaseModel):
    name: str
    metric: str = "composite"
    op: str = ">="
    threshold: float = 70.0
    symbol: str | None = None
    direction: str | None = None
    channels: list[str] = Field(default_factory=lambda: ["log"])
    cooldown_s: int = 1800
    enabled: bool = True


@router.get("/alerts")
async def get_alerts(user: AuthContext = Depends(current_user)) -> dict:
    try:
        rules = await list_rules(user.user_id)
    except Exception as e:
        raise HTTPException(503, "alert store unavailable") from e
    return {
        "rules": [
            {
                "id": r.id, "name": r.name, "metric": r.metric, "op": r.op,
                "threshold": r.threshold, "symbol": r.symbol, "direction": r.direction,
                "channels": list(r.channels), "cooldown_s": r.cooldown_s, "enabled": r.enabled,
            }
            for r in rules
        ]
    }


@router.post("/alerts")
async def post_alert(body: RuleBody, user: AuthContext = Depends(current_user)) -> dict:
    ent = user.entitlements
    # Channel must be permitted by tier (e.g. telegram/webhook are paid).
    bad = [c for c in body.channels if c not in ent.channels]
    if bad:
        raise HTTPException(
            403, f"channels {bad} not available on tier '{user.tier}' (allowed: {list(ent.channels)})"
        )
    try:
        existing = await list_rules(user.user_id)
    except Exception as e:
        raise HTTPException(503, "alert store unavailable") from e
    # Rule-count cap per tier (counts only this user's rules).
    if not within_limit(len(existing) + 1, ent.max_alerts):
        raise HTTPException(
            403, f"alert limit for tier '{user.tier}' is {ent.max_alerts}. Upgrade to add more."
        )
    try:
        new_id = await create_rule(body.model_dump(), user.user_id)
    except Exception as e:
        raise HTTPException(503, "alert store unavailable") from e
    return {"id": new_id}


@router.delete("/alerts/{rule_id}")
async def remove_alert(rule_id: str, user: AuthContext = Depends(current_user)) -> dict:
    try:
        await delete_rule(rule_id, user.user_id)
    except Exception as e:
        raise HTTPException(503, "alert store unavailable") from e
    return {"deleted": rule_id}


@router.get("/alerts/deliveries")
async def get_deliveries(limit: int = 100) -> dict:
    try:
        return {"deliveries": await list_deliveries(limit=limit)}
    except Exception as e:
        raise HTTPException(503, "alert store unavailable") from e
