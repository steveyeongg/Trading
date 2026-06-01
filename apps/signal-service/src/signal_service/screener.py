"""Screener: rank candidate symbols by composite score with reasons attached.

BLUEPRINT §4 (Stock Screener Design) + §13.2 (`POST /v1/screener/run`).

Loads universes from `infra/data/universes.json`. Falls back to the default
watchlist if the file is missing so a fresh checkout still produces results.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from atlas_shared.logging import get_logger
from atlas_shared.schemas import Horizon, Signal

log = get_logger("screener")

# ── Universe loading ───────────────────────────────────────────────────────────

# screener.py lives at apps/signal-service/src/signal_service/screener.py — four
# parents up is `apps/`, five is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_UNIVERSE_PATH = _REPO_ROOT / "infra" / "data" / "universes.json"

_FALLBACK_UNIVERSES: dict[str, dict] = {
    "default_watchlist": {
        "label": "Default Watchlist",
        "description": "Embedded fallback; install infra/data/universes.json for the full set.",
        "symbols": ["AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"],
    }
}


def _universes_path() -> Path:
    """Allow `ATLAS_UNIVERSES_PATH` env override so deployments can point at
    their own list without modifying the repo."""
    return Path(os.environ.get("ATLAS_UNIVERSES_PATH", str(_DEFAULT_UNIVERSE_PATH)))


@lru_cache(maxsize=1)
def load_universes() -> dict[str, dict]:
    """Read the universes file once per process. Cache invalidated on restart."""
    path = _universes_path()
    if not path.exists():
        log.warning("screener.universes.missing", path=str(path))
        return dict(_FALLBACK_UNIVERSES)
    try:
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            raise ValueError("universes file must be a non-empty object")
        return data
    except Exception as e:
        log.warning("screener.universes.read_failed", error=str(e))
        return dict(_FALLBACK_UNIVERSES)


def list_universes() -> list[dict]:
    """Return universes as a list for the API — easier for the UI to render."""
    return [
        {
            "key": key,
            "label": meta.get("label", key),
            "description": meta.get("description", ""),
            "symbol_count": len(meta.get("symbols", [])),
        }
        for key, meta in load_universes().items()
    ]


def resolve_symbols(universe: str | None, symbols: list[str] | None) -> list[str]:
    """Build the symbol list for a scan. BLUEPRINT §4.1 — manual input must
    always work; otherwise the named universe is loaded.

    Manual symbols and universe symbols are merged + deduped when both are
    supplied so a user can append a one-off ticker to a named universe.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    if universe and universe != "manual":
        meta = load_universes().get(universe)
        if not meta:
            raise ValueError(f"unknown universe: {universe!r}")
        for s in meta.get("symbols", []):
            _add(s)

    for s in (symbols or []):
        _add(s)

    if not out:
        # Manual mode with no symbols → fall back to the default watchlist
        # rather than returning an empty result. Matches BLUEPRINT §4.3.
        for s in load_universes()["default_watchlist"]["symbols"]:
            _add(s)

    return out


# ── Run-shaped result ──────────────────────────────────────────────────────────


@dataclass
class ScreenerRow:
    """One row of a screener run. Either `signal` is set (the candidate
    published) or `no_signal_reason` explains why it didn't. Never both."""

    symbol: str
    signal: Signal | None
    no_signal_reason: str | None
    composite_score: float | None
    direction_implied: str | None


@dataclass
class ScreenerRun:
    run_id: str
    universe: str
    horizon: Horizon
    requested_symbols: list[str]
    min_composite: float
    top_n: int | None
    generated_at: datetime
    results: list[ScreenerRow]
    skipped: list[dict]

    def to_payload(self, include_explanation: bool) -> dict:
        return {
            "run_id": self.run_id,
            "universe": self.universe,
            "horizon": self.horizon.value,
            "generated_at": self.generated_at.isoformat(),
            "min_composite": self.min_composite,
            "top_n": self.top_n,
            "scanned": len(self.requested_symbols),
            "published": sum(1 for r in self.results if r.signal is not None),
            "results": [
                _row_payload(r, include_explanation=include_explanation, rank=i + 1)
                for i, r in enumerate(self.results)
            ],
            "skipped": self.skipped,
        }


def _row_payload(row: ScreenerRow, *, include_explanation: bool, rank: int) -> dict:
    if row.signal is None:
        return {
            "rank": rank,
            "symbol": row.symbol,
            "published": False,
            "composite_score": row.composite_score,
            "direction_implied": row.direction_implied,
            "no_signal_reason": row.no_signal_reason,
        }
    sig = row.signal
    payload = {
        "rank": rank,
        "symbol": sig.symbol,
        "published": True,
        "direction": sig.direction.value,
        "composite_score": sig.composite_score,
        "confidence_pct": sig.confidence_pct,
        "conviction": sig.conviction.value,
        "regime": sig.regime,
        "entry_price": sig.entry_price,
        "stop_price": sig.stop_price,
        "take_profit_levels": sig.take_profit_levels,
        "position_size_pct": sig.position_size_pct,
        "expected_rr": sig.expected_rr,
        "sub_scores": sig.sub_scores.model_dump(),
        "invalidations": sig.invalidations,
        "bar_age_seconds": sig.bar_age_seconds,
    }
    if include_explanation:
        payload["rationale_md"] = sig.rationale_md
    return payload


def make_run_id() -> str:
    now = datetime.now(UTC)
    return f"scan_{now.strftime('%Y%m%d_%H%M%S')}"
