"""Swing product persistence: settings, nightly scans and their ideas, positions, job runs, digests.

These are the tables of migration 0010. The functions are the async layer the nightly job, the
API and the digest build on; they return plain dicts (UUIDs as :class:`uuid.UUID`, dates as
``date``, measures as ``float``, JSON columns already parsed) and never ``Decimal``, because no
column here is a ``NUMERIC``. Each function is one transaction through :func:`session_scope`.

Publishing is idempotent. An idea is keyed by ``(session, symbol, setup)``, so scanning a
session twice (the scheduled run and a "Run scan now", or a catch-up) leaves one row per idea.
A second :func:`save_scan` refreshes the sizing, rank and live flag but keeps the first
``published_at`` and ``reconstructed`` and never touches what forward tracking has written.

Missing values reach the database as NULL, never NaN, as in :mod:`swing_engine.store`.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from sqlalchemy import TextClause, text

from swing_engine.scan import Idea, ScanResult, json_safe, scan_to_dict
from swing_engine.settings import SwingSettings

log = get_logger("swing.product_store")

_SETTINGS_FIELDS = tuple(SwingSettings.model_fields)

_SCAN_COLUMNS = (
    "session",
    "target_session",
    "generated_at",
    "regime",
    "breadth",
    "universe_size",
    "reconstructed",
    "payload",
)
_IDEA_COLUMNS = (
    "id",
    "session",
    "target_session",
    "symbol",
    "setup",
    "version",
    "live",
    "watch_only",
    "trigger",
    "stop",
    "atr14",
    "rs_pct",
    "rank",
    "shares",
    "risk_dollars",
    "position_value",
    "overlaps",
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
)
# What forward tracking writes back to an idea; nothing else about an idea changes after it
# is published except through the upsert.
_TRACK_COLUMNS = (
    "track_status",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
    "exit_reason",
    "known_date",
    "r_multiple",
    "current_stop",
)
_POSITION_COLUMNS = (
    "id",
    "symbol",
    "opened_on",
    "entry_price",
    "shares",
    "initial_stop",
    "current_stop",
    "trail_atr",
    "idea_id",
    "status",
    "closed_on",
    "exit_price",
    "exit_reason",
    "r_multiple",
    "last_action",
    "last_action_text",
    "last_action_on",
    "notes",
    "created_at",
)
_POSITION_REQUIRED = ("symbol", "opened_on", "entry_price", "shares", "initial_stop")
_POSITION_WRITABLE = tuple(c for c in _POSITION_COLUMNS if c not in ("id", "created_at"))
_POSITION_FLOATS = frozenset(
    {
        "entry_price",
        "shares",
        "initial_stop",
        "current_stop",
        "trail_atr",
        "exit_price",
        "r_multiple",
    }
)
_TRACK_FLOATS = frozenset({"entry_price", "exit_price", "r_multiple", "current_stop"})
_JOB_COLUMNS = (
    "id",
    "kind",
    "session",
    "started_at",
    "finished_at",
    "status",
    "steps",
    "error",
)
_UUID_COLUMNS = frozenset({"id", "idea_id"})


_RESERVED = frozenset({"overlaps"})  # Postgres reserved words that are also column names


def _quoted(column: str) -> str:
    return f'"{column}"' if column in _RESERVED else column


def _cols(columns: Sequence[str]) -> str:
    return ", ".join(_quoted(c) for c in columns)


def _binds(columns: Sequence[str]) -> str:
    return ", ".join(f":{c}" for c in columns)


# --- SQL -------------------------------------------------------------------------------

_GET_SETTINGS_SQL = text(f"SELECT {_cols(_SETTINGS_FIELDS)} FROM swing_settings WHERE id = 1")
_PUT_SETTINGS_SQL = text(
    f"""
    INSERT INTO swing_settings (id, {_cols(_SETTINGS_FIELDS)})
    VALUES (1, {_binds(_SETTINGS_FIELDS)})
    ON CONFLICT (id) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _SETTINGS_FIELDS)},
        updated_at = now()
    RETURNING {_cols(_SETTINGS_FIELDS)}
    """
)

# A re-scan refreshes the scan but keeps whether the first one was a catch-up.
_UPSERT_SCAN_SQL = text(
    """
    INSERT INTO swing_scans
        (session, target_session, generated_at, regime, breadth, universe_size,
         reconstructed, payload)
    VALUES
        (:session, :target_session, now(), :regime, :breadth, :universe_size,
         :reconstructed, CAST(:payload AS JSONB))
    ON CONFLICT (session) DO UPDATE SET
        target_session = EXCLUDED.target_session,
        generated_at = EXCLUDED.generated_at,
        regime = EXCLUDED.regime,
        breadth = EXCLUDED.breadth,
        universe_size = EXCLUDED.universe_size,
        payload = EXCLUDED.payload
    """
)

# The idea keeps its first published_at and reconstructed flag, its trigger and stop, and every
# tracking column; a re-scan only refreshes how it is sized and shown (shares, risk, position
# value, rank, live, the open-position overlaps and whether it is parked as watch-only).
_UPSERT_IDEA_SQL = text(
    """
    INSERT INTO swing_ideas
        (session, target_session, symbol, setup, version, live, watch_only, trigger, stop,
         atr14, rs_pct, rank, shares, risk_dollars, position_value, "overlaps", published_at,
         reconstructed)
    VALUES
        (:session, :target_session, :symbol, :setup, :version, :live, :watch_only, :trigger,
         :stop, :atr14, :rs_pct, :rank, :shares, :risk_dollars, :position_value, :overlaps,
         :published_at, :reconstructed)
    ON CONFLICT (session, symbol, setup) DO UPDATE SET
        live = EXCLUDED.live,
        watch_only = EXCLUDED.watch_only,
        rank = EXCLUDED.rank,
        shares = EXCLUDED.shares,
        risk_dollars = EXCLUDED.risk_dollars,
        position_value = EXCLUDED.position_value,
        "overlaps" = EXCLUDED."overlaps"
    """
)

_LATEST_SCAN_SQL = text(
    f"SELECT {_cols(_SCAN_COLUMNS)} FROM swing_scans ORDER BY session DESC LIMIT 1"
)
_SCANNED_SESSIONS_SQL = text("SELECT session FROM swing_scans WHERE session >= :since")

_TRACKABLE_IDEAS_SQL = text(
    f"""
    SELECT {_cols(_IDEA_COLUMNS)}
    FROM swing_ideas
    WHERE track_status IN ('pending', 'open')
    ORDER BY session, symbol, setup
    """
)
_ALL_IDEAS_SQL = text(
    f"SELECT {_cols(_IDEA_COLUMNS)} FROM swing_ideas ORDER BY session, symbol, setup"
)
_SETUP_IDEAS_SQL = text(
    f"""
    SELECT {_cols(_IDEA_COLUMNS)}
    FROM swing_ideas
    WHERE setup = :setup
    ORDER BY session, symbol, setup
    """
)

_ALL_POSITIONS_SQL = text(
    f"""
    SELECT {_cols(_POSITION_COLUMNS)}
    FROM swing_positions
    ORDER BY opened_on DESC, created_at DESC, symbol
    """
)
_POSITIONS_BY_STATUS_SQL = text(
    f"""
    SELECT {_cols(_POSITION_COLUMNS)}
    FROM swing_positions
    WHERE status = :status
    ORDER BY opened_on DESC, created_at DESC, symbol
    """
)
_GET_POSITION_SQL = text(f"SELECT {_cols(_POSITION_COLUMNS)} FROM swing_positions WHERE id = :id")

_START_JOB_SQL = text(
    """
    INSERT INTO swing_job_runs (kind, session, started_at, status)
    VALUES (:kind, :session, clock_timestamp(), 'running')
    RETURNING id
    """
)
_FINISH_JOB_SQL = text(
    """
    UPDATE swing_job_runs
    SET finished_at = clock_timestamp(), status = :status,
        steps = CAST(:steps AS JSONB), error = :error
    WHERE id = :id
    """
)
# Progress while a job runs (a first backfill takes hours): the step log only, never the status.
_UPDATE_JOB_STEPS_SQL = text(
    "UPDATE swing_job_runs SET steps = CAST(:steps AS JSONB) WHERE id = :id"
)
_LATEST_JOB_SQL = text(
    f"SELECT {_cols(_JOB_COLUMNS)} FROM swing_job_runs ORDER BY started_at DESC LIMIT 1"
)
_LAST_STEP_OK_SQL = text(
    """
    SELECT started_at FROM swing_job_runs
    WHERE status = 'ok' AND steps -> CAST(:step AS TEXT) ->> 'status' = 'ok'
    ORDER BY started_at DESC LIMIT 1
    """
)

_DIGEST_SENT_SQL = text("SELECT ok FROM swing_digests WHERE session = :session AND kind = :kind")
# A failed attempt can be replaced by a retry, but a digest that was sent is never overwritten.
_MARK_DIGEST_SQL = text(
    """
    INSERT INTO swing_digests (session, kind, sent_at, ok, detail)
    VALUES (:session, :kind, now(), :ok, :detail)
    ON CONFLICT (session, kind) DO UPDATE SET
        sent_at = EXCLUDED.sent_at,
        ok = EXCLUDED.ok,
        detail = EXCLUDED.detail
    WHERE NOT swing_digests.ok
    """
)


# --- helpers ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """``value`` as a float, with None, NaN and infinities as None (NULL, never NaN)."""
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(str(value))


def _dumps(value: Any) -> str:
    """JSON text for a JSONB parameter: NaN is null and an odd value is stringified."""
    try:
        safe = json_safe(value)
    except TypeError:
        safe = json_safe(json.loads(json.dumps(value, default=str)))
    return json.dumps(safe)


def _row(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """A database row as a plain dict, with stdlib UUIDs.

    JSONB columns (``payload``, ``steps``) are already parsed: SQLAlchemy's asyncpg dialect
    registers a JSON decoder on every connection, so they must not be decoded a second time.
    """
    out = dict(mapping)
    for key in _UUID_COLUMNS:
        if out.get(key) is not None:
            out[key] = uuid.UUID(str(out[key]))
    return out


async def _fetch(
    statement: TextClause, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        result = await session.execute(statement, dict(params or {}))
        return [_row(m) for m in result.mappings().all()]


def _check_columns(
    given: Mapping[str, Any], allowed: Sequence[str], what: str, *, extra: Sequence[str] = ()
) -> None:
    unknown = sorted(set(given) - set(allowed) - set(extra))
    if unknown:
        raise ValueError(f"{what} has no writable column(s) {unknown}")


# --- settings --------------------------------------------------------------------------


async def get_settings() -> SwingSettings:
    """The stored settings, or the defaults when none have been saved."""
    async with session_scope() as session:
        row = (await session.execute(_GET_SETTINGS_SQL)).mappings().first()
    return SwingSettings.model_validate(dict(row)) if row else SwingSettings()


async def put_settings(settings: SwingSettings) -> SwingSettings:
    """Replace the single settings row and return what was stored."""
    params = settings.model_dump()
    async with session_scope() as session:
        row = (await session.execute(_PUT_SETTINGS_SQL, params)).mappings().one()
    return SwingSettings.model_validate(dict(row))


# --- scans and ideas -------------------------------------------------------------------


def _idea_params(
    scan: ScanResult,
    target_session: date,
    published_at: datetime,
    reconstructed: bool,
    idea: Idea,
    *,
    watch_only: bool,
) -> dict[str, Any]:
    sizing = idea.sizing
    return {
        "session": scan.session,
        "target_session": target_session,
        "symbol": idea.symbol,
        "setup": idea.setup,
        "version": idea.version,
        "live": bool(idea.live),
        "watch_only": watch_only,
        "trigger": _num(idea.trigger),
        "stop": _num(idea.stop),
        "atr14": _num(idea.atr14),
        "rs_pct": _num(idea.rs_pct),
        "rank": int(idea.rank),
        "shares": int(sizing.shares),
        "risk_dollars": _num(sizing.risk_dollars),
        "position_value": _num(sizing.position_value),
        "overlaps": [str(s) for s in idea.overlaps],
        "published_at": published_at,
        "reconstructed": reconstructed,
    }


async def save_scan(
    scan: ScanResult, target_session: date, published_at: datetime, reconstructed: bool
) -> None:
    """Store a scan and its ideas (the ranked ones and the watch-only ones) in one transaction.

    A watch-only idea is stored at zero shares with ``watch_only`` set. Saving the same session
    again updates the scan and refreshes each idea's sizing, rank and live flag, but keeps the
    first ``published_at`` and ``reconstructed`` (and any tracking already written), so a repeat
    run, a manual run or a catch-up leaves one set of ideas per ``(session, symbol, setup)``.
    ``published_at`` must be timezone-aware.
    """
    if published_at.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    scan_params = {
        "session": scan.session,
        "target_session": target_session,
        "regime": str(scan.regime.value),
        "breadth": _num(scan.breadth),
        "universe_size": int(scan.universe_size),
        "reconstructed": reconstructed,
        "payload": _dumps(scan_to_dict(scan)),
    }
    rows = [
        _idea_params(scan, target_session, published_at, reconstructed, idea, watch_only=parked)
        for ideas, parked in ((scan.ideas, False), (scan.watch_only, True))
        for idea in ideas
    ]
    async with session_scope() as session:
        await session.execute(_UPSERT_SCAN_SQL, scan_params)
        if rows:
            await session.execute(_UPSERT_IDEA_SQL, rows)
    log.info(
        "swing.product.scan_saved",
        session=str(scan.session),
        ideas=len(scan.ideas),
        watch_only=len(scan.watch_only),
        reconstructed=reconstructed,
    )


async def latest_scan() -> dict[str, Any] | None:
    """The most recent scan row (``payload`` parsed), or None when nothing has been scanned."""
    rows = await _fetch(_LATEST_SCAN_SQL)
    return rows[0] if rows else None


async def scanned_sessions(since: date) -> set[date]:
    """The sessions on or after ``since`` that already have a stored scan."""
    async with session_scope() as session:
        rows = (await session.execute(_SCANNED_SESSIONS_SQL, {"since": since})).all()
    return {row[0] for row in rows}


async def trackable_ideas() -> list[dict[str, Any]]:
    """Ideas still waiting for a fill or still open (``track_status`` pending or open)."""
    return await _fetch(_TRACKABLE_IDEAS_SQL)


async def idea_rows(setup: str | None = None) -> list[dict[str, Any]]:
    """Every stored idea, oldest session first, or only those of one ``setup``."""
    if setup is None:
        return await _fetch(_ALL_IDEAS_SQL)
    return await _fetch(_SETUP_IDEAS_SQL, {"setup": setup})


async def update_idea_tracks(rows: Sequence[Mapping[str, Any]]) -> None:
    """Write forward-tracking results back to ideas.

    Each row has the idea's ``id`` and any of ``track_status, entry_date, entry_price,
    exit_date, exit_price, exit_reason, known_date, r_multiple, current_stop``. Only the keys
    given are written (None clears a field); anything else raises ``ValueError`` before the
    database is touched.
    """
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("id") is None:
            raise ValueError("each idea track row needs the idea's 'id'")
        _check_columns(row, _TRACK_COLUMNS, "an idea track row", extra=("id",))
        fields = tuple(c for c in _TRACK_COLUMNS if c in row)
        if not fields:
            continue
        params: dict[str, Any] = {"id": _uuid(row["id"])}
        for column in fields:
            params[column] = _num(row[column]) if column in _TRACK_FLOATS else row[column]
        groups.setdefault(fields, []).append(params)
    if not groups:
        return
    async with session_scope() as session:
        for fields, batch in groups.items():
            assignments = ", ".join(f"{c} = :{c}" for c in fields)
            statement = text(f"UPDATE swing_ideas SET {assignments} WHERE id = :id")
            await session.execute(statement, batch)


# --- positions -------------------------------------------------------------------------


def _position_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column, value in fields.items():
        if column in _POSITION_FLOATS:
            out[column] = _num(value)
        elif column == "idea_id":
            out[column] = _uuid(value)
        else:
            out[column] = value
    return out


async def list_positions(status: str | None) -> list[dict[str, Any]]:
    """Positions, newest first; only those with ``status`` (``open``/``closed``) when given."""
    if status is None:
        return await _fetch(_ALL_POSITIONS_SQL)
    return await _fetch(_POSITIONS_BY_STATUS_SQL, {"status": status})


async def get_position(position_id: uuid.UUID | str) -> dict[str, Any] | None:
    """One position by id, or None."""
    rows = await _fetch(_GET_POSITION_SQL, {"id": _uuid(position_id)})
    return rows[0] if rows else None


async def create_position(row: Mapping[str, Any]) -> dict[str, Any]:
    """Insert a position and return it.

    Required: ``symbol, opened_on, entry_price, shares, initial_stop``. ``current_stop`` defaults
    to the initial stop. Optional: ``trail_atr, idea_id, status, notes`` and the close and
    last-action columns. Anything else, or a missing required field, raises ``ValueError``.
    """
    _check_columns(row, _POSITION_WRITABLE, "a position")
    values = _position_values(row)
    missing = [c for c in _POSITION_REQUIRED if values.get(c) is None]
    if missing:
        raise ValueError(f"a position needs {missing}")
    if values.get("current_stop") is None:
        values["current_stop"] = values["initial_stop"]
    columns = [c for c in _POSITION_WRITABLE if c in values]
    statement = text(
        f"INSERT INTO swing_positions ({_cols(columns)}) VALUES ({_binds(columns)}) "
        f"RETURNING {_cols(_POSITION_COLUMNS)}"
    )
    async with session_scope() as session:
        result = await session.execute(statement, values)
        created = _row(result.mappings().one())
    log.info("swing.product.position_created", symbol=created["symbol"])
    return created


async def update_position(
    position_id: uuid.UUID | str, fields: Mapping[str, Any]
) -> dict[str, Any]:
    """Change the given columns of a position and return it; ``KeyError`` when it doesn't exist.

    ``id`` and ``created_at`` can't change; any other unknown column raises ``ValueError``.
    """
    _check_columns(fields, _POSITION_WRITABLE, "a position")
    pid = _uuid(position_id)
    if not fields:
        current = await get_position(position_id)
        if current is None:
            raise KeyError(position_id)
        return current
    values = _position_values(fields)
    assignments = ", ".join(f"{c} = :{c}" for c in values)
    statement = text(
        f"UPDATE swing_positions SET {assignments} WHERE id = :id "
        f"RETURNING {_cols(_POSITION_COLUMNS)}"
    )
    async with session_scope() as session:
        result = await session.execute(statement, {**values, "id": pid})
        found = result.mappings().first()
    if found is None:
        raise KeyError(position_id)
    return _row(found)


# --- job runs and digests --------------------------------------------------------------


async def start_job(kind: str, session_date: date | None) -> uuid.UUID:
    """Record that a job started (status ``running``) and return its id."""
    async with session_scope() as session:
        row = (await session.execute(_START_JOB_SQL, {"kind": kind, "session": session_date})).one()
    return uuid.UUID(str(row[0]))


async def finish_job(job_id: uuid.UUID | str, status: str, steps: Any, error: str | None) -> None:
    """Close a job run with its final status, its per-step log (any JSON-able value) and error."""
    params = {
        "id": _uuid(job_id),
        "status": status,
        "steps": None if steps is None else _dumps(steps),
        "error": error,
    }
    async with session_scope() as session:
        await session.execute(_FINISH_JOB_SQL, params)


async def update_job_steps(job_id: uuid.UUID | str, steps: Any) -> None:
    """Save a running job's per-step log (its progress); the status and times are left alone."""
    params = {"id": _uuid(job_id), "steps": None if steps is None else _dumps(steps)}
    async with session_scope() as session:
        await session.execute(_UPDATE_JOB_STEPS_SQL, params)


async def latest_job() -> dict[str, Any] | None:
    """The most recently started job run (``steps`` parsed), or None."""
    rows = await _fetch(_LATEST_JOB_SQL)
    return rows[0] if rows else None


async def last_step_ok(step: str) -> datetime | None:
    """When the latest successful job run (status ``ok``) whose ``step`` ran ok started, or None.

    A step that was skipped, or one inside a run that failed later, does not count.
    """
    async with session_scope() as session:
        row = (await session.execute(_LAST_STEP_OK_SQL, {"step": step})).first()
    return None if row is None else row[0]


async def digest_sent(session_date: date, kind: str) -> bool:
    """True when the digest of ``kind`` for the session went out. A failed send is not sent."""
    async with session_scope() as session:
        row = (
            await session.execute(_DIGEST_SENT_SQL, {"session": session_date, "kind": kind})
        ).first()
    return bool(row and row[0])


async def mark_digest(session_date: date, kind: str, ok: bool, detail: str | None) -> None:
    """Record a send attempt. A failure can be replaced by a later attempt; a success stays."""
    params = {"session": session_date, "kind": kind, "ok": bool(ok), "detail": detail}
    async with session_scope() as session:
        await session.execute(_MARK_DIGEST_SQL, params)
