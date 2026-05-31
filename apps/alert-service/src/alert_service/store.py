"""Alert rules + delivery persistence."""

from __future__ import annotations

import json
from typing import Any

from atlas_shared.db import session_scope
from sqlalchemy import text

from alert_service.rules import AlertRule

_LIST_RULES = text(
    """
    SELECT id::text, name, metric, op, threshold, symbol, direction,
           channels, cooldown_s, enabled
    FROM alert_rules
    WHERE user_id = :uid
    ORDER BY created_at
    """
)

# For the broadcaster: every enabled rule across all users.
_LIST_ALL_RULES = text(
    """
    SELECT id::text, name, metric, op, threshold, symbol, direction,
           channels, cooldown_s, enabled
    FROM alert_rules
    WHERE enabled = TRUE
    ORDER BY created_at
    """
)

_INSERT_RULE = text(
    """
    INSERT INTO alert_rules (name, metric, op, threshold, symbol, direction, channels, cooldown_s, enabled, user_id)
    VALUES (:name, :metric, :op, :threshold, :symbol, :direction, :channels, :cooldown_s, :enabled, :user_id)
    RETURNING id::text
    """
)

# Ownership-scoped delete — a user can only delete their own rules.
_DELETE_RULE = text("DELETE FROM alert_rules WHERE id = :id AND user_id = :uid")

_INSERT_DELIVERY = text(
    """
    INSERT INTO alert_deliveries (rule_id, symbol, channel, ok, detail, payload)
    VALUES (:rule_id, :symbol, :channel, :ok, :detail, CAST(:payload AS JSONB))
    """
)

_LIST_DELIVERIES = text(
    """
    SELECT rule_id::text, symbol, channel, ok, detail, fired_at
    FROM alert_deliveries
    ORDER BY fired_at DESC
    LIMIT :limit
    """
)


def _row_to_rule(r: dict) -> AlertRule:
    return AlertRule(
        id=r["id"],
        name=r["name"],
        metric=r["metric"],
        op=r["op"],
        threshold=float(r["threshold"]),
        symbol=r["symbol"],
        direction=r["direction"],
        channels=tuple(r["channels"]),
        cooldown_s=int(r["cooldown_s"]),
        enabled=bool(r["enabled"]),
    )


async def list_rules(user_id: str) -> list[AlertRule]:
    """Rules owned by `user_id` (for CRUD)."""
    async with session_scope() as s:
        rows = (await s.execute(_LIST_RULES, {"uid": user_id})).mappings().all()
    return [_row_to_rule(dict(r)) for r in rows]


async def list_all_rules() -> list[AlertRule]:
    """Every enabled rule across users — for the broadcaster's evaluation."""
    async with session_scope() as s:
        rows = (await s.execute(_LIST_ALL_RULES)).mappings().all()
    return [_row_to_rule(dict(r)) for r in rows]


async def create_rule(rule: dict[str, Any], user_id: str) -> str:
    params = {
        "name": rule["name"],
        "metric": rule["metric"],
        "op": rule["op"],
        "threshold": float(rule["threshold"]),
        "symbol": (rule.get("symbol") or None),
        "direction": (rule.get("direction") or None),
        "channels": list(rule.get("channels") or ["log"]),
        "cooldown_s": int(rule.get("cooldown_s", 1800)),
        "enabled": bool(rule.get("enabled", True)),
        "user_id": user_id,
    }
    async with session_scope() as s:
        new_id = (await s.execute(_INSERT_RULE, params)).scalar_one()
    return str(new_id)


async def delete_rule(rule_id: str, user_id: str) -> None:
    async with session_scope() as s:
        await s.execute(_DELETE_RULE, {"id": rule_id, "uid": user_id})


async def record_delivery(rule_id: str | None, symbol: str, channel: str, ok: bool, detail: str, payload: dict) -> None:
    async with session_scope() as s:
        await s.execute(
            _INSERT_DELIVERY,
            {
                "rule_id": rule_id,
                "symbol": symbol,
                "channel": channel,
                "ok": ok,
                "detail": detail[:500],
                "payload": json.dumps(payload, default=str),
            },
        )


async def list_deliveries(limit: int = 100) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(_LIST_DELIVERIES, {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]
