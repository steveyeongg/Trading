"""Journal persistence + the Trade→entry converter."""

from __future__ import annotations

import json

from atlas_shared import to_jsonable
from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from backtest_service.types import Trade
from sqlalchemy import text

log = get_logger("journal.store")


_INSERT = text(
    """
    INSERT INTO journal_entries
      (symbol, side, strategy, opened_at, closed_at, entry_price, exit_price,
       quantity, realized_pnl, realized_return, r_multiple, max_run_up,
       max_drawdown, bars_held, exit_reason, fees_paid, source, notes)
    VALUES
      (:symbol, :side, :strategy, :opened_at, :closed_at, :entry_price, :exit_price,
       :quantity, :realized_pnl, :realized_return, :r_multiple, :max_run_up,
       :max_drawdown, :bars_held, :exit_reason, :fees_paid, :source, CAST(:notes AS JSONB))
    """
)

_LIST = text(
    """
    SELECT symbol, side, strategy, opened_at, closed_at, entry_price, exit_price,
           quantity, realized_pnl, realized_return, r_multiple, max_run_up,
           max_drawdown, bars_held, exit_reason, fees_paid, source, notes
    FROM journal_entries
    ORDER BY closed_at DESC NULLS LAST, id DESC
    LIMIT :limit
    """
)


def trade_to_row(trade: Trade, strategy: str, source: str = "backtest") -> dict:
    """Convert a backtest Trade into an insertable row.

    R-multiple = realized PnL / initial dollar risk, where initial risk is
    |entry - stop| * qty. This is the single most useful journal stat — it
    normalises wins/losses by the risk actually taken.
    """
    initial_risk = abs(trade.entry_price - trade.stop_price) * abs(trade.qty)
    r_multiple = (trade.realized_pnl / initial_risk) if initial_risk else None
    return {
        "symbol": trade.symbol,
        "side": trade.side.value,
        "strategy": strategy,
        "opened_at": trade.entry_ts,
        "closed_at": trade.exit_ts,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "quantity": trade.qty,
        "realized_pnl": trade.realized_pnl,
        "realized_return": trade.realized_return,
        "r_multiple": r_multiple,
        "max_run_up": trade.max_run_up,
        "max_drawdown": trade.max_drawdown,
        "bars_held": trade.bars_held,
        "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
        "fees_paid": trade.fees_paid,
        "source": source,
        "notes": json.dumps(trade.notes or {}),
    }


async def log_trades(trades: list[Trade], strategy: str, source: str = "backtest") -> int:
    rows = [trade_to_row(t, strategy, source) for t in trades if not t.is_open]
    if not rows:
        return 0
    async with session_scope() as s:
        for row in rows:
            await s.execute(_INSERT, row)
    log.info("journal.logged", n=len(rows), strategy=strategy, source=source)
    return len(rows)


async def log_live_close(
    *,
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
    realized_pnl: float,
    opened_at,            # datetime
    strategy: str = "manual",
    notes: dict | None = None,
) -> None:
    """Write one journal entry for a live (paper) position close. R-multiple is
    omitted (no recorded stop on the manual close path)."""
    from datetime import UTC, datetime

    cost_basis = entry_price * abs(quantity)
    row = {
        "symbol": symbol.upper(),
        "side": side,
        "strategy": strategy,
        "opened_at": opened_at,
        "closed_at": datetime.now(UTC),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "realized_pnl": realized_pnl,
        "realized_return": (realized_pnl / cost_basis) if cost_basis else 0.0,
        "r_multiple": None,
        "max_run_up": None,
        "max_drawdown": None,
        "bars_held": None,
        "exit_reason": "manual",
        "fees_paid": 0.0,
        "source": "paper",
        "notes": json.dumps(notes or {}),
    }
    async with session_scope() as s:
        await s.execute(_INSERT, row)


async def list_entries(limit: int = 200) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(_LIST, {"limit": limit})).mappings().all()
    # Cast Decimal → float so the frontend gets JSON numbers, not strings.
    return to_jsonable([dict(r) for r in rows])
