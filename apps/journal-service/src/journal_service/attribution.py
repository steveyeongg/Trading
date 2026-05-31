"""Journal attribution — turn a list of entries into post-mortem stats."""

from __future__ import annotations

from typing import Any

import numpy as np


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate journal entries into headline stats + breakdowns.

    Reads `r_multiple`, `realized_pnl`, `exit_reason` from each entry.
    """
    if not entries:
        return {
            "n": 0,
            "hit_rate": None,
            "avg_win_r": None,
            "avg_loss_r": None,
            "expectancy_r": None,
            "total_pnl": 0.0,
            "exit_reasons": {},
            "by_symbol": {},
        }

    rs = np.array([float(e["r_multiple"]) for e in entries if e.get("r_multiple") is not None])
    pnls = np.array([float(e["realized_pnl"]) for e in entries if e.get("realized_pnl") is not None])

    wins = rs[rs > 0]
    losses = rs[rs < 0]

    exit_reasons: dict[str, int] = {}
    for e in entries:
        r = e.get("exit_reason") or "unknown"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    by_symbol: dict[str, dict[str, float]] = {}
    for e in entries:
        sym = e["symbol"]
        d = by_symbol.setdefault(sym, {"n": 0.0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += float(e.get("realized_pnl") or 0.0)

    return {
        "n": len(entries),
        "hit_rate": float(np.mean(rs > 0)) if rs.size else None,
        "avg_win_r": float(np.mean(wins)) if wins.size else None,
        "avg_loss_r": float(np.mean(losses)) if losses.size else None,
        "expectancy_r": float(np.mean(rs)) if rs.size else None,
        "total_pnl": float(np.sum(pnls)) if pnls.size else 0.0,
        "exit_reasons": exit_reasons,
        "by_symbol": by_symbol,
    }
