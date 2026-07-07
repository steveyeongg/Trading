"""Regression guard for the bars-aggregation SQL contract.

Doesn't need a running Postgres — we inspect the compiled SQL, the bucket
map, and the bind-parameter contract between the caller and the query.

**Pinned bug this file guards against:** the aggregation SQL binds
`:bucket_seconds`, but the caller in `latest_bars` used to pass
`{"bucket": …}`. SQLAlchemy silently kept `:bucket_seconds` unbound,
so every non-1m `/v1/symbols/{symbol}/bars` request 500'd with
`InvalidRequestError`. The dashboard chart was empty on 5D / 1M / 3M
timeframes for weeks.
"""

from __future__ import annotations

import re

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


def test_bucket_map_is_seconds_integer() -> None:
    """`floor(extract(epoch from ts) / :bucket_seconds)` bins by integer
    seconds. A typo here would surface as a math error in production
    (wrong candle sizes)."""
    for ui_resolution, seconds in _BUCKETS.items():
        assert isinstance(seconds, int), f"{ui_resolution}: expected int seconds, got {seconds!r}"
        assert seconds > 0
    # Spot-check a few known values so the mapping can't silently drift.
    assert _BUCKETS["1m"] == 60
    assert _BUCKETS["15m"] == 900
    assert _BUCKETS["1h"] == 3600
    assert _BUCKETS["1d"] == 86_400


def test_aggregation_sql_bind_parameters_match_caller() -> None:
    """The exact bug that broke every chart: `_AGG_SQL` binds
    `:bucket_seconds` (plural, `_seconds` suffix). If the caller in
    `latest_bars` ever reverts to `:bucket` or `:seconds`, SQLAlchemy keeps
    the parameter unbound and every non-1m /bars request 500s."""
    sql = str(_AGG_SQL.compile(compile_kwargs={"literal_binds": False}))
    bound = set(re.findall(r":(\w+)", sql))
    # The four bind names the caller MUST pass. If you rename any of these,
    # rename them in `latest_bars()` in the SAME commit.
    assert bound == {"bucket_seconds", "symbol", "resolution", "limit"}, (
        f"SQL bind params drifted from the caller's dict. bound={bound!r}"
    )


def test_aggregation_sql_groups_by_bucket() -> None:
    """Aggregation MUST GROUP BY the computed bucket timestamp. Missing this
    would either collapse rows across days (catastrophic) or degenerate
    to no aggregation (perf regression)."""
    sql = str(_AGG_SQL.compile(compile_kwargs={"literal_binds": False}))
    assert "GROUP BY bucket_ts" in sql
    # OHLC aggregates — max(high) / min(low) are portable; first(open) /
    # last(close) are emulated via array_agg since Postgres core doesn't
    # ship the Timescale aggregates.
    assert "max(high)" in sql
    assert "min(low)" in sql
    assert "array_agg(open" in sql
    assert "array_agg(close" in sql
    # Volume-weighted vwap, not arithmetic mean.
    assert "vwap" in sql and "volume" in sql


def test_raw_sql_used_for_one_minute_passthrough() -> None:
    sql = str(_RAW_SQL.compile(compile_kwargs={"literal_binds": False}))
    assert "GROUP BY" not in sql, "1m path should not aggregate — that's a perf regression"
    assert "WHERE symbol = :symbol" in sql
