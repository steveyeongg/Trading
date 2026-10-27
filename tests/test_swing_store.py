"""Swing engine data store: migration 0009, the COPY-based bar upsert, and the reads.

The contract tests, the input validation and the batching/COPY choreography (against a
fake session) run everywhere. The round trips against a real database are skipped unless
something answers on 127.0.0.1:5432; they need migration 0009 applied and clean up after
themselves (only the ``ZZ*`` symbols below are ever touched).
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from swing_engine import store
from swing_engine.data.alpaca import BAR_COLUMNS, AlpacaData
from swing_engine.store import (
    _MERGE_BARS_SQL,
    _UPSERT_ASSETS_SQL,
    last_bar_dates,
    liquid_prefilter,
    load_assets,
    load_daily_bars,
    load_ohlcv,
    load_raw_closes,
    refresh_asset_bar_ranges,
    stored_close,
    upsert_assets,
    upsert_daily_bars,
)
from swing_engine.testing import make_bars, postgres_reachable

MIGRATIONS = Path(__file__).resolve().parents[1] / "packages" / "shared-py" / "migrations"
needs_db = pytest.mark.skipif(not postgres_reachable(), reason="needs local Postgres")


def _bars(symbol: str = "ZZTEST", closes: tuple[float, ...] = (10, 11, 12), **extra: Any):
    return make_bars(symbol, list(closes)).assign(resolution="1d", **extra)


# --- contract tests: the SQL text and the migration -----------------------------------


def test_merge_sql_upserts_on_natural_key():
    assert "ON CONFLICT (symbol, resolution, ts) DO UPDATE" in _MERGE_BARS_SQL.text


def test_merge_sql_writes_and_refreshes_every_non_key_column():
    sql = _MERGE_BARS_SQL.text
    assert "FROM _bars_in" in sql
    for column in BAR_COLUMNS:
        assert column in sql.split("ON CONFLICT")[0]
        if column not in ("symbol", "resolution", "ts"):
            assert f"{column} = EXCLUDED.{column}" in sql


def test_upsert_assets_sql_keys_on_symbol_and_stamps_updated_at():
    sql = _UPSERT_ASSETS_SQL.text
    assert "ON CONFLICT (symbol) DO UPDATE" in sql and "updated_at = now()" in sql


def test_migration_0009_has_no_numeric_columns():
    sql = (MIGRATIONS / "0009_swing_assets.sql").read_text()
    assert "NUMERIC" not in sql.upper() and "CREATE TABLE IF NOT EXISTS assets" in sql


def test_migration_0009_defines_assets_columns_and_bars_index():
    sql = " ".join((MIGRATIONS / "0009_swing_assets.sql").read_text().split())
    for fragment in (
        "symbol TEXT PRIMARY KEY",
        "status TEXT NOT NULL",
        "kind TEXT NOT NULL",
        "tradable BOOLEAN",
        "first_bar DATE",
        "last_bar DATE",
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "CREATE INDEX IF NOT EXISTS bars_resolution_ts_idx ON bars (resolution, ts)",
    ):
        assert fragment in sql


def test_migration_0009_has_no_sqlalchemy_bind_parameters():
    # The runner executes each file through text(), where ":word" would be read as a bind.
    sql = (MIGRATIONS / "0009_swing_assets.sql").read_text()
    assert text(sql).compile().params == {}


@pytest.mark.parametrize(
    "statement",
    [
        store._LAST_BAR_DATES_SQL,
        store._STORED_CLOSE_SQL,
        store._LOAD_BARS_SQL,
        store._LOAD_ALL_BARS_SQL,
        store._LIQUID_SQL,
        store._REFRESH_RANGES_SQL,
    ],
)
def test_bar_reads_only_look_at_daily_resolution(statement):
    assert "resolution = '1d'" in statement.text


def test_store_exposes_the_async_interface_backfill_and_scan_rely_on():
    for name in (
        "upsert_assets",
        "load_assets",
        "refresh_asset_bar_ranges",
        "upsert_daily_bars",
        "last_bar_dates",
        "stored_close",
        "load_daily_bars",
        "load_ohlcv",
        "load_raw_closes",
        "liquid_prefilter",
    ):
        assert inspect.iscoroutinefunction(getattr(store, name)), name


# --- input validation and normalisation (no database) ---------------------------------


def test_prepare_rejects_non_daily_resolution():
    df = _bars().assign(resolution=["1d", "1m", "1d"])
    with pytest.raises(ValueError, match="1m"):
        store._prepare_bars(df)


def test_prepare_treats_a_missing_resolution_column_as_daily():
    out = store._prepare_bars(make_bars("ZZTEST", [10, 11, 12]))
    assert set(out["resolution"]) == {"1d"}


def test_prepare_requires_timezone_aware_timestamps():
    df = _bars()
    df["ts"] = df["ts"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        store._prepare_bars(df)


def test_prepare_requires_session_date_at_midnight_utc():
    df = _bars()
    df["ts"] = df["ts"] + pd.Timedelta(hours=4)  # Alpaca's raw midnight New York
    with pytest.raises(ValueError, match="00:00 UTC"):
        store._prepare_bars(df)


def test_prepare_reports_missing_required_columns():
    with pytest.raises(ValueError, match="close"):
        store._prepare_bars(_bars().drop(columns=["close"]))


def test_prepare_rejects_a_missing_symbol():
    df = _bars().assign(symbol=["ZZTEST", None, "ZZTEST"])
    with pytest.raises(ValueError, match="symbol"):
        store._prepare_bars(df)


def test_prepare_converts_other_timezones_to_utc_midnight():
    df = _bars()
    df["ts"] = df["ts"].dt.tz_convert("Asia/Tokyo")  # 09:00 JST is still 00:00 UTC
    out = store._prepare_bars(df)
    assert str(out["ts"].dt.tz) == "UTC" and list(out["ts"]) == list(_bars()["ts"])


def test_prepare_drops_duplicate_keys_keeping_the_last_row():
    # ON CONFLICT DO UPDATE refuses to touch one row twice in a single statement.
    df = pd.concat([_bars(closes=(10, 11, 12)), _bars(closes=(20, 21, 22)).iloc[:1]])
    out = store._prepare_bars(df)
    assert len(out) == 3 and out["close"].tolist() == [20.0, 11.0, 12.0]


def test_prepare_sorts_by_symbol_then_time():
    df = pd.concat([_bars("ZZB"), _bars("ZZA")]).iloc[::-1]
    out = store._prepare_bars(df)
    assert list(out["symbol"]) == ["ZZA"] * 3 + ["ZZB"] * 3
    assert out.groupby("symbol")["ts"].apply(lambda s: s.is_monotonic_increasing).all()


def test_copy_records_are_python_scalars_in_column_order():
    df = _bars(vwap=[10.5, np.nan, 12.5], trade_count=[100.0, np.nan, 300.0])
    records = store._copy_records(store._prepare_bars(df))
    assert len(records) == 3 and all(len(r) == len(BAR_COLUMNS) for r in records)
    first, second, _ = records
    ts, symbol, resolution, open_, high, low, close, volume, vwap, trade_count = first
    assert isinstance(ts, datetime) and ts == datetime(2020, 1, 2, tzinfo=UTC)
    assert (symbol, resolution, close) == ("ZZTEST", "1d", 10.0)
    assert type(open_) is float and type(volume) is float and type(vwap) is float
    assert high > open_ > low
    assert type(trade_count) is int and trade_count == 100
    # NaN must reach Postgres as NULL: a stored NaN would win max(close * volume).
    assert second[8] is None and second[9] is None


def test_copy_records_leave_absent_optional_columns_null():
    records = store._copy_records(store._prepare_bars(_bars()))
    assert all(r[8] is None and r[9] is None for r in records)


def test_copy_records_null_out_nan_prices_and_volume():
    df = _bars()
    df.loc[1, ["close", "volume"]] = np.nan
    records = store._copy_records(store._prepare_bars(df))
    assert records[1][6] is None and records[1][7] is None


async def test_store_accepts_what_the_alpaca_client_returns():
    # Alpaca stamps midnight New York and omits "n"/"vw" on thin bars.
    payload = {
        "bars": {
            "AAA": [
                {"t": "2026-10-15T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 9, "vw": 1.5},
                {"t": "2026-10-16T04:00:00Z", "o": 2, "h": 3, "l": 2, "c": 3, "v": 9, "n": 40},
            ]
        }
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    async with AlpacaData(key="k", secret="s", transport=transport, min_interval_s=0) as client:
        fetched = await client.daily_bars(["AAA"], date(2026, 10, 1), date(2026, 10, 16), now=now)
    records = store._copy_records(store._prepare_bars(fetched))
    assert [(r[0], r[1], r[2]) for r in records] == [
        (datetime(2026, 10, 15, tzinfo=UTC), "AAA", "1d"),
        (datetime(2026, 10, 16, tzinfo=UTC), "AAA", "1d"),
    ]
    assert [r[8] for r in records] == [1.5, None] and [r[9] for r in records] == [None, 40]


def test_copy_records_accept_integer_trade_counts():
    records = store._copy_records(store._prepare_bars(_bars(trade_count=[1, 2, 3])))
    assert [r[9] for r in records] == [1, 2, 3] and all(type(r[9]) is int for r in records)


def test_prepare_rejects_fractional_trade_counts():
    with pytest.raises(TypeError):
        store._prepare_bars(_bars(trade_count=[1.5, 2.0, 3.0]))


# --- COPY/MERGE choreography against a fake session -----------------------------------


class _Driver:
    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self.calls = calls

    async def copy_records_to_table(self, table, *, records, columns=None, **_: Any):
        self.calls.append(("copy", table, tuple(columns or ()), list(records)))
        return f"COPY {len(self.calls)}"


class _Raw:
    def __init__(self, driver: _Driver) -> None:
        self.driver_connection = driver


class _Connection:
    def __init__(self, driver: _Driver) -> None:
        self._raw = _Raw(driver)

    async def get_raw_connection(self) -> _Raw:
        return self._raw


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Session:
    def __init__(self, calls: list[tuple[Any, ...]], rows: list[tuple[Any, ...]]) -> None:
        self.calls, self._rows = calls, rows
        self._connection = _Connection(_Driver(calls))

    async def connection(self) -> _Connection:
        self.calls.append(("connection",))
        return self._connection

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.calls.append(("sql", statement, params))
        return _Result(self._rows)


class _FakeDb:
    """Stands in for ``session_scope``; records every call in order."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.sessions = 0
        self._rows = rows or []

    @asynccontextmanager
    async def scope(self):
        self.sessions += 1
        yield _Session(self.calls, self._rows)
        self.calls.append(("commit",))


@pytest.fixture
def fake_db(monkeypatch) -> _FakeDb:
    db = _FakeDb()
    monkeypatch.setattr(store, "session_scope", db.scope)
    return db


def _kinds(db: _FakeDb) -> list[Any]:
    out: list[Any] = []
    for call in db.calls:
        if call[0] == "sql":
            out.append(
                {
                    store._CREATE_STAGING_SQL: "create",
                    store._MERGE_BARS_SQL: "merge",
                    store._CLEAR_STAGING_SQL: "clear",
                }.get(call[1], call[1])
            )
        else:
            out.append(call[0])
    return out


async def test_upsert_copies_then_merges_in_batches_inside_one_session(fake_db, monkeypatch):
    monkeypatch.setattr(store, "BATCH_ROWS", 2)
    df = _bars("ZZTEST", closes=(1, 2, 3, 4, 5))
    assert await upsert_daily_bars(df) == 5
    assert fake_db.sessions == 1
    assert _kinds(fake_db) == [
        "create",
        "connection",
        *(["copy", "merge", "clear"] * 3),
        "commit",
    ]
    copies = [c for c in fake_db.calls if c[0] == "copy"]
    assert [len(c[3]) for c in copies] == [2, 2, 1]
    assert {c[1] for c in copies} == {"_bars_in"} and {c[2] for c in copies} == {tuple(BAR_COLUMNS)}
    assert [r[6] for c in copies for r in c[3]] == [1.0, 2.0, 3.0, 4.0, 5.0]


async def test_upsert_stages_into_a_transaction_scoped_temp_table(fake_db):
    sql = store._CREATE_STAGING_SQL.text
    assert "CREATE TEMP TABLE _bars_in" in sql and "LIKE bars INCLUDING DEFAULTS" in sql
    assert "ON COMMIT DROP" in sql
    await upsert_daily_bars(_bars())
    assert _kinds(fake_db)[0] == "create"


async def test_upsert_of_nothing_does_not_open_a_session(fake_db):
    assert await upsert_daily_bars(_bars().iloc[:0]) == 0
    assert fake_db.sessions == 0


async def test_upsert_of_a_frame_with_no_columns_is_a_no_op(fake_db):
    assert await upsert_daily_bars(pd.DataFrame()) == 0 and fake_db.sessions == 0


async def test_upsert_validates_before_touching_the_database(fake_db):
    with pytest.raises(ValueError):
        await upsert_daily_bars(_bars().assign(resolution="1m"))
    assert fake_db.sessions == 0


async def test_upsert_assets_executes_one_row_per_asset(fake_db):
    rows = [
        {
            "symbol": "AAA",
            "name": "Aaa Inc.",
            "exchange": "NYSE",
            "status": "active",
            "kind": "common",
            "tradable": True,
        },
        {"symbol": "BBB", "status": "inactive", "kind": "etf"},
    ]
    assert await upsert_assets(rows) == 2
    (_, statement, params) = next(c for c in fake_db.calls if c[0] == "sql")
    assert statement is _UPSERT_ASSETS_SQL
    assert params[0] == {
        "symbol": "AAA",
        "name": "Aaa Inc.",
        "exchange": "NYSE",
        "status": "active",
        "kind": "common",
        "tradable": True,
    }
    assert params[1] == {
        "symbol": "BBB",
        "name": None,
        "exchange": None,
        "status": "inactive",
        "kind": "etf",
        "tradable": None,
    }


async def test_upsert_assets_of_nothing_is_a_no_op(fake_db):
    assert await upsert_assets([]) == 0 and fake_db.sessions == 0


async def test_upsert_assets_stringifies_enum_kinds(fake_db):
    from swing_engine.data.assets import AssetKind

    await upsert_assets([{"symbol": "AAA", "status": "active", "kind": AssetKind.COMMON}])
    (_, _, params) = next(c for c in fake_db.calls if c[0] == "sql")
    assert params[0]["kind"] == "common" and type(params[0]["kind"]) is str


async def test_load_daily_bars_for_no_symbols_never_queries(fake_db):
    out = await load_daily_bars([], date(2020, 1, 1), date(2020, 2, 1))
    assert out.empty and list(out.columns) == BAR_COLUMNS and fake_db.sessions == 0


async def test_load_daily_bars_refuses_a_bare_string(fake_db):
    with pytest.raises(TypeError, match="not a single string"):
        await load_daily_bars("AAPL", date(2020, 1, 1), date(2020, 2, 1))


def _db_rows(*specs: tuple[str, str, float | None]) -> list[tuple[Any, ...]]:
    return [
        (datetime.fromisoformat(f"{day}T00:00:00+00:00"), sym, "1d", c, c, c, c, 1e6, None, None)
        for sym, day, c in specs
    ]


async def test_load_daily_bars_shapes_and_sorts_the_result(monkeypatch):
    # Rows come back from Postgres in whatever order and with NULLs for missing values.
    db = _FakeDb(
        _db_rows(("ZZB", "2020-01-03", 2), ("ZZA", "2020-01-03", 5), ("ZZA", "2020-01-02", 4))
    )
    monkeypatch.setattr(store, "session_scope", db.scope)
    out = await load_daily_bars(["ZZA", "ZZB"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(out.columns) == BAR_COLUMNS
    assert list(out["symbol"]) == ["ZZA", "ZZA", "ZZB"] and list(out["close"]) == [4, 5, 2]
    days = ["2020-01-02", "2020-01-03", "2020-01-03"]
    assert list(out["ts"]) == [pd.Timestamp(d, tz="UTC") for d in days]
    assert str(out["ts"].dt.tz) == "UTC"
    assert out["close"].dtype == "float64" and out["vwap"].isna().all()
    (_, statement, params) = next(c for c in db.calls if c[0] == "sql")
    assert statement is store._LOAD_BARS_SQL
    assert params == {
        "symbols": ["ZZA", "ZZB"],
        "start": datetime(2020, 1, 1, tzinfo=UTC),
        "end": datetime(2020, 2, 1, tzinfo=UTC),
    }


async def test_load_daily_bars_without_symbols_reads_every_symbol(monkeypatch):
    db = _FakeDb(_db_rows(("ZZA", "2020-01-02", 4)))
    monkeypatch.setattr(store, "session_scope", db.scope)
    out = await load_daily_bars(None, date(2020, 1, 1), date(2020, 2, 1))
    (_, statement, params) = next(c for c in db.calls if c[0] == "sql")
    assert statement is store._LOAD_ALL_BARS_SQL and "symbols" not in params
    assert len(out) == 1


async def test_load_daily_bars_with_no_rows_returns_the_daily_bars_columns(monkeypatch):
    monkeypatch.setattr(store, "session_scope", _FakeDb([]).scope)
    out = await load_daily_bars(["ZZA"], date(2020, 1, 1), date(2020, 2, 1))
    assert out.empty and list(out.columns) == BAR_COLUMNS
    assert str(out["ts"].dt.tz) == "UTC" and out["close"].dtype == "float64"
    assert out["trade_count"].dtype == "int64"


async def test_load_daily_bars_keeps_trade_count_integral_unless_null(monkeypatch):
    rows = _db_rows(("ZZA", "2020-01-02", 4), ("ZZA", "2020-01-03", 5))
    full = [(*r[:9], 7) for r in rows]
    half = [full[0], (*rows[1][:9], None)]
    monkeypatch.setattr(store, "session_scope", _FakeDb(full).scope)
    out = await load_daily_bars(None, date(2020, 1, 1), date(2020, 2, 1))
    assert out["trade_count"].dtype == "int64"
    monkeypatch.setattr(store, "session_scope", _FakeDb(half).scope)
    out = await load_daily_bars(None, date(2020, 1, 1), date(2020, 2, 1))
    assert out["trade_count"].dtype == "float64" and out["trade_count"].isna().tolist() == [
        False,
        True,
    ]


def _flat(statement: Any) -> str:
    return " ".join(statement.text.split())


def test_the_validation_loads_select_only_the_columns_a_panel_needs():
    """The incident: the validation loader pulled every column, adjusted and raw, for ~5,000
    symbols over ten years through SQLAlchemy rows in one go (an estimated 7-10 GiB peak)."""
    ohlcv, raw = _flat(store._LOAD_OHLCV_SQL), _flat(store._LOAD_RAW_CLOSES_SQL)
    assert ohlcv.startswith("SELECT ts, symbol, open, high, low, close, volume FROM bars WHERE")
    assert "resolution = '1d' AND symbol = ANY(:symbols) AND ts >= :start AND ts <= :end" in ohlcv
    assert raw.startswith("SELECT ts, symbol, close FROM bars WHERE")
    assert "resolution = '1d_raw' AND symbol = ANY(:symbols) AND ts >= :start AND ts <= :end" in raw


async def test_load_ohlcv_and_raw_closes_shape_the_rows(monkeypatch):
    day = datetime(2020, 1, 2, tzinfo=UTC)
    db = _FakeDb([(day, "ZZB", 1, 2, 0.5, 1.5, 100), (day, "ZZA", 3, 4, 2.5, 3.5, None)])
    monkeypatch.setattr(store, "session_scope", db.scope)
    out = await load_ohlcv(["ZZA", "ZZB"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(out.columns) == ["ts", "symbol", "open", "high", "low", "close", "volume"]
    assert list(out["symbol"]) == ["ZZA", "ZZB"] and str(out["ts"].dt.tz) == "UTC"
    assert out["volume"].dtype == "float64" and np.isnan(out["volume"].iloc[0])
    (_, statement, params) = next(c for c in db.calls if c[0] == "sql")
    assert statement is store._LOAD_OHLCV_SQL
    assert params == {
        "symbols": ["ZZA", "ZZB"],
        "start": datetime(2020, 1, 1, tzinfo=UTC),
        "end": datetime(2020, 2, 1, tzinfo=UTC),
    }

    db = _FakeDb([(day, "ZZA", 7.0)])
    monkeypatch.setattr(store, "session_scope", db.scope)
    raw = await load_raw_closes(["ZZA"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(raw.columns) == ["ts", "symbol", "close"] and raw["close"].tolist() == [7.0]
    assert next(c for c in db.calls if c[0] == "sql")[1] is store._LOAD_RAW_CLOSES_SQL


async def test_column_loads_of_no_symbols_never_query(fake_db):
    for load in (load_ohlcv, load_raw_closes):
        out = await load([], date(2020, 1, 1), date(2020, 2, 1))
        assert out.empty and "symbol" in out.columns
    assert fake_db.sessions == 0


async def test_last_bar_dates_maps_symbol_to_session_date(monkeypatch):
    rows = [("AAA", date(2026, 10, 16)), ("BBB", date(2026, 10, 15))]
    monkeypatch.setattr(store, "session_scope", _FakeDb(rows).scope)
    assert await last_bar_dates() == {"AAA": date(2026, 10, 16), "BBB": date(2026, 10, 15)}


async def test_liquid_prefilter_returns_sorted_symbols(monkeypatch):
    db = _FakeDb([("BBB",), ("AAA",)])
    monkeypatch.setattr(store, "session_scope", db.scope)
    assert await liquid_prefilter(20_000_000) == ["AAA", "BBB"]
    (_, statement, params) = next(c for c in db.calls if c[0] == "sql")
    assert statement is store._LIQUID_SQL and params == {"min_dollar_volume": 20_000_000.0}


async def test_stored_close_is_none_without_a_bar_and_for_a_null_close(monkeypatch):
    monkeypatch.setattr(store, "session_scope", _FakeDb([]).scope)
    assert await stored_close("AAA", date(2026, 10, 16)) is None
    monkeypatch.setattr(store, "session_scope", _FakeDb([(None,)]).scope)
    assert await stored_close("AAA", date(2026, 10, 16)) is None


async def test_stored_close_looks_up_the_utc_midnight_row(monkeypatch):
    db = _FakeDb([(101.25,)])
    monkeypatch.setattr(store, "session_scope", db.scope)
    assert await stored_close("AAA", date(2026, 10, 16)) == 101.25
    (_, statement, params) = next(c for c in db.calls if c[0] == "sql")
    assert statement is store._STORED_CLOSE_SQL
    assert params == {"symbol": "AAA", "ts": datetime(2026, 10, 16, tzinfo=UTC)}


# --- round trips against a real database ----------------------------------------------

TEST_SYMBOLS = ["ZZTEST", "ZZLIQ", "ZZILLQ", "ZZNOBARS"]


async def _wipe() -> None:
    from atlas_shared.db import session_scope

    async with session_scope() as session:
        for table in ("bars", "assets"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE symbol = ANY(:symbols)"),
                {"symbols": TEST_SYMBOLS},
            )


@pytest.fixture
async def clean_db():
    # The engine is cached per process but asyncpg connections belong to one event loop,
    # and pytest-asyncio gives every test its own loop, so rebuild it around each test.
    from atlas_shared.db import get_engine, get_sessionmaker

    async def reset() -> None:
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()

    await _wipe()
    try:
        yield
    finally:
        await _wipe()
        await reset()


@needs_db
async def test_daily_bars_round_trip_and_idempotent(clean_db):
    df = make_bars("ZZTEST", [10, 11, 12]).assign(resolution="1d")
    assert await upsert_daily_bars(df) == 3 and await upsert_daily_bars(df) == 3
    out = await load_daily_bars(["ZZTEST"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(out["close"]) == [10, 11, 12]


@needs_db
async def test_second_upsert_overwrites_and_nulls_become_nan(clean_db):
    first = _bars(vwap=[10.5, 11.5, 12.5], trade_count=[100, 200, 300])
    await upsert_daily_bars(first)
    second = _bars(closes=(10, 15, 12), vwap=[np.nan] * 3, trade_count=[np.nan, 250.0, np.nan])
    await upsert_daily_bars(second)
    out = await load_daily_bars(["ZZTEST"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(out["close"]) == [10, 15, 12] and len(out) == 3
    assert out["vwap"].isna().all()
    assert out["trade_count"].isna().tolist() == [True, False, True]
    assert out["ts"].dt.tz is not None and str(out["ts"].dt.tz) == "UTC"


@needs_db
async def test_load_daily_bars_filters_by_symbol_and_inclusive_dates(clean_db):
    await upsert_daily_bars(pd.concat([_bars("ZZTEST"), _bars("ZZLIQ", closes=(1, 2, 3))]))
    only = await load_daily_bars(["ZZTEST"], date(2020, 1, 3), date(2020, 1, 3))
    assert list(only["symbol"]) == ["ZZTEST"] and list(only["close"]) == [11]
    both = await load_daily_bars(["ZZTEST", "ZZLIQ"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(both["symbol"]) == ["ZZLIQ"] * 3 + ["ZZTEST"] * 3
    everything = await load_daily_bars(None, date(2020, 1, 1), date(2020, 1, 4))
    assert {"ZZTEST", "ZZLIQ"} <= set(everything["symbol"])


@needs_db
async def test_column_loads_read_only_their_resolution(clean_db):
    raw = _bars("ZZTEST", closes=(20, 22, 24)).assign(resolution="1d_raw")
    await upsert_daily_bars(pd.concat([_bars("ZZTEST"), raw]))
    ohlcv = await load_ohlcv(["ZZTEST"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(ohlcv["close"]) == [10, 11, 12] and "vwap" not in ohlcv.columns
    closes = await load_raw_closes(["ZZTEST"], date(2020, 1, 3), date(2020, 1, 3))
    assert list(closes["close"]) == [22] and list(closes.columns) == ["ts", "symbol", "close"]


@needs_db
async def test_last_bar_close_and_prefilter_reads(clean_db):
    thin = make_bars("ZZILLQ", [10, 10, 10], volume=1_000).assign(resolution="1d")
    await upsert_daily_bars(pd.concat([_bars("ZZLIQ", closes=(10, 11, 12)), thin]))
    assert (await last_bar_dates())["ZZLIQ"] == date(2020, 1, 6)
    assert await stored_close("ZZLIQ", date(2020, 1, 3)) == 11.0
    assert await stored_close("ZZLIQ", date(2020, 1, 1)) is None
    liquid = await liquid_prefilter(10_000_000)
    assert "ZZLIQ" in liquid and "ZZILLQ" not in liquid and liquid == sorted(liquid)


@needs_db
async def test_assets_upsert_overwrites_and_ranges_follow_the_bars(clean_db):
    asset = {
        "symbol": "ZZTEST",
        "name": "Zz Test Inc.",
        "exchange": "NYSE",
        "status": "active",
        "kind": "common",
        "tradable": True,
    }
    never = {"symbol": "ZZNOBARS", "status": "inactive", "kind": "etf"}
    assert await upsert_assets([asset, never]) == 2
    await upsert_assets([{**asset, "name": "Zz Renamed Inc.", "tradable": False}])
    await upsert_daily_bars(_bars())
    await refresh_asset_bar_ranges()
    loaded = await load_assets()
    row = loaded.set_index("symbol").loc["ZZTEST"]
    assert row["name"] == "Zz Renamed Inc." and not row["tradable"]
    assert row["first_bar"] == date(2020, 1, 2) and row["last_bar"] == date(2020, 1, 6)
    idle = loaded.set_index("symbol").loc["ZZNOBARS"]
    assert idle["kind"] == "etf" and pd.isna(idle["first_bar"]) and pd.isna(idle["last_bar"])
    assert list(loaded["symbol"]) == sorted(loaded["symbol"])
