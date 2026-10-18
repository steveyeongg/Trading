"""Swing-engine persistence: the ``assets`` table and daily bars in the ``bars`` hypertable.

Daily bars are rows of ``bars`` with ``resolution='1d'`` and ``ts`` the session date at
00:00 UTC (the storage convention shared with :mod:`swing_engine.data.alpaca`). Every read
here filters on that resolution, so minute bars from the legacy pipeline never leak in.

Writing is built for a full-market backfill, millions of rows, so rows do not go through
``executemany``. Each batch is streamed with the binary ``COPY`` protocol into a staging
table and merged with a single ``INSERT ... SELECT ... ON CONFLICT DO UPDATE``. The
staging table is created once per call and lives only as long as the transaction. A call
is atomic (one transaction), and replaying it is harmless because the merge is an upsert.

Missing values reach the database as NULL, never NaN. Postgres orders NaN above every
number, so a stored NaN would win ``max(close * volume)`` and slip past the liquidity
prefilter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import Any

import numpy as np
import pandas as pd
from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from sqlalchemy import text

from swing_engine.data.alpaca import BAR_COLUMNS

log = get_logger("swing.store")

BATCH_ROWS = 50_000
DAILY = "1d"

_STAGING_TABLE = "_bars_in"
_PRICE_COLUMNS = ["open", "high", "low", "close", "volume", "vwap"]
_REQUIRED_COLUMNS = ["ts", "symbol", "open", "high", "low", "close", "volume"]
_ASSET_COLUMNS = [
    "symbol",
    "name",
    "exchange",
    "status",
    "kind",
    "tradable",
    "first_bar",
    "last_bar",
    "updated_at",
]
_BAR_SELECT = ", ".join(BAR_COLUMNS)

# --- SQL -------------------------------------------------------------------------------

_UPSERT_ASSETS_SQL = text(
    """
    INSERT INTO assets (symbol, name, exchange, status, kind, tradable)
    VALUES (:symbol, :name, :exchange, :status, :kind, :tradable)
    ON CONFLICT (symbol) DO UPDATE SET
        name = EXCLUDED.name,
        exchange = EXCLUDED.exchange,
        status = EXCLUDED.status,
        kind = EXCLUDED.kind,
        tradable = EXCLUDED.tradable,
        updated_at = now()
    """
)

_LOAD_ASSETS_SQL = text(f"SELECT {', '.join(_ASSET_COLUMNS)} FROM assets")

# Only assets that have daily bars are touched, so an asset keeps its last known range.
_REFRESH_RANGES_SQL = text(
    """
    WITH ranges AS (
        SELECT symbol,
               CAST(min(ts) AT TIME ZONE 'UTC' AS date) AS first_bar,
               CAST(max(ts) AT TIME ZONE 'UTC' AS date) AS last_bar
        FROM bars
        WHERE resolution = '1d'
        GROUP BY symbol
    )
    UPDATE assets AS a
    SET first_bar = ranges.first_bar, last_bar = ranges.last_bar
    FROM ranges
    WHERE a.symbol = ranges.symbol
    """
)

# LIKE copies the column types and NOT NULLs but not the primary key, so staging a
# duplicate key never fails here. _prepare_bars removes duplicates before the merge,
# which ON CONFLICT DO UPDATE would otherwise refuse ("cannot affect row a second time").
_CREATE_STAGING_SQL = text(
    f"CREATE TEMP TABLE {_STAGING_TABLE} (LIKE bars INCLUDING DEFAULTS) ON COMMIT DROP"
)
_CLEAR_STAGING_SQL = text(f"TRUNCATE {_STAGING_TABLE}")
_MERGE_BARS_SQL = text(
    f"""
    INSERT INTO bars (ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count)
    SELECT ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count
    FROM {_STAGING_TABLE}
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

_LAST_BAR_DATES_SQL = text(
    """
    SELECT symbol, CAST(max(ts) AT TIME ZONE 'UTC' AS date) AS last_bar
    FROM bars
    WHERE resolution = '1d'
    GROUP BY symbol
    """
)
_STORED_CLOSE_SQL = text(
    "SELECT close FROM bars WHERE symbol = :symbol AND resolution = '1d' AND ts = :ts"
)
_LOAD_BARS_SQL = text(
    f"""
    SELECT {_BAR_SELECT}
    FROM bars
    WHERE resolution = '1d' AND symbol = ANY(:symbols) AND ts >= :start AND ts <= :end
    """
)
_LOAD_ALL_BARS_SQL = text(
    f"""
    SELECT {_BAR_SELECT}
    FROM bars
    WHERE resolution = '1d' AND ts >= :start AND ts <= :end
    """
)
_LIQUID_SQL = text(
    """
    SELECT symbol
    FROM bars
    WHERE resolution = '1d'
    GROUP BY symbol
    HAVING max(close * volume) >= :min_dollar_volume
    """
)

# --- assets ----------------------------------------------------------------------------


def _asset_params(row: dict[str, Any]) -> dict[str, Any]:
    tradable = row.get("tradable")
    return {
        "symbol": row["symbol"],
        "name": row.get("name"),
        "exchange": row.get("exchange"),
        "status": str(row["status"]),
        "kind": str(row["kind"]),  # an AssetKind is a StrEnum; bind its plain value
        "tradable": None if tradable is None else bool(tradable),
    }


async def upsert_assets(rows: Sequence[dict[str, Any]]) -> int:
    """Insert or update assets by symbol; returns the number of rows written.

    Rows carry ``symbol, name, exchange, status, kind, tradable``; only ``symbol``,
    ``status`` and ``kind`` are required. ``first_bar``/``last_bar`` are left alone (see
    :func:`refresh_asset_bar_ranges`).
    """
    if not rows:
        return 0
    params = [_asset_params(row) for row in rows]
    async with session_scope() as session:
        await session.execute(_UPSERT_ASSETS_SQL, params)
    log.info("swing.store.assets_upserted", n=len(params))
    return len(params)


async def load_assets() -> pd.DataFrame:
    """Every asset, sorted by symbol. ``first_bar``/``last_bar`` are ``date`` or None."""
    async with session_scope() as session:
        rows = (await session.execute(_LOAD_ASSETS_SQL)).all()
    df = pd.DataFrame(rows, columns=_ASSET_COLUMNS)
    return df.sort_values("symbol", kind="stable").reset_index(drop=True)


async def refresh_asset_bar_ranges() -> None:
    """Set ``first_bar``/``last_bar`` on each asset from its stored daily bars."""
    async with session_scope() as session:
        await session.execute(_REFRESH_RANGES_SQL)


# --- writing daily bars ----------------------------------------------------------------


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    """``df[column]`` as numbers, or all-missing when the column is absent."""
    if column in df.columns:
        return pd.to_numeric(df[column])
    return pd.Series(np.nan, index=df.index, dtype="float64")


def _prepare_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalise a daily-bars frame into the shape that gets copied.

    ``resolution`` may be absent but must otherwise be ``'1d'``; ``vwap`` and
    ``trade_count`` may be absent (NULL). ``trade_count`` becomes a nullable integer, so
    NaN stands for NULL and a fractional count is an error. The result is sorted by
    symbol and time (better index locality on insert) and has one row per key, the last
    one winning.
    """
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"daily bars are missing columns {missing}")
    df = df.reset_index(drop=True)
    if "resolution" in df.columns:
        others = sorted({str(r) for r in df["resolution"].unique() if r != DAILY})
        if others:
            raise ValueError(f"only '1d' bars are stored, got resolution {others}")
    if df["symbol"].isna().any():
        raise ValueError("daily bars have a missing symbol")
    ts = pd.to_datetime(df["ts"])
    if ts.dt.tz is None:
        raise ValueError("ts must be timezone-aware (the session date at 00:00 UTC)")
    ts = ts.dt.tz_convert("UTC")
    if (ts != ts.dt.normalize()).any():
        raise ValueError("ts must be the session date at 00:00 UTC")

    out = pd.DataFrame(
        {
            "ts": ts,
            "symbol": df["symbol"].astype(str),
            "resolution": DAILY,
            **{c: _numeric(df, c).astype("float64") for c in _PRICE_COLUMNS},
            "trade_count": _numeric(df, "trade_count").astype("Int64"),
        }
    )[BAR_COLUMNS]
    out = out.sort_values(["symbol", "ts"], kind="stable")
    deduped = out.drop_duplicates(["symbol", "ts"], keep="last")
    if len(deduped) < len(out):
        log.warning("swing.store.duplicate_bars_dropped", n=len(out) - len(deduped))
    return deduped.reset_index(drop=True)


def _python_values(series: pd.Series) -> list[Any]:
    """Native Python scalars (asyncpg's encoders are strict), with NaN/NA as None."""
    values: list[Any] = series.astype(object).where(series.notna(), None).tolist()
    return values


def _copy_records(batch: pd.DataFrame) -> list[tuple[Any, ...]]:
    """Rows of a prepared batch as tuples in ``BAR_COLUMNS`` order, for binary COPY."""
    values: dict[str, list[Any]] = {
        "ts": pd.DatetimeIndex(batch["ts"]).to_pydatetime().tolist(),
        "symbol": batch["symbol"].tolist(),
        "resolution": batch["resolution"].tolist(),
        **{c: _python_values(batch[c]) for c in [*_PRICE_COLUMNS, "trade_count"]},
    }
    return list(zip(*(values[c] for c in BAR_COLUMNS), strict=True))


async def upsert_daily_bars(df: pd.DataFrame) -> int:
    """Insert or update daily bars; returns the number of distinct bars written.

    ``df`` has the columns of ``AlpacaData.daily_bars``. ``resolution`` must be ``'1d'``
    (or absent) and ``ts`` the timezone-aware session date at 00:00 UTC; anything else
    raises ``ValueError`` before the database is touched.
    """
    if df.empty:
        return 0
    bars = _prepare_bars(df)
    async with session_scope() as session:
        await session.execute(_CREATE_STAGING_SQL)
        # COPY is a driver feature, so reach the asyncpg connection under this session.
        # It is the connection (and transaction) the session's own statements use.
        raw = await (await session.connection()).get_raw_connection()
        driver = raw.driver_connection
        for offset in range(0, len(bars), BATCH_ROWS):
            batch = bars.iloc[offset : offset + BATCH_ROWS]
            await driver.copy_records_to_table(
                _STAGING_TABLE, records=_copy_records(batch), columns=BAR_COLUMNS
            )
            await session.execute(_MERGE_BARS_SQL)
            await session.execute(_CLEAR_STAGING_SQL)
    log.info("swing.store.bars_upserted", n=len(bars), symbols=bars["symbol"].nunique())
    return len(bars)


# --- reading daily bars ----------------------------------------------------------------


def _utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _bars_frame(rows: Sequence[Any]) -> pd.DataFrame:
    """Database rows to the ``AlpacaData.daily_bars`` shape, sorted by symbol then time.

    ``trade_count`` is int64, or float64 with NaN when any bar has none (as in
    ``daily_bars``); NULL prices and volumes are NaN.
    """
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df[_PRICE_COLUMNS] = df[_PRICE_COLUMNS].astype("float64")
    df["trade_count"] = pd.to_numeric(df["trade_count"])
    return df.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)


async def load_daily_bars(symbols: Sequence[str] | None, start: date, end: date) -> pd.DataFrame:
    """Daily bars for ``symbols`` (all symbols when None) from ``start`` to ``end`` inclusive.

    Both dates are session dates. The frame has the columns of ``AlpacaData.daily_bars``,
    sorted by ``symbol, ts`` with ``ts`` timezone-aware UTC, and is empty (same columns)
    when nothing matches. An empty ``symbols`` list loads nothing, not everything.
    """
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of tickers, not a single string")
    params: dict[str, Any] = {"start": _utc_midnight(start), "end": _utc_midnight(end)}
    if symbols is None:
        statement = _LOAD_ALL_BARS_SQL
    elif not symbols:
        return _bars_frame([])
    else:
        statement = _LOAD_BARS_SQL
        params["symbols"] = list(dict.fromkeys(symbols))
    async with session_scope() as session:
        rows = (await session.execute(statement, params)).all()
    return _bars_frame(rows)


async def last_bar_dates() -> dict[str, date]:
    """The session date of each symbol's newest stored daily bar."""
    async with session_scope() as session:
        rows = (await session.execute(_LAST_BAR_DATES_SQL)).all()
    return {row[0]: row[1] for row in rows}


async def stored_close(symbol: str, day: date) -> float | None:
    """The stored close of ``symbol`` on session ``day``, or None when there is none."""
    async with session_scope() as session:
        rows = (
            await session.execute(_STORED_CLOSE_SQL, {"symbol": symbol, "ts": _utc_midnight(day)})
        ).all()
    if not rows or rows[0][0] is None:
        return None
    return float(rows[0][0])


async def liquid_prefilter(min_dollar_volume: float) -> list[str]:
    """Symbols whose best day ever has ``close * volume >= min_dollar_volume``, sorted.

    That is a necessary condition for a rolling median of dollar volume to reach the
    threshold, so it is a cheap, safe way to shrink the universe before loading bars. It
    never decides tradability on its own.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(_LIQUID_SQL, {"min_dollar_volume": float(min_dollar_volume)})
        ).all()
    return sorted(row[0] for row in rows)
