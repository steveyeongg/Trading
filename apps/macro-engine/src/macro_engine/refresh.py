"""CLI: refresh macro series and write a regime snapshot to Redis.

Run on a schedule (cron / Airflow). The signal-service reads the latest
snapshot from Redis on every request — never blocks on FRED.

Usage:
    uv run python -m macro_engine.refresh --days 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import redis.asyncio as redis_async
from atlas_shared.config import get_settings
from atlas_shared.logging import get_logger, setup_logging

from macro_engine.fred import SERIES, FredClient
from macro_engine.regime import RegimeClassifier, _features

setup_logging()
log = get_logger("macro.refresh")

REDIS_KEY = "atlas:macro:snapshot"
TTL_SECONDS = 60 * 60 * 6   # 6h — refresh job re-runs every hour in prod


async def _refresh(days: int) -> dict:
    settings = get_settings()
    series: dict = {}
    async with FredClient() as fred:
        if not fred.available:
            log.warning("macro.refresh.no_key", note="emitting synthetic snapshot")
        for sid in SERIES.values():
            series[sid] = await fred.series(sid, days=days)

    clf = RegimeClassifier().fit(series)
    out = clf.classify(series)

    feats = _features(series)
    snapshot: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "regime": out.label,
        "regime_confidence": out.confidence,
        "regime_probabilities": out.probabilities,
    }
    if not feats.empty:
        latest = feats.iloc[-1]
        for k in ("term_spread", "vix", "vix_z", "dxy_z", "curve_slope", "cpi_yoy"):
            v = latest.get(k)
            if v is not None and v == v:  # not NaN
                snapshot[k] = float(v)

    client = redis_async.from_url(settings.redis_url)
    try:
        await client.set(REDIS_KEY, json.dumps(snapshot, default=str), ex=TTL_SECONDS)
        log.info("macro.refresh.cached", key=REDIS_KEY)
    except Exception as e:
        # Redis down → still return the computed snapshot. The signal-service
        # will fall back to live recompute or `unknown` regime.
        log.warning("macro.refresh.cache_failed", error=str(e))
    finally:
        await client.aclose()

    log.info("macro.refresh.done", regime=out.label, confidence=round(out.confidence, 2))
    return snapshot


async def load_snapshot() -> dict | None:
    """Read the most recent regime snapshot. None if Redis unreachable or empty."""
    settings = get_settings()
    client = redis_async.from_url(settings.redis_url)
    try:
        raw = await client.get(REDIS_KEY)
    except Exception as e:
        log.warning("macro.snapshot.unavailable", error=str(e))
        return None
    finally:
        await client.aclose()
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> None:
    from atlas_shared import load_env

    load_env()  # so FredClient (reads os.environ) sees FRED_API_KEY from .env
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=900)
    args = p.parse_args()
    snapshot = asyncio.run(_refresh(args.days))
    print(json.dumps(snapshot, indent=2, default=str))


if __name__ == "__main__":
    main()
