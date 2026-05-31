"""Order persistence."""

from __future__ import annotations

from atlas_shared import to_jsonable
from atlas_shared.db import session_scope
from sqlalchemy import text

_INSERT = text(
    """
    INSERT INTO orders
      (user_id, symbol, side, intent, quantity, limit_price, fill_price,
       status, broker, realized_pnl, detail, signal_ref)
    VALUES
      (:user_id, :symbol, :side, :intent, :quantity, :limit_price, :fill_price,
       :status, :broker, :realized_pnl, :detail, :signal_ref)
    RETURNING id::text
    """
)

_LIST = text(
    """
    SELECT id::text, symbol, side, intent, quantity, limit_price, fill_price,
           status, broker, realized_pnl, detail, signal_ref, created_at
    FROM orders
    WHERE user_id = :uid
    ORDER BY created_at DESC
    LIMIT :limit
    """
)


async def record_order(**fields) -> str:
    async with session_scope() as s:
        new_id = (await s.execute(_INSERT, fields)).scalar_one()
    return str(new_id)


async def list_orders(user_id: str, limit: int = 100) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(_LIST, {"uid": user_id, "limit": limit})).mappings().all()
    # Cast Decimal → float so the frontend gets JSON numbers, not strings.
    return to_jsonable([dict(r) for r in rows])
