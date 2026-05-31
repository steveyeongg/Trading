"""Positions persistence."""

from __future__ import annotations

from dataclasses import dataclass

from atlas_shared.db import session_scope
from sqlalchemy import text

DEFAULT_USER = "dashboard"


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    asset_class: str
    sector: str | None
    quantity: float
    avg_cost: float


_RESOLVE_PORTFOLIO = text(
    "SELECT id::text, cash_balance FROM portfolios WHERE user_id = :uid ORDER BY created_at LIMIT 1"
)

_ENSURE_PORTFOLIO = text(
    """
    INSERT INTO portfolios (name, user_id, cash_balance)
    VALUES ('Default', :uid, 100000)
    RETURNING id::text, cash_balance
    """
)

_LIST_SQL = text(
    """
    SELECT symbol, asset_class, sector, quantity, avg_cost
    FROM positions
    WHERE portfolio_id = :pid AND closed_at IS NULL
    ORDER BY symbol
    """
)

# All open positions across users that carry protective levels — for the
# position monitor. Joins to portfolios to recover the owning user_id.
_LIST_MONITORABLE = text(
    """
    SELECT pf.user_id AS user_id, p.symbol AS symbol, p.quantity AS quantity,
           p.metadata AS metadata
    FROM positions p
    JOIN portfolios pf ON pf.id = p.portfolio_id
    WHERE p.closed_at IS NULL
      AND (p.metadata ? 'stop' OR p.metadata ? 'target' OR p.metadata ? 'time_stop_at')
    """
)

_INSERT_SQL = text(
    """
    INSERT INTO positions (portfolio_id, symbol, asset_class, sector, quantity, avg_cost, metadata)
    VALUES (:pid, :symbol, :asset_class, :sector, :quantity, :avg_cost, CAST(:metadata AS JSONB))
    """
)


async def _resolve(user_id: str, *, create: bool = False) -> tuple[str | None, float]:
    """Return (portfolio_id, cash) for a user; optionally create if missing."""
    async with session_scope() as s:
        row = (await s.execute(_RESOLVE_PORTFOLIO, {"uid": user_id})).mappings().first()
        if row:
            return row["id"], float(row["cash_balance"])
        if create:
            new = (await s.execute(_ENSURE_PORTFOLIO, {"uid": user_id})).mappings().first()
            return new["id"], float(new["cash_balance"])
    return None, 0.0


async def list_positions(user_id: str = DEFAULT_USER) -> list[PositionRow]:
    pid, _ = await _resolve(user_id)
    if pid is None:
        return []
    async with session_scope() as s:
        rows = (await s.execute(_LIST_SQL, {"pid": pid})).mappings().all()
    return [
        PositionRow(
            symbol=r["symbol"],
            asset_class=r["asset_class"],
            sector=r["sector"],
            quantity=float(r["quantity"]),
            avg_cost=float(r["avg_cost"]),
        )
        for r in rows
    ]


async def cash_balance(user_id: str = DEFAULT_USER) -> float:
    _, cash = await _resolve(user_id)
    return cash


async def add_position(
    symbol: str,
    quantity: float,
    avg_cost: float,
    asset_class: str = "equity",
    sector: str | None = None,
    user_id: str = DEFAULT_USER,
    *,
    monitor: dict | None = None,
) -> None:
    import json as _json

    pid, _ = await _resolve(user_id, create=True)
    # Protective levels / trailing state live in metadata so the monitor can
    # auto-manage the position (partial ladder exits, trailing stops).
    meta: dict = dict(monitor or {})
    async with session_scope() as s:
        await s.execute(
            _INSERT_SQL,
            {
                "pid": pid,
                "symbol": symbol.upper(),
                "asset_class": asset_class,
                "sector": sector,
                "quantity": quantity,
                "avg_cost": avg_cost,
                "metadata": _json.dumps(meta),
            },
        )


async def list_monitorable() -> list[dict]:
    """All open positions (across users) carrying protective levels."""
    async with session_scope() as s:
        rows = (await s.execute(_LIST_MONITORABLE)).mappings().all()
    return [
        {
            "user_id": r["user_id"],
            "symbol": r["symbol"],
            "quantity": float(r["quantity"]),
            "metadata": r["metadata"] or {},
        }
        for r in rows
    ]


@dataclass(frozen=True)
class ClosedLot:
    symbol: str
    quantity: float
    avg_cost: float
    exit_price: float
    realized_pnl: float
    opened_at: object  # datetime
    sector: str | None
    direction: str = "long"


def realized_pnl(direction: str, avg_cost: float, exit_price: float, qty: float) -> float:
    """Direction-aware realized PnL. Long profits when price rises; short when
    it falls. Pure — the testable core of position closing."""
    if direction == "short":
        return (avg_cost - exit_price) * qty
    return (exit_price - avg_cost) * qty


_CLOSE_SQL = text(
    """
    UPDATE positions
    SET closed_at = now(), realized_pnl = :pnl
    WHERE portfolio_id = :pid AND symbol = :symbol AND closed_at IS NULL
    RETURNING quantity, avg_cost, opened_at, sector
    """
)


async def close_position(user_id: str, symbol: str, exit_price: float) -> ClosedLot | None:
    """Close all open lots of `symbol` for the user at `exit_price`. Returns a
    single aggregated ClosedLot (qty-weighted avg cost), or None if flat."""
    pid, _ = await _resolve(user_id)
    if pid is None:
        return None
    async with session_scope() as s:
        rows = (
            await s.execute(_CLOSE_SQL, {"pid": pid, "symbol": symbol.upper(), "pnl": 0})
        ).mappings().all()
    if not rows:
        return None
    total_qty = sum(float(r["quantity"]) for r in rows)
    if total_qty == 0:
        return None
    avg_cost = sum(float(r["quantity"]) * float(r["avg_cost"]) for r in rows) / total_qty
    realized = (exit_price - avg_cost) * total_qty
    return ClosedLot(
        symbol=symbol.upper(),
        quantity=total_qty,
        avg_cost=avg_cost,
        exit_price=exit_price,
        realized_pnl=realized,
        opened_at=rows[0]["opened_at"],
        sector=rows[0]["sector"],
    )


_GET_OPEN = text(
    """
    SELECT id::text AS id, quantity, avg_cost, opened_at, sector, metadata
    FROM positions
    WHERE portfolio_id = :pid AND symbol = :symbol AND closed_at IS NULL
    ORDER BY opened_at
    LIMIT 1
    """
)

_UPDATE_META = text("UPDATE positions SET metadata = CAST(:meta AS JSONB) WHERE id = :id")

_REDUCE = text("UPDATE positions SET quantity = quantity - :qty WHERE id = :id")

_INSERT_CLOSED_PORTION = text(
    """
    INSERT INTO positions
      (portfolio_id, symbol, asset_class, sector, quantity, avg_cost, opened_at, closed_at, realized_pnl, metadata)
    VALUES
      (:pid, :symbol, 'equity', :sector, :qty, :avg_cost, :opened_at, now(), :pnl, CAST(:meta AS JSONB))
    """
)

_CLOSE_BY_ID = text("UPDATE positions SET closed_at = now(), realized_pnl = :pnl WHERE id = :id")


async def get_open(user_id: str, symbol: str) -> dict | None:
    """Single open lot for (user, symbol) with its metadata — for the monitor."""
    pid, _ = await _resolve(user_id)
    if pid is None:
        return None
    async with session_scope() as s:
        row = (await s.execute(_GET_OPEN, {"pid": pid, "symbol": symbol.upper()})).mappings().first()
    if row is None:
        return None
    return {
        "id": row["id"],
        "quantity": float(row["quantity"]),
        "avg_cost": float(row["avg_cost"]),
        "opened_at": row["opened_at"],
        "sector": row["sector"],
        "metadata": row["metadata"] or {},
    }


async def update_position_meta(position_id: str, meta: dict) -> None:
    import json as _json

    async with session_scope() as s:
        await s.execute(_UPDATE_META, {"id": position_id, "meta": _json.dumps(meta)})


async def reduce_position(user_id: str, symbol: str, qty: float, exit_price: float) -> ClosedLot | None:
    """Close `qty` of the open (user, symbol) lot. Full close when qty >= open;
    otherwise splits — decrement the open row, insert a closed portion row.
    Realized PnL respects the position's direction (long vs short), read from
    its metadata."""
    import json as _json

    pid, _ = await _resolve(user_id)
    if pid is None:
        return None
    open_lot = await get_open(user_id, symbol)
    if open_lot is None:
        return None
    open_qty = open_lot["quantity"]
    avg_cost = open_lot["avg_cost"]
    closing = min(qty, open_qty)
    if closing <= 0:
        return None
    meta = open_lot["metadata"]
    direction = (meta or {}).get("direction", "long")
    realized = realized_pnl(direction, avg_cost, exit_price, closing)
    is_short = direction == "short"
    async with session_scope() as s:
        if closing >= open_qty - 1e-9:
            await s.execute(_CLOSE_BY_ID, {"id": open_lot["id"], "pnl": realized})
        else:
            await s.execute(_REDUCE, {"id": open_lot["id"], "qty": closing})
            await s.execute(
                _INSERT_CLOSED_PORTION,
                {
                    "pid": pid, "symbol": symbol.upper(), "sector": open_lot["sector"],
                    "qty": closing, "avg_cost": avg_cost, "opened_at": open_lot["opened_at"],
                    "pnl": realized, "meta": _json.dumps(meta),
                },
            )
    return ClosedLot(
        symbol=symbol.upper(), quantity=closing, avg_cost=avg_cost,
        exit_price=exit_price, realized_pnl=realized,
        opened_at=open_lot["opened_at"], sector=open_lot["sector"],
        direction="short" if is_short else "long",
    )
