"""Bar persistence — async upserts into the `bars` hypertable.

Read path supports on-the-fly aggregation: callers ask for `1m` (raw rows) or
`5m`/`15m`/`1h`/`4h`/`1d` (TimescaleDB `time_bucket()` aggregation off the same
`resolution='1m'` storage). We deliberately store one resolution and aggregate
at read time — keeps ingest simple, lets users zoom out without re-ingesting,
and is fast on a hypertable. Continuous-aggregate materialised views are a
future optimisation (BLUEPRINT §6.2) for when 30-day reads start hurting.
"""

from __future__ import annotations

import pandas as pd
from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from sqlalchemy import text

log = get_logger("ingest.store")

# ON CONFLICT lets us replay backfills idempotently. The natural key is
# (symbol, resolution, ts); see migrations/0001_initial.sql.
_UPSERT_SQL = text(
    """
    INSERT INTO bars (ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count)
    VALUES (:ts, :symbol, :resolution, :open, :high, :low, :close, :volume, :vwap, :trade_count)
    ON CONFLICT (symbol, resolution, ts) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        vwap = EXCLUDED.vwap,
        trade_count = EXCLUDED.trade_count
    """
)


# Columns the upsert binds. Sources that omit optional ones (vwap, trade_count
# in the synthetic generator) get NULLs rather than a bind-parameter error.
_BAR_FIELDS = ("ts", "symbol", "resolution", "open", "high", "low", "close", "volume", "vwap", "trade_count")


# UI-friendly resolution → bucket size in seconds. Used as a divisor on the
# row's epoch to floor-bucket it. Works on vanilla Postgres AND TimescaleDB
# — no extension-specific aggregates required.
_BUCKETS: dict[str, int] = {
    "1m":  60,          # passthrough, no aggregation
    "5m":  5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h":  60 * 60,
    "4h":  4 * 60 * 60,
    "1d":  24 * 60 * 60,
    "1w":  7 * 24 * 60 * 60,
}


def is_supported_resolution(resolution: str) -> bool:
    return resolution in _BUCKETS


# Raw read path — used when `resolution='1m'` matches what we ingest.
_RAW_SQL = text(
    """
    SELECT ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count
    FROM bars
    WHERE symbol = :symbol AND resolution = :resolution
    ORDER BY ts DESC
    LIMIT :limit
    """
)

# Aggregated read path — portable epoch-floor bucketing. Works on vanilla
# Postgres and TimescaleDB without depending on the extension's first()/last()
# aggregates (which require the extension to be enabled per-database and have
# bitten us in the past).
#
# Bucketing trick:
#   to_timestamp(floor(extract(epoch from ts) / :bucket_seconds) * :bucket_seconds)
# floor-buckets ts to the nearest :bucket_seconds boundary. open/close are
# selected via ordered array_agg — `(array_agg(open ORDER BY ts ASC))[1]`
# returns the open of the earliest minute in the bucket; mirror for close.
# This is the standard portable equivalent of Timescale's `first()`/`last()`.
_AGG_SQL = text(
    """
    WITH bucketed AS (
        SELECT
            to_timestamp(floor(extract(epoch FROM ts) / :bucket_seconds)
                         * :bucket_seconds) AT TIME ZONE 'UTC'    AS bucket_ts,
            ts, open, high, low, close, volume, vwap, trade_count
        FROM bars
        WHERE symbol = :symbol AND resolution = '1m'
    )
    SELECT
        bucket_ts                                                AS ts,
        :symbol                                                  AS symbol,
        :resolution                                              AS resolution,
        (array_agg(open  ORDER BY ts ASC))[1]                    AS open,
        max(high)                                                AS high,
        min(low)                                                 AS low,
        (array_agg(close ORDER BY ts DESC))[1]                   AS close,
        sum(volume)                                              AS volume,
        CASE
            WHEN sum(volume) > 0
            THEN sum(COALESCE(vwap, close) * volume) / sum(volume)
            ELSE avg(close)
        END                                                      AS vwap,
        sum(trade_count)                                         AS trade_count
    FROM bucketed
    GROUP BY bucket_ts
    ORDER BY bucket_ts DESC
    LIMIT :limit
    """
)


async def upsert_bars(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    # Backfill any missing optional columns with NULL so SQLAlchemy can bind
    # every named parameter regardless of source (synthetic vs. Polygon).
    rows = [
        {field: row.get(field) for field in _BAR_FIELDS}
        for row in df.to_dict(orient="records")
    ]
    async with session_scope() as s:
        await s.execute(_UPSERT_SQL, rows)
    log.info("ingest.upsert", n=len(rows), symbol=df["symbol"].iloc[0])
    return len(rows)


async def latest_bars(symbol: str, resolution: str = "1m", limit: int = 500) -> pd.DataFrame:
    """Return the most recent `limit` bars for `symbol`, ascending by `ts`.

    For `resolution='1m'` we read the raw hypertable rows. For any other
    supported resolution we aggregate the 1m bars on the fly with
    `time_bucket()`. Unknown resolutions raise — the route layer is expected
    to validate via `is_supported_resolution()` first.
    """
    if resolution not in _BUCKETS:
        raise ValueError(f"unsupported resolution: {resolution!r}; expected one of {sorted(_BUCKETS)}")

    sql, params = (
        (_RAW_SQL, {"symbol": symbol, "resolution": "1m", "limit": limit})
        if resolution == "1m"
        else (
            _AGG_SQL,
            {
                "symbol": symbol,
                "resolution": resolution,
                "bucket": _BUCKETS[resolution],
                "limit": limit,
            },
        )
    )
    async with session_scope() as s:
        result = await s.execute(sql, params)
        rows = result.mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return df
