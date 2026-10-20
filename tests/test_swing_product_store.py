"""Swing product layer: migration 0010, the validated settings model and the product store.

The settings validation, the migration text, the SQL contracts and the parameter building (against
a fake session) run everywhere. The round trips against a real database are skipped unless
something answers on 127.0.0.1:5432 and migration 0010 is applied.

Isolation of the database tests: every one runs inside a transaction on its own connection that
is rolled back at the end (``session_scope`` is swapped for a savepoint-based one), so nothing
they write is ever committed, whatever fails, and the real rows in the same tables are never
touched. The sessions and symbols they use are still far-past or far-future ones (1999-01-04,
``ZZTEST*``) so a hand run against a populated database stays unambiguous. They also build their
own engine instead of the lru-cached ``get_engine``, so they cannot add to the "Event loop is
closed" teardown noise that a loop-crossing cached engine causes.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from atlas_shared.config import get_settings
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from swing_engine import product_store
from swing_engine.config import RiskSettings, UniverseFilter
from swing_engine.product_store import (
    _MARK_DIGEST_SQL,
    _UPSERT_IDEA_SQL,
    _UPSERT_SCAN_SQL,
    create_position,
    digest_sent,
    finish_job,
    get_position,
    idea_rows,
    latest_job,
    latest_scan,
    list_positions,
    mark_digest,
    put_settings,
    save_scan,
    scanned_sessions,
    start_job,
    trackable_ideas,
    update_idea_tracks,
    update_position,
)
from swing_engine.product_store import (
    get_settings as get_swing_settings,
)
from swing_engine.regime import Regime
from swing_engine.scan import Idea, ScanResult, scan_to_dict
from swing_engine.settings import SwingSettings
from swing_engine.sizing import Capacity, Sizing
from swing_engine.testing import postgres_reachable

MIGRATIONS = Path(__file__).resolve().parents[1] / "packages" / "shared-py" / "migrations"
MIGRATION = MIGRATIONS / "0010_swing_product.sql"
db = pytest.mark.skipif(not postgres_reachable(), reason="needs local Postgres")

SESSION = date(1999, 1, 4)
TARGET = date(1999, 1, 5)
FIRST_PUBLISH = datetime(1999, 1, 4, 22, 5, tzinfo=UTC)
SECOND_PUBLISH = datetime(1999, 1, 4, 23, 30, tzinfo=UTC)


# --- settings model ---------------------------------------------------------------------


def test_settings_defaults_and_bounds():
    assert SwingSettings().risk().risk_pct == 0.01 and SwingSettings().universe().min_price == 10.0
    with pytest.raises(ValidationError):
        SwingSettings(risk_pct=0)
    with pytest.raises(ValidationError):
        SwingSettings(max_positions=0)
    with pytest.raises(ValidationError):
        SwingSettings(equity=-1)


def test_differs_from_default_universe():
    assert SwingSettings(min_price=5).differs_from_default_universe()


def test_default_settings_do_not_differ_from_the_default_universe():
    assert not SwingSettings().differs_from_default_universe()
    assert SwingSettings(min_dollar_volume=5_000_000).differs_from_default_universe()


def test_settings_defaults_are_the_engine_defaults():
    # The validated setups ran on RiskSettings() and UniverseFilter(); the product starts there.
    s, risk, universe = SwingSettings(), RiskSettings(), UniverseFilter()
    assert s.risk() == risk
    assert s.universe() == universe
    assert s.telegram_digest is True


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [
        ("equity", [0.01, 25_000, 1e9], [0, -1, float("inf"), float("nan")]),
        ("risk_pct", [0.0001, 0.05], [0, -0.01, 0.0501, 1, float("nan")]),
        ("max_position_pct", [0.01, 1], [0, 1.01, -0.2]),
        ("max_positions", [1, 50], [0, 51, -3, 2.5]),
        ("max_total_risk_pct", [0.01, 0.5], [0, 0.51, 1]),
        ("min_price", [1, 10, 500], [0, 0.99, -5]),
        ("min_dollar_volume", [1_000_000, 5e7], [0, 999_999, -1, float("inf")]),
    ],
)
def test_settings_bounds_are_inclusive_where_the_brief_says(field, good, bad):
    for value in good:
        assert getattr(SwingSettings(**{field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError):
            SwingSettings(**{field: value})


def test_settings_stay_valid_after_assignment():
    s = SwingSettings()
    s.equity = 40_000
    assert s.equity == 40_000
    with pytest.raises(ValidationError):
        s.risk_pct = 0.5


def test_settings_is_a_pydantic_model_with_the_documented_fields():
    assert issubclass(SwingSettings, BaseModel)
    assert list(SwingSettings.model_fields) == [
        "equity",
        "risk_pct",
        "max_position_pct",
        "max_positions",
        "max_total_risk_pct",
        "min_price",
        "min_dollar_volume",
        "telegram_digest",
    ]


def test_risk_and_universe_carry_the_user_values():
    s = SwingSettings(
        equity=40_000,
        risk_pct=0.02,
        max_position_pct=0.1,
        max_positions=5,
        max_total_risk_pct=0.08,
        min_price=15,
        min_dollar_volume=30_000_000,
    )
    assert s.risk() == RiskSettings(
        equity=40_000,
        risk_pct=0.02,
        max_position_pct=0.1,
        max_positions=5,
        max_total_risk_pct=0.08,
    )
    # Only the two product-level fields move; every other universe rule stays at its code default.
    assert s.universe() == replace(UniverseFilter(), min_price=15, min_median_dollar_volume=30e6)
    assert s.universe().min_atr_pct == 0.005 and s.universe().max_gap_sessions == 10


# --- migration 0010 ---------------------------------------------------------------------


def test_migration_0010_no_numeric_and_unique_idea_key():
    sql = (MIGRATIONS / "0010_swing_product.sql").read_text().upper()
    assert "NUMERIC" not in sql and "UNIQUE (SESSION, SYMBOL, SETUP)" in sql


def test_migration_0010_has_no_sqlalchemy_bind_parameters():
    # The runner executes each file through text(), where ":word" would be read as a bind.
    assert text(MIGRATION.read_text()).compile().params == {}


def _flat_sql() -> str:
    return " ".join(MIGRATION.read_text().split())


def test_migration_0010_creates_every_product_table():
    sql = _flat_sql()
    for table in (
        "swing_settings",
        "swing_scans",
        "swing_ideas",
        "swing_positions",
        "swing_job_runs",
        "swing_digests",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql


def test_migration_0010_settings_is_a_single_row_table():
    sql = _flat_sql()
    assert "id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1)" in sql


def test_migration_0010_scan_and_digest_keys():
    sql = _flat_sql()
    assert "session DATE PRIMARY KEY" in sql
    assert "PRIMARY KEY (session, kind)" in sql


def _table_body(name: str) -> str:
    sql = _flat_sql()
    start = sql.index(f"CREATE TABLE IF NOT EXISTS {name} (")
    return sql[start : sql.index(");", start)]


def test_migration_0010_ideas_carry_known_date_after_exit_reason_and_watch_only():
    body = _table_body("swing_ideas")
    assert "exit_reason TEXT, known_date DATE, r_multiple DOUBLE PRECISION" in body
    assert "watch_only BOOLEAN NOT NULL DEFAULT FALSE" in body
    assert "track_status TEXT NOT NULL DEFAULT 'pending'" in body
    assert '"overlaps" TEXT[]' in body  # OVERLAPS is reserved in Postgres, so it is quoted


def test_migration_0010_scan_payload_is_jsonb():
    assert "payload JSONB" in _table_body("swing_scans")
    assert "steps JSONB" in _table_body("swing_job_runs")


# --- SQL contracts ----------------------------------------------------------------------


def test_save_scan_sql_preserves_published_at_on_conflict():
    assert "published_at" not in _UPSERT_IDEA_SQL.text.split("DO UPDATE SET")[1]


def test_idea_upsert_keeps_first_publication_and_tracking_state():
    update = _UPSERT_IDEA_SQL.text.split("DO UPDATE SET")[1]
    assert "ON CONFLICT (session, symbol, setup) DO UPDATE SET" in _UPSERT_IDEA_SQL.text
    for kept in (
        "published_at",
        "reconstructed",
        "track_status",
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "exit_reason",
        "known_date",
        "r_multiple",
        "current_stop",
        "trigger",
        "stop",
    ):
        assert kept not in update, kept
    for refreshed in ("shares", "risk_dollars", "position_value", "rank", "live"):
        assert f"{refreshed} = EXCLUDED.{refreshed}" in update


def test_scan_upsert_keeps_the_first_reconstructed_flag():
    update = _UPSERT_SCAN_SQL.text.split("DO UPDATE SET")[1]
    assert "ON CONFLICT (session) DO UPDATE SET" in _UPSERT_SCAN_SQL.text
    assert "reconstructed" not in update
    assert "payload = EXCLUDED.payload" in update


def test_a_failed_digest_never_overwrites_a_sent_one():
    sql = _MARK_DIGEST_SQL.text
    assert "ON CONFLICT (session, kind) DO UPDATE" in sql and "NOT swing_digests.ok" in sql


def test_product_store_exposes_the_async_interface_later_tasks_rely_on():
    for fn in (
        get_swing_settings,
        put_settings,
        save_scan,
        latest_scan,
        scanned_sessions,
        trackable_ideas,
        update_idea_tracks,
        idea_rows,
        list_positions,
        create_position,
        update_position,
        get_position,
        start_job,
        finish_job,
        latest_job,
        digest_sent,
        mark_digest,
    ):
        assert inspect.iscoroutinefunction(fn), fn.__name__


# --- parameter building against a fake session (no database) ---------------------------


def _idea(
    symbol: str,
    setup: str = "breakout",
    *,
    rank: int = 1,
    live: bool = True,
    shares: int = 40,
    atr14: float = 1.5,
    overlaps: Sequence[str] = (),
) -> Idea:
    return Idea(
        symbol=symbol,
        setup=setup,
        version=f"{setup}-v1",
        trigger=50.0,
        stop=47.0,
        atr14=atr14,
        rs_pct=92.5,
        rank=rank,
        sizing=Sizing(
            shares=shares,
            risk_dollars=shares * 3.0,
            position_value=shares * 50.0,
            capped_by=None,
        ),
        overlaps=list(overlaps),
        live=live,
        regime_warning=False,
    )


def _scan(ideas: Sequence[Idea], watch_only: Sequence[Idea] = (), **kw: Any) -> ScanResult:
    return ScanResult(
        session=kw.get("session", SESSION),
        regime=kw.get("regime", Regime.UPTREND),
        regime_factor=1.0,
        breadth=kw.get("breadth", 61.5),
        universe_size=kw.get("universe_size", 1800),
        ideas=list(ideas),
        watch_only=list(watch_only),
        capacity=Capacity(
            open_positions=0, max_positions=8, open_risk_dollars=0.0, max_risk_dollars=1500.0
        ),
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeDb:
    """Stands in for ``session_scope``; records every execute call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.sessions = 0

    @asynccontextmanager
    async def scope(self):
        self.sessions += 1
        db = self

        class _Session:
            async def execute(self, statement: Any, params: Any = None) -> _Result:
                db.calls.append((statement, params))
                return _Result([])

        yield _Session()


@pytest.fixture
def fake_db(monkeypatch) -> _FakeDb:
    fake = _FakeDb()
    monkeypatch.setattr(product_store, "session_scope", fake.scope)
    return fake


async def test_save_scan_writes_ideas_and_watch_only_with_flags_and_one_payload(fake_db):
    live_idea = _idea("ZZTEST1", rank=1)
    unproven = _idea("ZZTEST2", "pullback", rank=2, live=False, overlaps=["ZZTEST9"])
    parked = _idea("ZZTEST3", rank=1, shares=0, atr14=float("nan"))
    scan = _scan([live_idea, unproven], [parked])
    await save_scan(scan, TARGET, FIRST_PUBLISH, reconstructed=True)

    assert fake_db.sessions == 1  # scan and ideas are one transaction
    by_statement = {id(s): p for s, p in fake_db.calls}
    scan_params = by_statement[id(_UPSERT_SCAN_SQL)]
    assert scan_params["session"] == SESSION and scan_params["target_session"] == TARGET
    assert scan_params["regime"] == "uptrend" and scan_params["reconstructed"] is True
    assert scan_params["breadth"] == 61.5 and scan_params["universe_size"] == 1800
    assert json.loads(scan_params["payload"]) == scan_to_dict(scan)

    rows = by_statement[id(_UPSERT_IDEA_SQL)]
    assert [(r["symbol"], r["setup"], r["watch_only"]) for r in rows] == [
        ("ZZTEST1", "breakout", False),
        ("ZZTEST2", "pullback", False),
        ("ZZTEST3", "breakout", True),
    ]
    first, second, third = rows
    assert first["version"] == "breakout-v1" and first["live"] is True
    assert (first["trigger"], first["stop"], first["rs_pct"], first["rank"]) == (
        50.0,
        47.0,
        92.5,
        1,
    )
    assert (first["shares"], first["risk_dollars"], first["position_value"]) == (40, 120.0, 2000.0)
    assert first["overlaps"] == [] and second["overlaps"] == ["ZZTEST9"]
    assert second["live"] is False
    assert third["shares"] == 0 and third["risk_dollars"] == 0.0
    assert third["atr14"] is None  # NaN reaches Postgres as NULL
    for r in rows:
        assert r["session"] == SESSION and r["target_session"] == TARGET
        assert r["published_at"] == FIRST_PUBLISH and r["reconstructed"] is True


async def test_save_scan_with_no_ideas_still_stores_the_scan(fake_db):
    await save_scan(_scan([]), TARGET, FIRST_PUBLISH, reconstructed=False)
    assert [s for s, _ in fake_db.calls] == [_UPSERT_SCAN_SQL]


async def test_save_scan_payload_is_valid_json_even_with_nan_breadth(fake_db):
    scan = _scan([_idea("ZZTEST1")], breadth=float("nan"))
    await save_scan(scan, TARGET, FIRST_PUBLISH, reconstructed=False)
    params = fake_db.calls[0][1]
    assert params["breadth"] is None
    assert json.loads(params["payload"])["breadth"] is None


async def test_track_and_position_updates_reject_unknown_columns_before_the_database(fake_db):
    with pytest.raises(ValueError, match="bogus"):
        await update_idea_tracks([{"id": uuid.uuid4(), "bogus": 1}])
    with pytest.raises(ValueError, match="id"):
        await update_idea_tracks([{"track_status": "open"}])
    with pytest.raises(ValueError, match="bogus"):
        await update_position(uuid.uuid4(), {"bogus": 1})
    with pytest.raises(ValueError, match="id"):
        await update_position(uuid.uuid4(), {"id": uuid.uuid4()})
    with pytest.raises(ValueError, match="entry_price"):
        await create_position({"symbol": "ZZTEST", "opened_on": SESSION})
    assert fake_db.sessions == 0 and fake_db.calls == []


async def test_update_idea_tracks_of_nothing_never_opens_a_session(fake_db):
    await update_idea_tracks([])
    assert fake_db.sessions == 0


# --- round trips against a real database ------------------------------------------------


@pytest.fixture
async def pg(monkeypatch):
    """Run the store inside one transaction on a private engine, rolled back at the end."""
    engine = create_async_engine(get_settings().postgres_dsn)
    try:
        async with engine.connect() as conn:
            present = (await conn.execute(text("SELECT to_regclass('swing_settings')"))).scalar()
            if present is None:
                pytest.skip("migration 0010 is not applied (run atlas_shared.migrate up)")
            await conn.rollback()
            outer = await conn.begin()

            @asynccontextmanager
            async def scope():
                async with AsyncSession(
                    bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
                ) as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise

            monkeypatch.setattr(product_store, "session_scope", scope)
            try:
                yield conn
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


async def _idea_row(conn, symbol: str, setup: str = "breakout") -> dict[str, Any]:
    result = await conn.execute(
        text("SELECT * FROM swing_ideas WHERE session = :s AND symbol = :y AND setup = :u"),
        {"s": SESSION, "y": symbol, "u": setup},
    )
    return dict(result.mappings().one())


@db
async def test_save_scan_twice_keeps_first_published_at(pg):
    first = _scan([_idea("ZZTEST1", rank=1, shares=40, live=False)])
    await save_scan(first, TARGET, FIRST_PUBLISH, reconstructed=True)
    again = _scan([_idea("ZZTEST1", rank=3, shares=25, live=True)])
    await save_scan(again, TARGET, SECOND_PUBLISH, reconstructed=False)

    rows = (
        (await pg.execute(text("SELECT * FROM swing_ideas WHERE session = :s"), {"s": SESSION}))
        .mappings()
        .all()
    )
    assert len(rows) == 1  # one set of ideas per (session, symbol, setup)
    row = rows[0]
    assert row["published_at"] == FIRST_PUBLISH and row["reconstructed"] is True
    assert (row["shares"], row["rank"], row["live"]) == (25, 3, True)
    assert row["risk_dollars"] == 75.0 and row["position_value"] == 1250.0
    assert row["track_status"] == "pending" and row["watch_only"] is False
    assert row["known_date"] is None and row["current_stop"] is None
    scans = (
        (await pg.execute(text("SELECT * FROM swing_scans WHERE session = :s"), {"s": SESSION}))
        .mappings()
        .all()
    )
    assert len(scans) == 1 and scans[0]["reconstructed"] is True
    assert scans[0]["target_session"] == TARGET


@db
async def test_save_scan_resave_does_not_reset_tracking_state(pg):
    await save_scan(_scan([_idea("ZZTEST1")]), TARGET, FIRST_PUBLISH, reconstructed=False)
    row = await _idea_row(pg, "ZZTEST1")
    await update_idea_tracks(
        [
            {
                "id": row["id"],
                "track_status": "open",
                "entry_date": TARGET,
                "entry_price": 50.1,
                "known_date": TARGET,
                "current_stop": 47.0,
            }
        ]
    )
    await save_scan(_scan([_idea("ZZTEST1", rank=2)]), TARGET, SECOND_PUBLISH, reconstructed=False)
    after = await _idea_row(pg, "ZZTEST1")
    assert after["id"] == row["id"] and after["rank"] == 2
    assert after["track_status"] == "open" and after["entry_price"] == 50.1
    assert after["entry_date"] == TARGET and after["known_date"] == TARGET


@db
async def test_save_scan_stores_watch_only_ideas_apart_and_a_downtrend_payload(pg):
    scan = _scan(
        [_idea("ZZTEST1")],
        [_idea("ZZTEST2", rank=1, shares=0, live=False, overlaps=["ZZTEST1"])],
        regime=Regime.DOWNTREND,
    )
    await save_scan(scan, TARGET, FIRST_PUBLISH, reconstructed=False)
    live_row, parked_row = await _idea_row(pg, "ZZTEST1"), await _idea_row(pg, "ZZTEST2")
    assert live_row["watch_only"] is False and parked_row["watch_only"] is True
    assert parked_row["shares"] == 0 and parked_row["live"] is False
    assert parked_row["overlaps"] == ["ZZTEST1"]
    stored = (
        await pg.execute(
            text("SELECT payload, regime FROM swing_scans WHERE session = :s"), {"s": SESSION}
        )
    ).one()
    assert stored.regime == "downtrend"
    assert stored.payload == scan_to_dict(scan)


@db
async def test_latest_scan_scanned_sessions_and_payload_round_trip(pg):
    future = date(2999, 1, 4)
    older = _scan([_idea("ZZTEST1")], session=SESSION)
    newest = _scan([_idea("ZZTEST1")], session=future, regime=Regime.MIXED, universe_size=1234)
    await save_scan(older, TARGET, FIRST_PUBLISH, reconstructed=False)
    await save_scan(newest, future + timedelta(days=1), FIRST_PUBLISH, reconstructed=True)

    latest = await latest_scan()
    assert latest is not None
    assert latest["session"] == future and latest["target_session"] == date(2999, 1, 5)
    assert latest["regime"] == "mixed" and latest["universe_size"] == 1234
    assert latest["breadth"] == 61.5 and latest["reconstructed"] is True
    assert isinstance(latest["generated_at"], datetime) and latest["generated_at"].tzinfo
    assert latest["payload"] == scan_to_dict(newest)  # parsed JSON, not a string
    assert {SESSION, future} <= await scanned_sessions(SESSION)
    assert future in await scanned_sessions(date(2999, 1, 1))
    assert SESSION not in await scanned_sessions(date(1999, 1, 5))


@db
async def test_latest_scan_is_none_without_scans(pg):
    await pg.execute(text("DELETE FROM swing_scans"))
    assert await latest_scan() is None
    assert await scanned_sessions(date(1990, 1, 1)) == set()


@db
async def test_track_updates_write_known_date_and_feed_the_readers(pg):
    ideas = [_idea("ZZTEST1", "breakout"), _idea("ZZTEST2", "pullback", rank=2)]
    await save_scan(_scan(ideas), TARGET, FIRST_PUBLISH, reconstructed=False)
    mine = {r["symbol"]: r for r in await trackable_ideas() if r["session"] == SESSION}
    assert set(mine) == {"ZZTEST1", "ZZTEST2"}
    one = mine["ZZTEST1"]
    assert isinstance(one["id"], uuid.UUID) and one["track_status"] == "pending"
    assert one["known_date"] is None and one["overlaps"] == []

    await update_idea_tracks(
        [
            {
                "id": one["id"],
                "track_status": "closed",
                "entry_date": TARGET,
                "entry_price": 50.2,
                "exit_date": date(1999, 1, 12),
                "exit_price": 55.0,
                "exit_reason": "trail",
                "known_date": date(1999, 1, 12),
                "r_multiple": 1.6,
                "current_stop": 52.0,
            },
            {"id": mine["ZZTEST2"]["id"], "track_status": "open", "current_stop": 47.0},
        ]
    )
    still = {r["symbol"]: r for r in await trackable_ideas() if r["session"] == SESSION}
    assert set(still) == {"ZZTEST2"}  # the closed idea left the trackable set
    assert still["ZZTEST2"]["track_status"] == "open" and still["ZZTEST2"]["current_stop"] == 47.0
    assert still["ZZTEST2"]["entry_price"] is None  # an omitted field is left alone

    closed = [r for r in await idea_rows("breakout") if r["session"] == SESSION]
    assert [r["symbol"] for r in closed] == ["ZZTEST1"]
    got = closed[0]
    assert got["entry_date"] == TARGET and got["known_date"] == date(1999, 1, 12)
    assert got["exit_reason"] == "trail" and got["r_multiple"] == 1.6
    assert got["watch_only"] is False and got["version"] == "breakout-v1"
    everything = [r for r in await idea_rows() if r["session"] == SESSION]
    assert {r["symbol"] for r in everything} == {"ZZTEST1", "ZZTEST2"}


@db
async def test_update_idea_tracks_can_clear_a_field_with_none(pg):
    await save_scan(_scan([_idea("ZZTEST1")]), TARGET, FIRST_PUBLISH, reconstructed=False)
    row = await _idea_row(pg, "ZZTEST1")
    await update_idea_tracks([{"id": row["id"], "track_status": "open", "entry_price": 51.0}])
    await update_idea_tracks([{"id": row["id"], "entry_price": None}])
    after = await _idea_row(pg, "ZZTEST1")
    assert after["entry_price"] is None and after["track_status"] == "open"


@db
async def test_settings_default_without_a_row_then_round_trip(pg):
    await pg.execute(text("DELETE FROM swing_settings"))
    assert await get_swing_settings() == SwingSettings()
    mine = SwingSettings(equity=40_000, risk_pct=0.02, max_positions=5, telegram_digest=False)
    assert await put_settings(mine) == mine
    assert await get_swing_settings() == mine
    updated = mine.model_copy(update={"min_price": 15.0})
    assert await put_settings(updated) == updated  # a second put replaces the single row
    assert (await pg.execute(text("SELECT count(*) FROM swing_settings"))).scalar() == 1
    assert await get_swing_settings() == updated


@db
async def test_settings_table_refuses_a_second_row(pg):
    await put_settings(SwingSettings())
    with pytest.raises(Exception, match="swing_settings"):
        async with pg.begin_nested():
            await pg.execute(
                text(
                    "INSERT INTO swing_settings (id, equity, risk_pct, max_position_pct, "
                    "max_total_risk_pct, min_price, min_dollar_volume, max_positions, "
                    "telegram_digest) VALUES (2, 1, 0.01, 0.2, 0.06, 10, 2e7, 8, true)"
                )
            )


@db
async def test_positions_create_list_get_update(pg):
    row = await create_position(
        {
            "symbol": "ZZTEST1",
            "opened_on": SESSION,
            "entry_price": 50.25,
            "shares": 40,
            "initial_stop": 47.0,
            "trail_atr": 3.0,
            "notes": "test",
        }
    )
    assert isinstance(row["id"], uuid.UUID) and row["idea_id"] is None
    assert row["status"] == "open" and row["current_stop"] == 47.0  # defaults to the initial stop
    assert row["shares"] == 40.0 and isinstance(row["shares"], float)
    assert isinstance(row["created_at"], datetime)
    assert row["closed_on"] is None and row["last_action"] is None

    other = await create_position(
        {
            "symbol": "ZZTEST2",
            "opened_on": TARGET,
            "entry_price": 10.0,
            "shares": 5,
            "initial_stop": 9.0,
            "current_stop": 9.5,
        }
    )
    assert other["current_stop"] == 9.5
    assert (await get_position(row["id"])) == row
    assert await get_position(uuid.uuid4()) is None
    open_symbols = {p["symbol"] for p in await list_positions("open")}
    assert {"ZZTEST1", "ZZTEST2"} <= open_symbols

    moved = await update_position(
        row["id"],
        {
            "current_stop": 49.0,
            "last_action": "raise_stop",
            "last_action_text": "Raise stop to 49.00",
            "last_action_on": TARGET,
        },
    )
    assert moved["current_stop"] == 49.0 and moved["last_action"] == "raise_stop"
    assert moved["last_action_on"] == TARGET and moved["entry_price"] == 50.25

    closed = await update_position(
        row["id"],
        {
            "status": "closed",
            "closed_on": date(1999, 1, 11),
            "exit_price": 54.0,
            "exit_reason": "manual",
            "r_multiple": 1.2,
        },
    )
    assert closed["status"] == "closed" and closed["r_multiple"] == 1.2
    still_open = {p["symbol"] for p in await list_positions("open")}
    assert "ZZTEST1" not in still_open and "ZZTEST2" in still_open
    assert "ZZTEST1" in {p["symbol"] for p in await list_positions("closed")}
    every = {p["symbol"] for p in await list_positions(None)}
    assert {"ZZTEST1", "ZZTEST2"} <= every
    with pytest.raises(KeyError):
        await update_position(uuid.uuid4(), {"notes": "x"})


@db
async def test_position_can_link_to_the_idea_it_came_from(pg):
    await save_scan(_scan([_idea("ZZTEST1")]), TARGET, FIRST_PUBLISH, reconstructed=False)
    idea = await _idea_row(pg, "ZZTEST1")
    made = await create_position(
        {
            "symbol": "ZZTEST1",
            "opened_on": TARGET,
            "entry_price": 50.2,
            "shares": 40,
            "initial_stop": 47.0,
            "idea_id": idea["id"],
        }
    )
    assert made["idea_id"] == idea["id"] and isinstance(made["idea_id"], uuid.UUID)


@db
async def test_job_runs_start_finish_latest(pg):
    started = await start_job("zztest-nightly", SESSION)
    assert isinstance(started, uuid.UUID)
    running = await latest_job()
    assert running is not None and running["id"] == started
    assert running["kind"] == "zztest-nightly" and running["session"] == SESSION
    assert running["status"] == "running" and running["finished_at"] is None
    assert isinstance(running["started_at"], datetime) and running["error"] is None

    steps = [{"step": "ingest", "ok": True, "n": 12}, {"step": "scan", "ok": False}]
    await finish_job(started, "failed", steps, "scan: SPY has no bar")
    done = await latest_job()
    assert done is not None and done["id"] == started
    assert done["status"] == "failed" and done["error"] == "scan: SPY has no bar"
    assert done["steps"] == steps  # parsed JSON, not a string
    assert done["finished_at"] is not None and done["finished_at"] >= done["started_at"]

    later = await start_job("zztest-manual", None)
    await finish_job(later, "ok", {"scan": {"ideas": 3}}, None)
    newest = await latest_job()
    assert newest is not None and newest["id"] == later and newest["session"] is None
    assert newest["steps"] == {"scan": {"ideas": 3}} and newest["error"] is None


@db
async def test_digests_are_per_session_and_kind_and_failures_can_retry(pg):
    assert await digest_sent(SESSION, "evening") is False
    await mark_digest(SESSION, "evening", False, "telegram 502")
    assert await digest_sent(SESSION, "evening") is False  # a failed send is not "sent"
    await mark_digest(SESSION, "evening", True, "sent")
    assert await digest_sent(SESSION, "evening") is True
    assert await digest_sent(SESSION, "morning") is False
    assert await digest_sent(TARGET, "evening") is False
    await mark_digest(SESSION, "evening", False, "late failure")  # must not undo a success
    assert await digest_sent(SESSION, "evening") is True
    detail = (
        await pg.execute(
            text("SELECT detail FROM swing_digests WHERE session = :s AND kind = 'evening'"),
            {"s": SESSION},
        )
    ).scalar()
    assert detail == "sent"
