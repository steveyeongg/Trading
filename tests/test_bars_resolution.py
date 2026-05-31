"""Regression guard for the bars-aggregation SQL contract.

Doesn't need a running Postgres — we inspect the compiled SQL + the bucket
map. The SQL itself runs against TimescaleDB's `time_bucket()`, `first()`,
and `last()` aggregates; those are exercised by the existing integration
smoke tests when the DB is up.
"""

from __future__ import annotations

import pytest

from ingest_equities.store import (
    _AGG_SQL,
    _BUCKETS,
    _RAW_SQL,
    is_supported_resolution,
    latest_bars,
)


def test_supported_resolutions_include_all_ui_choices() -> None:
    """If the frontend offers a timeframe, the backend must accept its
    resolution. This is the cross-stack contract."""
    for ui_resolution in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
        assert is_supported_resolution(ui_resolution), f"backend rejects {ui_resolution}"


def test_unknown_resolution_rejected() -> None:
    assert not is_supported_resolution("1y")
    assert not is_supported_resolution("3m")  # 3-minute is intentionally not exposed
    assert not is_supported_resolution("")


@pytest.mark.asyncio
async def test_latest_bars_raises_on_unsupported_resolution() -> None:
    """Defensive: even if a caller bypasses the route validation, the store
    layer fails fast rather than silently returning empty."""
    with pytest.raises(ValueError, match="unsupported resolution"):
        await latest_bars("AAPL", resolution="bogus")


def test_bucket_map_uses_postgres_interval_literals() -> None:
    """`time_bucket()` requires a valid Postgres interval literal. A typo
    here would surface as a SQL error in production rather than a code
    review comment."""
    for ui_resolution, pg_interval in _BUCKETS.items():
        # Every value must be parseable as a Postgres interval — two words
        # ("N <unit>") or "1 week" / "1 day" style.
        assert pg_interval.count(" ") == 1, f"bad interval literal for {ui_resolution}: {pg_interval!r}"
        n, unit = pg_interval.split(" ")
        assert n.isdigit()
        assert unit in {"minute", "minutes", "hour", "hours", "day", "days", "week", "weeks"}


def test_aggregation_sql_groups_by_bucket_and_symbol() -> None:
    """The aggregation SQL must group by `time_bucket()` and `symbol` — if
    the GROUP BY drifts, candles will collapse across symbols (catastrophic)
    or per-row (no aggregation, defeats the point)."""
    sql = str(_AGG_SQL.compile(compile_kwargs={"literal_binds": False}))
    assert "time_bucket" in sql
    assert "GROUP BY 1, 2" in sql
    # OHLC must use the right aggregates so candles are correct.
    assert "first(open" in sql
    assert "last(close" in sql
    assert "max(high)" in sql
    assert "min(low)" in sql
    # Volume-weighted vwap, not arithmetic mean.
    assert "vwap" in sql and "volume" in sql


def test_raw_sql_used_for_one_minute_passthrough() -> None:
    sql = str(_RAW_SQL.compile(compile_kwargs={"literal_binds": False}))
    assert "time_bucket" not in sql, "1m path should not aggregate — that's a perf regression"
    assert "WHERE symbol = :symbol" in sql
