"""Pure exit-planning logic for the position monitor.

Given a position's protective metadata and the latest price, decide the next
action: nothing, a partial ladder take-profit, or a full close (stop / trail /
time / final target). Also ratchets the trailing state (high-water mark,
break-even, chandelier stop) into the returned metadata.

No I/O — exhaustively unit-testable. BLUEPRINT §10.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ExitAction:
    kind: str          # "none" | "partial" | "all"
    qty: float         # quantity to close (partial); full handled as "all"
    reason: str        # "" | "target" | "stop" | "trail" | "time"
    meta: dict[str, Any]   # metadata to persist (hwm / stop / targets_hit updated)


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def plan_exit(
    *,
    meta: dict[str, Any],
    last_price: float,
    open_qty: float,
    now: datetime | None = None,
) -> ExitAction:
    """Decide the exit action for one open position.

    Expected `meta` keys (set at open):
      direction, entry, stop_init, stop (current), atr,
      targets (list), allocations (list), targets_hit (int),
      initial_qty, hwm, trail_atr_mult, time_stop_at
    """
    now = now or datetime.now(UTC)
    direction = meta.get("direction", "long")
    is_long = direction != "short"

    entry = float(meta["entry"])
    stop_init = float(meta.get("stop_init", meta.get("stop", entry)))
    cur_stop = float(meta.get("stop", stop_init))
    atr = float(meta.get("atr") or (abs(entry - stop_init) / 1.8) or 0.0)
    targets = list(meta.get("targets", []))
    allocs = list(meta.get("allocations", []))
    hit = int(meta.get("targets_hit", 0))
    initial_qty = float(meta.get("initial_qty", open_qty))
    hwm = float(meta.get("hwm", entry))
    trail_mult = float(meta.get("trail_atr_mult", 3.0))

    new_meta = dict(meta)

    # 1) Ratchet the high-water mark.
    hwm = max(hwm, last_price) if is_long else min(hwm, last_price)
    new_meta["hwm"] = hwm

    # 2) Effective stop: break-even after first rung, then chandelier once >1R.
    eff_stop = cur_stop
    if hit >= 1:  # move to break-even
        eff_stop = max(eff_stop, entry) if is_long else min(eff_stop, entry)
    risk = (entry - stop_init) if is_long else (stop_init - entry)
    if risk > 0:
        r_mult = (last_price - entry) / risk if is_long else (entry - last_price) / risk
        if r_mult >= 1.0 and atr > 0:
            chandelier = hwm - trail_mult * atr if is_long else hwm + trail_mult * atr
            eff_stop = max(eff_stop, chandelier) if is_long else min(eff_stop, chandelier)
    new_meta["stop"] = eff_stop

    # 3) Stop (checked before targets — worst-case-first).
    stopped = (is_long and last_price <= eff_stop) or (not is_long and last_price >= eff_stop)
    if stopped:
        reason = "trail" if eff_stop != stop_init else "stop"
        return ExitAction("all", open_qty, reason, new_meta)

    # 4) Time stop.
    tsa = _parse_ts(meta.get("time_stop_at"))
    if tsa and now >= tsa:
        return ExitAction("all", open_qty, "time", new_meta)

    # 5) Target ladder.
    if hit < len(targets):
        nxt = float(targets[hit])
        reached = (is_long and last_price >= nxt) or (not is_long and last_price <= nxt)
        if reached:
            new_meta["targets_hit"] = hit + 1
            # Final rung → close the remainder.
            if hit + 1 >= len(targets):
                return ExitAction("all", open_qty, "target", new_meta)
            alloc = float(allocs[hit]) if hit < len(allocs) else 0.0
            qty = initial_qty * alloc
            if qty <= 0 or qty >= open_qty:
                return ExitAction("all", open_qty, "target", new_meta)
            return ExitAction("partial", qty, "target", new_meta)

    return ExitAction("none", 0.0, "", new_meta)
