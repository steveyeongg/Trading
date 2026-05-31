"""Position monitor — auto-manage open positions: partial ladder take-profits,
break-even + chandelier trailing stops, hard stop, and time stop.

`decide_exit` (simple) and `plan_exit` (ladder, in ladder.py) are pure and
unit-tested. `run_monitor_once` marks each open position to the latest bar,
persists the ratcheted trailing state, and closes the triggered quantity via
the execution engine (which realizes PnL + writes the journal).
"""

from __future__ import annotations

from datetime import UTC, datetime

from atlas_shared import metrics as mx
from atlas_shared.logging import get_logger
from ingest_equities.store import latest_bars
from portfolio_service import get_open, list_monitorable, update_position_meta

from execution_service.engine import ExecutionEngine
from execution_service.ladder import plan_exit

log = get_logger("execution.monitor")


def decide_exit(
    *,
    direction: str,
    last_price: float,
    stop: float | None,
    target: float | None,
    now: datetime | None = None,
    time_stop_at: datetime | None = None,
) -> str | None:
    """Return 'stop' | 'target' | 'time' | None.

    Stop is checked before target (worst-case-first, mirrors the backtest
    simulator). Long stops are below, shorts above; targets the inverse.
    """
    is_long = direction != "short"
    if stop is not None:
        if is_long and last_price <= stop:
            return "stop"
        if not is_long and last_price >= stop:
            return "stop"
    if target is not None:
        if is_long and last_price >= target:
            return "target"
        if not is_long and last_price <= target:
            return "target"
    if time_stop_at is not None:
        now = now or datetime.now(UTC)
        if now >= time_stop_at:
            return "time"
    return None


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


async def run_monitor_once(engine: ExecutionEngine) -> list[dict]:
    """One sweep. Returns a list of {symbol, user_id, reason, kind, qty} for
    actions taken (partial or full closes)."""
    positions = await list_monitorable()
    if not positions:
        return []
    actions: list[dict] = []
    now = datetime.now(UTC)
    for pos in positions:
        symbol = pos["symbol"]
        user_id = pos["user_id"]
        # Fetch the fresh open lot (qty + metadata may have changed since the
        # batch scan, e.g. a prior partial this same sweep).
        lot = await get_open(user_id, symbol)
        if lot is None or lot["quantity"] <= 0:
            continue
        bars = await latest_bars(symbol, resolution="1m", limit=1)
        if bars.empty:
            continue
        last = float(bars["close"].iloc[-1])

        action = plan_exit(meta=lot["metadata"], last_price=last, open_qty=lot["quantity"], now=now)

        # Persist the ratcheted trailing state (hwm / stop / targets_hit) even
        # when no exit fires — so the trail keeps climbing.
        await update_position_meta(lot["id"], action.meta)

        if action.kind == "none":
            continue

        qty = lot["quantity"] if action.kind == "all" else action.qty
        result = await engine.execute(
            user_id=user_id, symbol=symbol, intent="close",
            quantity=qty, limit_price=last, signal_ref=f"monitor:{action.reason}",
        )
        mx.MONITOR_EXITS_TOTAL.labels(reason=action.reason).inc()
        log.info("monitor.exit", symbol=symbol, user=user_id, reason=action.reason,
                 kind=action.kind, qty=qty, ok=result.ok)
        actions.append({"symbol": symbol, "user_id": user_id, "reason": action.reason,
                        "kind": action.kind, "qty": qty})
    return actions
