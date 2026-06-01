"""Provider status + data-freshness reporting.

BLUEPRINT §13.1 — `GET /v1/providers/status`, `GET /v1/data/freshness`.

Returns the configured/available state of every external dependency the
signal pipeline talks to, plus the age of the latest stored data. Everything
here is fail-soft: a missing key or a downed Redis must not 500 the endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from atlas_shared.logging import get_logger

log = get_logger("providers")


@dataclass(frozen=True)
class ProviderStatus:
    """One row in the providers list."""

    name: str
    category: str            # "market_data" | "macro" | "news" | "llm" | "alerts" | "infra"
    configured: bool         # required env vars present
    available: bool          # passes a cheap availability check (often == configured)
    fallback: str            # what the system does when this provider is unavailable
    note: str = ""


def _has_env(*keys: str) -> bool:
    return all(bool(os.environ.get(k)) for k in keys)


def _provider_rows() -> list[ProviderStatus]:
    """Synchronous configuration probe. Doesn't open sockets — callers that
    want a live ping should await `provider_health_pings` instead."""
    return [
        ProviderStatus(
            name="alpaca",
            category="market_data",
            configured=_has_env("ALPACA_API_KEY", "ALPACA_API_SECRET"),
            available=_has_env("ALPACA_API_KEY", "ALPACA_API_SECRET"),
            fallback="polygon → yfinance → synthetic GBM",
            note="Free IEX feed when configured. BLUEPRINT §4.4.",
        ),
        ProviderStatus(
            name="polygon",
            category="market_data",
            configured=_has_env("POLYGON_API_KEY"),
            available=_has_env("POLYGON_API_KEY"),
            fallback="alpaca → yfinance → synthetic GBM",
        ),
        ProviderStatus(
            name="yfinance",
            category="market_data",
            configured=True,           # no key required
            available=True,
            fallback="synthetic GBM",
        ),
        ProviderStatus(
            name="synthetic",
            category="market_data",
            configured=True,
            available=True,
            fallback="(none — terminal fallback)",
            note="Deterministic GBM bars for offline dev / tests.",
        ),
        ProviderStatus(
            name="fred",
            category="macro",
            configured=_has_env("FRED_API_KEY"),
            available=_has_env("FRED_API_KEY"),
            fallback="synthetic macro series",
            note="Synthetic stub keeps the regime classifier running offline.",
        ),
        ProviderStatus(
            name="newsapi",
            category="news",
            configured=_has_env("NEWSAPI_KEY"),
            available=_has_env("NEWSAPI_KEY"),
            fallback="RSS feeds + file replay",
        ),
        ProviderStatus(
            name="rss",
            category="news",
            configured=True,
            available=True,
            fallback="(none — free baseline)",
        ),
        ProviderStatus(
            name="deepseek",
            category="llm",
            configured=_has_env("DEEPSEEK_API_KEY"),
            available=_has_env("DEEPSEEK_API_KEY"),
            fallback="templated rationale",
            note="BLUEPRINT §10 — when missing, the pipeline keeps producing structured §10.3 explanations from templates.",
        ),
        ProviderStatus(
            name="telegram",
            category="alerts",
            configured=_has_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
            available=_has_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
            fallback="log channel",
        ),
        ProviderStatus(
            name="webhook",
            category="alerts",
            configured=_has_env("ATLAS_WEBHOOK_URL"),
            available=_has_env("ATLAS_WEBHOOK_URL"),
            fallback="log channel",
        ),
        ProviderStatus(
            name="postgres",
            category="infra",
            configured=_has_env("POSTGRES_DSN") or _has_env("DATABASE_URL"),
            available=_has_env("POSTGRES_DSN") or _has_env("DATABASE_URL"),
            fallback="in-memory watchlist + fallback symbols",
            note="Watchlist + journal + alerts persist here.",
        ),
        ProviderStatus(
            name="redis",
            category="infra",
            configured=_has_env("REDIS_URL"),
            available=_has_env("REDIS_URL"),
            fallback="recompute macro snapshot on the fly",
        ),
    ]


def provider_status_payload() -> dict[str, Any]:
    """Return a serialisable status report grouped by category."""
    rows = _provider_rows()
    by_category: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_category.setdefault(r.category, []).append({
            "name": r.name,
            "configured": r.configured,
            "available": r.available,
            "fallback": r.fallback,
            "note": r.note,
        })
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total": len(rows),
            "available": sum(1 for r in rows if r.available),
            "missing_keys": [r.name for r in rows if not r.configured and r.category != "market_data"],
        },
        "providers": by_category,
        # BLUEPRINT §17 — these are the rules. Surfacing them on the endpoint
        # lets the dashboard explain to the user that missing keys won't crash
        # anything, just degrade specific features.
        "policy": {
            "missing_optional_keys_must_not_crash": True,
            "missing_market_data_falls_back_or_warns": True,
            "missing_deepseek_uses_templated_explanations": True,
            "missing_telegram_keeps_log_alerts_active": True,
        },
    }


# ── Data freshness ────────────────────────────────────────────────────────────


async def _bars_freshness(symbol: str) -> dict[str, Any]:
    """Last-bar age for `symbol`. Returns a dict the API renders verbatim."""
    from ingest_equities.store import latest_bars  # local import — avoids
                                                    # signal-service depending on
                                                    # ingest-equities at import time.
    try:
        bars = await latest_bars(symbol, resolution="1m", limit=1)
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": str(e)[:200]}
    if bars.empty:
        return {"symbol": symbol, "ok": True, "has_bars": False, "age_seconds": None}
    ts = pd.Timestamp(bars["ts"].iloc[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC)
    age = (datetime.now(UTC) - ts.to_pydatetime()).total_seconds()
    return {
        "symbol": symbol,
        "ok": True,
        "has_bars": True,
        "last_bar_ts": ts.isoformat(),
        "age_seconds": round(age, 1),
    }


async def data_freshness_payload(symbols: list[str]) -> dict[str, Any]:
    """Compose `/v1/data/freshness`: per-symbol bar age + macro snapshot age."""
    from macro_engine.refresh import load_snapshot  # local — same reason as above.

    bars_results: list[dict[str, Any]] = []
    for sym in symbols:
        bars_results.append(await _bars_freshness(sym))

    macro_age: float | None = None
    macro_ts: str | None = None
    try:
        snapshot = await load_snapshot()
    except Exception:
        snapshot = None
    if snapshot and snapshot.get("ts"):
        try:
            macro_ts = str(snapshot["ts"])
            ts = pd.Timestamp(macro_ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize(UTC)
            macro_age = round((datetime.now(UTC) - ts.to_pydatetime()).total_seconds(), 1)
        except Exception:
            macro_age = None

    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "macro": {
            "snapshot_ts": macro_ts,
            "age_seconds": macro_age,
            "regime": (snapshot or {}).get("regime", "unknown"),
        },
        "symbols": bars_results,
    }
