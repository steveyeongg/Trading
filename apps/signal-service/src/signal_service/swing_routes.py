"""The swing product's HTTP routes: Today, a stock, positions, track record, settings, status.

These are the dashboard's routes for the rules-based swing engine (``swing_engine``). They read
what the nightly job stored and the committed validation reports, and they never run a scan or
a backtest inside a request:

* ``GET /v1/today`` reads the latest stored scan (its ideas carry their narration, PR13), the
  latest job run, the settings and the open positions. It needs no ``ScanContext``.
* ``GET /v1/stocks/{symbol}`` and the positions routes read the **context cache**
  (:mod:`signal_service.swing_context`): the job's target session's context, or one built in
  the background, once, off the event loop, from the stored bars of the latest scanned session.
  It is rebuilt only when the universe filters in Settings change and never goes back to an
  earlier session. No request waits for a cold build beyond a short bound: ``GET /v1/positions``
  not at all (no last close or R until it is ready), a position write
  :data:`~signal_service.swing_context.WRITE_WAIT_S` (then it saves without advice and closes
  without the split check, and the nightly job advises it), a stock page
  :data:`~signal_service.swing_context.STOCK_WAIT_S` (then 503 "Loading market data...").
* ``GET /v1/track-record`` reads ``ml/validation`` as Phase 1 writes it, plus the stored ideas'
  forward tracking for the live record (:mod:`signal_service.swing_track_record`).
* An idea is Live only when its scan said so **and** its family's verdict is Live now.
* ``POST /v1/scan/run`` starts the same job the scheduler runs, in the background.

Every number on these pages is a rule's output or a measured historical frequency; nothing is
called a confidence or a probability, nothing promises a profit, every idea is long-only, and
nothing here places an order (``auto_execution`` reports the API's startup flag, off by default).

**Single user.** The swing tables (migration 0010) have no ``user_id``: one installation, one
account. Like the other ``/v1`` data routes in :mod:`signal_service.routes`, these take no auth
dependency.

**Only the dashboard writes.** Every ``POST``, ``PUT`` or ``PATCH`` under ``/v1/positions``,
``/v1/settings`` and ``/v1/scan/run`` needs the header ``X-ATLAS-Client: web`` (the dashboard's
client sends it), and answers 403 without it. A page on another site can make a browser send a
simple cross-site form post to localhost, but not a custom header, so a drive-by page cannot
record a position or start a scan. Reads need no header.

**Failures.** A database that cannot be reached answers 503 with a readable reason (every route
here goes through :class:`_SwingRoute`), and every 422 has FastAPI's validation shape, ``detail``
a list of ``{loc, msg, type}``. Responses pass through :func:`atlas_shared.to_jsonable`, with NaN
and infinities as null and dates in ISO format.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import os
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
from alert_service.channels.base import redact_secrets
from atlas_shared import to_jsonable
from atlas_shared.logging import get_logger
from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError
from swing_engine import job, product_store
from swing_engine import store as swing_bars
from swing_engine.checklist import dollar_volume_rule, eligibility_checklist
from swing_engine.config import BreakoutParams, PullbackParams, UniverseFilter
from swing_engine.digest import DISCLAIMER, REGIME_LABEL, send_digest
from swing_engine.indicators import compute_indicators
from swing_engine.job import JobDeps, ProductStore, safe_reason
from swing_engine.loader import SCAN_WINDOW_DAYS
from swing_engine.narrate import SETUP_LABEL
from swing_engine.panel import Panel, build_panel
from swing_engine.positions import (
    exit_params,
    nightly_update,
    open_risk,
    rescaled,
    resolve_exit_rules,
    split_ratio,
    stored_action,
)
from swing_engine.regime import Regime
from swing_engine.scan import ScanContext, default_setups
from swing_engine.scheduler import scheduler_state
from swing_engine.settings import SwingSettings
from swing_engine.validation.protocol import FAMILIES

from signal_service import swing_context, swing_deps
from signal_service.swing_context import (
    LOADING_DETAIL,
    context_within,
    invalidate_context,
    is_building,
    last_failure,
)
from signal_service.swing_track_record import (
    FamilyRecord,
    Verdict,
    as_float,
    as_int,
    family_out,
    idea_record,
    read_families,
)

log = get_logger("swing.api")

# --- what the routes reach outside themselves (module attributes, so tests can swap them) -------

store: Any = product_store
bars_store: Any = swing_bars
run_job = job.run_job
make_swing_deps = swing_deps.make_swing_deps
repo_root = swing_deps.repo_root

_AUTO_EXECUTION = os.environ.get("ATLAS_ENABLE_AUTO_EXECUTION", "0") == "1"
"""The flag the API started with: read once at import, from the shell environment, exactly as
``main._AUTO_EXEC_ENABLED`` (which decides whether the position monitor runs) reads it."""

BARS_SHOWN = 300
"""Daily bars on a stock page."""

ASSET_NAMES_TTL_S = 600.0
"""How long the symbol-to-name map is kept (the asset list changes weekly)."""

ASSET_MISS_RELOAD_S = 60.0
"""A symbol missing from a map at least this old reloads it once (a listing added tonight)."""

JOB_ID_WAIT_S = 20.0
"""How long "Run scan now" waits for the job's row (the calendar is fetched first). Under the
dashboard proxy's timeout of about 30 seconds."""

TELEGRAM_TEST_TEXT = (
    "ATLAS test message: the nightly digest will arrive in this chat.\n" + DISCLAIMER
)
"""The fixed text ``POST /v1/settings/telegram-test`` sends."""

_SECRET_DETAIL_CHARS = 300


# --- errors -----------------------------------------------------------------------------------

_DB_ERRORS: tuple[type[BaseException], ...] = (OSError, SQLAlchemyError)
with contextlib.suppress(ImportError):  # asyncpg raises its own errors before SQLAlchemy wraps them
    import asyncpg

    _DB_ERRORS = (*_DB_ERRORS, asyncpg.PostgresError)


CLIENT_HEADER = "X-ATLAS-Client"
"""The header every mutating swing request must carry, with the value :data:`CLIENT_VALUE`."""
CLIENT_VALUE = "web"
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_GUARDED_PATHS = ("/v1/positions", "/v1/settings", "/v1/scan/run")


def _needs_client_header(request: Request) -> bool:
    """A write to the positions, settings or scan routes without ``X-ATLAS-Client: web``."""
    path = request.url.path
    guarded = any(path == p or path.startswith(f"{p}/") for p in _GUARDED_PATHS)
    return (
        request.method in _MUTATING
        and guarded
        and request.headers.get(CLIENT_HEADER) != CLIENT_VALUE
    )


class _SwingRoute(APIRoute):
    """Answers 403 to a write without the dashboard's header (see the module docstring), and 503
    with a readable reason when the database cannot be reached or read."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if _needs_client_header(request):
                return JSONResponse(
                    status_code=403, content={"detail": f"missing {CLIENT_HEADER} header"}
                )
            try:
                return await handler(request)
            except _DB_ERRORS as exc:
                reason = safe_reason(exc)
                log.warning("swing.api.database_unreachable", path=request.url.path, error=reason)
                return JSONResponse(
                    status_code=503, content={"detail": f"Can't reach the ATLAS database: {reason}"}
                )

        return guarded


router = APIRouter(prefix="/v1", tags=["swing"], route_class=_SwingRoute)


def _invalid(field: str, message: str, value: Any = None) -> RequestValidationError:
    """A 422 in FastAPI's own shape for a body field checked in the route."""
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", field), "msg": message, "input": value}]
    )


# --- JSON -------------------------------------------------------------------------------------


def _finite(value: Any) -> Any:
    """Plain values: NaN and infinities as None, numpy scalars as Python, UUIDs as strings."""
    if isinstance(value, Mapping):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _json(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A response body: ``to_jsonable`` (no ``Decimal``), then NaN and infinities as null."""
    cleaned: dict[str, Any] = _finite(to_jsonable([dict(payload)])[0])
    return cleaned


# --- response models --------------------------------------------------------------------------


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdeaTrackRecord(_Out):
    verdict: Verdict
    verdict_text: str
    win_rate: float | None
    expectancy_r: float | None
    trades: int | None
    trades_distinct: int | None
    source: Literal["oos", "insample"] | None


class IdeaOut(_Out):
    id: str | None
    symbol: str
    name: str | None
    setup: str
    setup_label: str
    version: str
    buy_above: float | None
    stop: float | None
    target_2r: float | None
    shares: int
    risk_dollars: float | None
    position_value: float | None
    rs_pct: float | None
    rank: int
    live: bool
    watch_only: bool
    regime_warning: bool
    overlaps: list[str]
    already_held: bool
    why: str | None
    cancels: str | None
    track_record: IdeaTrackRecord


class FreshnessOut(_Out):
    data_as_of: date | None
    ideas_for: date | None
    next_scan_at: datetime | None
    last_scan_status: Literal["ok", "failed", "running", "never"]
    failed_step: str | None
    failed_reason: str | None


class RegimeOut(_Out):
    state: Literal["uptrend", "mixed", "downtrend"]
    factor: float | None
    breadth: float | None
    label: str


class ActionOut(_Out):
    position_id: str
    symbol: str
    kind: Literal["hold", "raise_stop", "stop_hit", "max_hold", "no_data"]
    stop: float | None
    text: str


class CapacityOut(_Out):
    open_positions: int
    max_positions: int
    open_risk_dollars: float
    max_risk_dollars: float


class ProgressOut(_Out):
    done: int
    total: int


class PartialDataOut(_Out):
    missing: int
    expected: int


class TodayOut(_Out):
    freshness: FreshnessOut
    regime: RegimeOut | None
    ideas: list[IdeaOut]
    unproven: list[IdeaOut]
    watch_only: list[IdeaOut]
    positions: list[ActionOut]
    capacity: CapacityOut
    empty_reason: Literal["first_run", "downtrend", "no_candidates", "last_scan_failed"] | None
    backfill: ProgressOut | None
    # The step the latest run is in while it runs (``assets``, ``bars``, ...; null between steps,
    # while it reads the calendar, and when no run is running).
    running_step: str | None
    # Why automatic scans are off (no Alpaca keys, ATLAS_DISABLE_SCHEDULER=1, ...), or null.
    disabled_reason: str | None
    # The latest scan was saved from incomplete bars: how many of the stocks that traded the
    # session before had no bar on its session (null when the scan had them all).
    partial_data: PartialDataOut | None
    # Whether any setup family's verdict is Live now (read from the committed validation files,
    # like each idea's ``live``): "no setup has passed testing" is true exactly when this is False.
    any_live_setup: bool


class BarOut(_Out):
    time: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None


class PointOut(_Out):
    time: date
    value: float


class LinesOut(_Out):
    sma50: list[PointOut]
    sma200: list[PointOut]
    ema20: list[PointOut]


class CheckOut(_Out):
    rule: str
    passed: bool
    value: str
    threshold: str


class LevelsOut(_Out):
    trigger: float | None
    stop: float | None
    target_2r: float | None
    trail: float | None


class StockOut(_Out):
    symbol: str
    name: str | None
    session: date
    bars: list[BarOut]
    lines: LinesOut
    checklist: list[CheckOut]
    idea: IdeaOut | None
    why: str | None
    cancels: str | None
    levels: LevelsOut | None


class StatsOut(_Out):
    n: int
    n_distinct: int | None
    win_rate: float | None
    avg_win_r: float | None
    avg_loss_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    avg_hold: float | None
    open: int | None


class VersionOut(_Out):
    version: str
    run_index: int | None
    freeze_date: date | None
    passed: bool | None


class BaselineOut(_Out):
    median: float | None
    p5: float | None
    p95: float | None


class EquityOut(_Out):
    strategy: list[PointOut]
    spy: list[PointOut]


class PortfolioStatsOut(_Out):
    cagr: float | None
    sharpe: float | None
    max_dd: float | None
    exposure: float | None
    trades: int | None


class PortfolioOut(_Out):
    strategy: PortfolioStatsOut | None
    spy: PortfolioStatsOut | None
    combined: PortfolioStatsOut | None
    combined_note: str | None


class FamilyOut(_Out):
    family: str
    label: str
    verdict: Verdict
    verdict_text: str
    failures: list[str]
    versions: list[VersionOut]
    insample: StatsOut | None
    oos: StatsOut | None
    oos_distinct: StatsOut | None
    live: StatsOut
    reconstructed: StatsOut
    baseline: BaselineOut | None
    equity: EquityOut
    portfolio: PortfolioOut
    warnings: list[str]


class TrackRecordOut(_Out):
    families: list[FamilyOut]


class PositionOut(_Out):
    id: str
    symbol: str
    name: str | None
    opened_on: date
    entry_price: float
    shares: float
    initial_stop: float
    current_stop: float | None
    status: str
    last_close: float | None
    r_now: float | None
    last_action: str | None
    last_action_text: str | None
    last_action_on: date | None
    closed_on: date | None
    exit_price: float | None
    exit_reason: str | None
    r_multiple: float | None
    held_sessions: int | None
    idea_id: str | None
    notes: str | None


class ScanRunOut(_Out):
    job_id: str | None
    starting: bool


class TelegramTestOut(_Out):
    ok: bool
    detail: str


class LastScanOut(_Out):
    session: date | None
    kind: str
    started_at: datetime | None
    generated_at: datetime | None
    status: str
    steps: dict[str, Any]
    error: str | None


class StatusOut(_Out):
    data_as_of: date | None
    last_scan: LastScanOut | None
    next_scan_at: datetime | None
    backfill: ProgressOut | None
    alpaca_configured: bool
    telegram_configured: bool
    auto_execution: bool
    scheduler_running: bool
    disabled_reason: str | None


class DetailOut(_Out):
    detail: str


# --- request bodies ---------------------------------------------------------------------------

_SYMBOL = r"^[A-Za-z0-9.\-]{1,16}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PositionIn(_In):
    """A trade the user placed. ``initial_stop`` defaults to the linked idea's stop."""

    symbol: str = Field(pattern=_SYMBOL)
    opened_on: date
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    shares: float = Field(gt=0, allow_inf_nan=False)
    initial_stop: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    idea_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _stop_under_entry(self) -> PositionIn:
        if self.initial_stop is not None and self.initial_stop >= self.entry_price:
            raise ValueError("the stop must be below the entry price")
        return self


class PositionPatch(_In):
    current_stop: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    shares: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("current_stop", "shares")
    @classmethod
    def _not_null(cls, value: float | None) -> float | None:
        if value is None:
            raise ValueError("leave the field out to keep it; null is not a value")
        return value


class CloseIn(_In):
    closed_on: date
    exit_price: float = Field(gt=0, allow_inf_nan=False)
    exit_reason: str | None = Field(default=None, max_length=200)


class SettingsIn(_In):
    """Any of the settings; fields left out keep their stored values. The bounds are
    :class:`~swing_engine.settings.SwingSettings`'s, checked on the merged settings."""

    equity: float | None = None
    risk_pct: float | None = None
    max_position_pct: float | None = None
    max_positions: int | None = None
    max_total_risk_pct: float | None = None
    min_price: float | None = None
    min_dollar_volume: float | None = None
    telegram_digest: bool | None = None


# --- asset names ------------------------------------------------------------------------------


@dataclass
class _AssetNames:
    names: dict[str, str | None] | None = None
    loaded: float = 0.0


_ASSETS = _AssetNames()


async def _asset_names(symbol: str | None = None) -> dict[str, str | None]:
    """Every known symbol and its company name, from the assets table (cached)."""
    cache = _ASSETS
    age = time.monotonic() - cache.loaded
    stale = cache.names is None or age > ASSET_NAMES_TTL_S
    missing = symbol is not None and cache.names is not None and symbol not in cache.names
    if stale or (missing and age > ASSET_MISS_RELOAD_S):
        assets = await bars_store.load_assets()
        names: dict[str, str | None] = {}
        if not assets.empty:
            for sym, name in zip(assets["symbol"], assets["name"], strict=True):
                names[str(sym)] = None if pd.isna(name) else str(name)
        cache.names, cache.loaded = names, time.monotonic()
    return cache.names or {}


def _symbol(text: str) -> str:
    return text.strip().upper()


# --- ideas ------------------------------------------------------------------------------------


def _idea_out(
    row: Mapping[str, Any],
    *,
    watch_only: bool,
    names: Mapping[str, str | None],
    ids: Mapping[tuple[str, str], str],
    held: set[str],
    families: Mapping[str, FamilyRecord],
) -> dict[str, Any]:
    """One stored idea (a scan payload row) as the dashboard shows it."""
    symbol, setup = str(row["symbol"]), str(row["setup"])
    trigger, stop = as_float(row.get("trigger")), as_float(row.get("stop"))
    sizing = row.get("sizing") or {}
    family = families.get(setup)
    # Live is the scan's flag and the family's verdict now: a setup whose verdict was withdrawn
    # since the scan (its rules or files changed) shows its ideas as unproven.
    live = bool(row.get("live")) and family is not None and family.verdict == "live"
    return {
        "id": ids.get((symbol, setup)),
        "symbol": symbol,
        "name": names.get(symbol),
        "setup": setup,
        "setup_label": SETUP_LABEL.get(setup, setup.capitalize()),
        "version": str(row.get("version") or ""),
        "buy_above": trigger,
        "stop": stop,
        "target_2r": None if trigger is None or stop is None else trigger + 2 * (trigger - stop),
        "shares": as_int(sizing.get("shares")) or 0,
        "risk_dollars": as_float(sizing.get("risk_dollars")),
        "position_value": as_float(sizing.get("position_value")),
        "rs_pct": as_float(row.get("rs_pct")),
        "rank": as_int(row.get("rank")) or 0,
        "live": live,
        "watch_only": watch_only,
        "regime_warning": bool(row.get("regime_warning")),
        "overlaps": [str(s) for s in row.get("overlaps") or []],
        "already_held": symbol.upper() in held,
        "why": row.get("why"),
        "cancels": row.get("cancels"),
        "track_record": idea_record(family),
    }


async def _idea_ids(session: date) -> dict[tuple[str, str], str]:
    return {
        (str(r["symbol"]), str(r["setup"])): str(r["id"])
        for r in await store.session_ideas(session)
    }


def _held(positions: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(p["symbol"]).upper() for p in positions}


# --- the latest job ---------------------------------------------------------------------------


def _job_status(run: Mapping[str, Any] | None) -> Literal["ok", "failed", "running", "never"]:
    if run is None:
        return "never"
    status = str(run.get("status"))
    if status in ("ok", "running"):
        return cast(Literal["ok", "running"], status)
    return "failed"


def _failure(run: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """The step a failed run stopped at and why, from its step log (else its error line)."""
    if _job_status(run) != "failed" or run is None:
        return None, None
    for name, info in (run.get("steps") or {}).items():
        if isinstance(info, Mapping) and info.get("status") == "failed":
            return str(name), None if info.get("error") is None else str(info["error"])
    error = run.get("error")
    if isinstance(error, str) and ": " in error:
        step, reason = error.split(": ", 1)
        return step, reason
    return None, None if error is None else str(error)


def _backfill(run: Mapping[str, Any] | None) -> dict[str, int] | None:
    """The backfill's ``{done, total}`` while that run's bars step is downloading; otherwise None
    (an incremental update reports no progress, and a finished bars step is no download)."""
    if run is None or run.get("status") != "running":
        return None
    bars = (run.get("steps") or {}).get("bars")
    if not isinstance(bars, Mapping) or bars.get("status") != "running":
        return None
    done, total = as_int(bars.get("done")), as_int(bars.get("total"))
    return None if done is None or total is None else {"done": done, "total": total}


def _partial(
    run: Mapping[str, Any] | None, scan: Mapping[str, Any] | None
) -> dict[str, int] | None:
    """``{missing, expected}`` when the latest run's scan step flagged the latest scan
    ``partial`` (too few stocks had a bar on its session); otherwise None."""
    if run is None or scan is None or run.get("session") != scan.get("session"):
        return None
    info = (run.get("steps") or {}).get("scan")
    if not isinstance(info, Mapping) or info.get("partial") is not True:
        return None
    missing, expected = as_int(info.get("missing")), as_int(info.get("expected"))
    return None if missing is None or expected is None else {"missing": missing, "expected": expected}


AFTER_SCAN_STEPS = frozenset({"tracking", "positions", "digest"})
"""Steps that run after tonight's scan is saved: a failure in one of them still shows tonight's
scan (Today says which step failed), unlike a failure before it (``last_scan_failed``)."""


def _running_step(run: Mapping[str, Any] | None) -> str | None:
    """The step a running run is in (the first one recorded as running), else None."""
    if run is None or run.get("status") != "running":
        return None
    for name, info in (run.get("steps") or {}).items():
        if isinstance(info, Mapping) and info.get("status") == "running":
            return str(name)
    return None


# --- routes: Today ----------------------------------------------------------------------------


@router.get("/today", response_model=TodayOut)
async def today() -> dict[str, Any]:
    """The latest scan's ideas (validated, unproven, watch-only), position actions and capacity."""
    scan = await store.latest_scan()
    run = await store.latest_job()
    settings = await store.get_settings()
    positions = await store.list_positions("open")
    names = await _asset_names()
    ids = {} if scan is None else await _idea_ids(scan["session"])
    families = read_families(repo_root())
    held = _held(positions)
    payload: Mapping[str, Any] = (scan or {}).get("payload") or {}

    def outs(rows: Any, parked: bool) -> list[dict[str, Any]]:
        return [
            _idea_out(r, watch_only=parked, names=names, ids=ids, held=held, families=families)
            for r in rows or []
        ]

    ranked = outs(payload.get("ideas"), parked=False)
    ideas = [i for i in ranked if i["live"]]
    unproven = [i for i in ranked if not i["live"]]
    watch_only = outs(payload.get("watch_only"), parked=True)

    regime = None
    state = None if scan is None else str(payload.get("regime") or scan.get("regime"))
    if state in {r.value for r in Regime}:
        regime = {
            "state": state,
            "factor": as_float(payload.get("regime_factor")),
            "breadth": as_float(payload.get("breadth", scan.get("breadth") if scan else None)),
            "label": REGIME_LABEL[Regime(state)],
        }

    status = _job_status(run)
    step, reason = _failure(run)
    sched = scheduler_state()
    if scan is None:
        empty: str | None = "first_run"
    elif status == "failed" and step not in AFTER_SCAN_STEPS:
        # Tonight's scan was not saved (assets, bars, the scan itself, or a pre-step such as the
        # calendar or the settings, or a step not recorded): the ideas shown are the previous ones.
        empty = "last_scan_failed"
    elif state == Regime.DOWNTREND.value:
        empty = "downtrend"
    elif not (ideas or unproven or watch_only):
        empty = "no_candidates"
    else:
        empty = None

    actions = []
    for pos in positions:
        action = stored_action(pos)
        if action is not None:
            actions.append(
                {
                    "position_id": action.position_id,
                    "symbol": action.symbol,
                    "kind": action.kind.value,
                    "stop": action.stop,
                    "text": action.text,
                }
            )

    return _json(
        {
            "freshness": {
                "data_as_of": None if scan is None else scan["session"],
                "ideas_for": None if scan is None else scan.get("target_session"),
                "next_scan_at": sched["next_scan_at"],
                "last_scan_status": status,
                "failed_step": step,
                "failed_reason": reason,
            },
            "regime": regime,
            "ideas": ideas,
            "unproven": unproven,
            "watch_only": watch_only,
            "positions": actions,
            "capacity": {
                "open_positions": len(positions),
                "max_positions": settings.max_positions,
                "open_risk_dollars": float(sum(open_risk(p) for p in positions)),
                "max_risk_dollars": settings.equity * settings.max_total_risk_pct,
            },
            "empty_reason": empty,
            "backfill": _backfill(run),
            "running_step": _running_step(run),
            "disabled_reason": None if sched["running"] else sched["disabled_reason"],
            "partial_data": _partial(run, scan),
            "any_live_setup": any(f.verdict == "live" for f in families.values()),
        }
    )


# --- routes: a stock --------------------------------------------------------------------------

_FIELDS = ("open", "high", "low", "close", "volume")
_LINES = ("sma50", "sma200", "ema20")


def _setup_params(repo: Path) -> tuple[BreakoutParams | None, PullbackParams | None]:
    """The pre-registered parameters the ideas were scanned with (``default_setups``)."""
    breakout = pullback = None
    for spec in default_setups(repo):
        if isinstance(spec.params, BreakoutParams):
            breakout = spec.params
        elif isinstance(spec.params, PullbackParams):
            pullback = spec.params
    return breakout, pullback


def _chart(panel: Panel, lines: Mapping[str, pd.DataFrame], symbol: str) -> dict[str, Any]:
    """The last :data:`BARS_SHOWN` bars of ``symbol`` and the indicator lines over them."""
    close = panel.close[symbol].dropna()
    days = close.index[-BARS_SHOWN:]
    frames = {f: getattr(panel, f)[symbol].reindex(days) for f in _FIELDS}
    bars = [
        {"time": day.date(), **{f: as_float(frames[f].at[day]) for f in _FIELDS}} for day in days
    ]
    out: dict[str, list[dict[str, Any]]] = {}
    for name in _LINES:
        series = lines[name][symbol].reindex(days)
        out[name] = [
            {"time": day.date(), "value": float(v)}
            for day, v in series.items()
            if v is not None and math.isfinite(v)
        ]
    return {"bars": bars, "lines": out}


async def _stored_chart(
    symbol: str, ctx: ScanContext, universe: UniverseFilter
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The chart and the dollar-volume rule of a known stock the cached panel lacks (below the
    liquidity prefilter), from its stored adjusted bars on the market's sessions, so a missing
    day stays a gap and the lines start over after a long one, as the engine computes them."""
    session = ctx.session.date()
    start = session - timedelta(days=SCAN_WINDOW_DAYS)
    rows = await bars_store.load_daily_bars([symbol], start, session)
    days = ctx.panel.close.index[ctx.panel.close.index >= pd.Timestamp(start)]
    if rows.empty:
        blank = pd.DataFrame({symbol: np.nan}, index=days)
        panel = Panel(**{f: blank.copy() for f in _FIELDS})
    else:
        built = build_panel(rows)
        panel = Panel(
            **{f: getattr(built, f).reindex(index=days, columns=[symbol]) for f in _FIELDS}
        )
    ind = compute_indicators(panel)
    chart = _chart(panel, {name: getattr(ind, name) for name in _LINES}, symbol)
    rule = dollar_volume_rule(dataclasses.replace(ctx, panel=panel), symbol, universe)
    return chart, dataclasses.asdict(rule)


def _levels(idea: Mapping[str, Any] | None, pos: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The chart's price lines: the idea's buy-above, stop and 2R; the trail of a held stock."""
    if idea is None and pos is None:
        return None
    trail = None if pos is None else as_float(pos.get("current_stop"))
    if idea is not None:
        return {
            "trigger": idea["buy_above"],
            "stop": idea["stop"],
            "target_2r": idea["target_2r"],
            "trail": trail,
        }
    assert pos is not None
    entry, stop = float(pos["entry_price"]), float(pos["initial_stop"])
    return {"trigger": None, "stop": stop, "target_2r": entry + 2 * (entry - stop), "trail": trail}


@router.get("/stocks/{symbol}", response_model=StockOut, responses={404: {"model": DetailOut}})
async def stock(symbol: str) -> dict[str, Any]:
    """One stock: daily bars and lines, the eligibility checklist, the idea if it has one."""
    sym = _symbol(symbol)
    names = await _asset_names(sym)
    if sym not in names:
        raise HTTPException(404, f"unknown symbol {sym}: it is not in the asset list")
    cached = await context_within(swing_context.STOCK_WAIT_S)
    scan = await store.latest_scan()
    if cached is None:
        failure = last_failure()
        if is_building():
            raise HTTPException(503, LOADING_DETAIL)
        if scan is None or failure is None:
            raise HTTPException(
                503,
                "No scan data is loaded yet: stock pages need a completed scan of the stored bars.",
            )
        raise HTTPException(503, f"Couldn't load scan data: {failure}")
    ctx, universe = cached
    positions = await store.list_positions("open")
    pos = next((p for p in positions if str(p["symbol"]).upper() == sym), None)

    idea = None
    if scan is not None:
        payload = scan.get("payload") or {}
        rows = [(r, False) for r in payload.get("ideas") or []]
        rows += [(r, True) for r in payload.get("watch_only") or []]
        mine = [(r, parked) for r, parked in rows if str(r["symbol"]).upper() == sym]
        if mine:
            row, parked = min(mine, key=lambda rp: (rp[1], as_int(rp[0].get("rank")) or 0))
            idea = _idea_out(
                row,
                watch_only=parked,
                names=names,
                ids=await _idea_ids(scan["session"]),
                held=_held(positions),
                families=read_families(repo_root()),
            )

    if sym in ctx.panel.close.columns:
        chart = _chart(ctx.panel, {name: getattr(ctx.ind, name) for name in _LINES}, sym)
        breakout, pullback = _setup_params(repo_root())
        checklist = [
            dataclasses.asdict(item)
            for item in eligibility_checklist(
                ctx, sym, universe, breakout=breakout, pullback=pullback
            )
        ]
    else:
        chart, item = await _stored_chart(sym, ctx, universe)
        checklist = [item]

    return _json(
        {
            "symbol": sym,
            "name": names.get(sym),
            "session": ctx.session.date(),
            **chart,
            "checklist": checklist,
            "idea": idea,
            "why": None if idea is None else idea["why"],
            "cancels": None if idea is None else idea["cancels"],
            "levels": _levels(idea, pos),
        }
    )


# --- routes: positions ------------------------------------------------------------------------


def _position_out(
    pos: Mapping[str, Any], ctx: ScanContext | None, names: Mapping[str, str | None]
) -> dict[str, Any]:
    """A position row with its name and, from the cached panel, its last close, its R now (open
    positions) and the sessions it has been held (sessions with a bar, the open day included)."""
    symbol = str(pos["symbol"])
    is_open = pos.get("status") == "open"
    last_close = r_now = held = None
    if ctx is not None and symbol in ctx.panel.close.columns:
        closes = ctx.panel.close[symbol].loc[: ctx.session]
        opened = pd.Timestamp(pos["opened_on"]).normalize()
        end = ctx.session if is_open else pd.Timestamp(pos.get("closed_on") or ctx.session)
        if len(closes.index) and opened >= closes.index[0] and end <= ctx.session:
            held = int(closes.loc[opened:end].notna().sum())
        if is_open:
            priced = closes.dropna()
            last_close = float(priced.iloc[-1]) if len(priced) else None
            entry, stop = float(pos["entry_price"]), float(pos["initial_stop"])
            if last_close is not None and entry > stop:
                r_now = (last_close - entry) / (entry - stop)
    return {
        "id": pos["id"],
        "symbol": symbol,
        "name": names.get(symbol),
        "opened_on": pos["opened_on"],
        "entry_price": pos["entry_price"],
        "shares": pos["shares"],
        "initial_stop": pos["initial_stop"],
        "current_stop": pos.get("current_stop"),
        "status": pos.get("status"),
        "last_close": last_close,
        "r_now": r_now,
        "last_action": pos.get("last_action"),
        "last_action_text": pos.get("last_action_text"),
        "last_action_on": pos.get("last_action_on"),
        "closed_on": pos.get("closed_on"),
        "exit_price": pos.get("exit_price"),
        "exit_reason": pos.get("exit_reason"),
        "r_multiple": pos.get("r_multiple"),
        "held_sessions": held,
        "idea_id": pos.get("idea_id"),
        "notes": pos.get("notes"),
    }


async def _write_context() -> ScanContext | None:
    """The context for a position write, waiting at most ``WRITE_WAIT_S`` for a cold build."""
    cached = await context_within(swing_context.WRITE_WAIT_S)
    return None if cached is None else cached[0]


async def _position_response(pos: Mapping[str, Any], ctx: ScanContext | None) -> dict[str, Any]:
    names = await _asset_names()
    return _json(_position_out(pos, ctx, names))


async def _advise(
    pos: dict[str, Any], ctx: ScanContext | None, *, check_stop: bool = True
) -> dict[str, Any]:
    """Tonight's advice for ``pos`` on the context ``ctx``, saved, as the nightly job saves it.

    The position is judged on the context's session alone (as one never advised). Without a
    context (none yet, or not within the write's wait), or for a symbol the cached panel lacks
    (the nightly job loads it), it is returned unadvised: advice from no bars would read as "no
    price data". It is also left as it is when it was advised for a later session than the
    context's.
    """
    if ctx is None or str(pos["symbol"]) not in ctx.panel.close.columns:
        return pos
    last = pos.get("last_action_on")
    if last is not None and ctx.session.date() < pd.Timestamp(last).date():
        # The job advised it for a later session than the cache holds (it ran elsewhere): advice
        # on the older session would overwrite that, and the next nightly would walk from it.
        log.info("swing.api.advice_skipped", symbol=str(pos["symbol"]), cached=str(ctx.session))
        return pos
    ruled, max_hold = await resolve_exit_rules(pos, store.get_idea, repo_root())
    update = nightly_update(
        {**ruled, "last_action_on": None}, ctx, max_hold, check_stop=check_stop
    )
    if not update.fields:
        return pos
    saved: dict[str, Any] = await store.update_position(pos["id"], update.fields)
    return saved


_CREATE_LOCK = asyncio.Lock()
"""Serialises "is there an open position in this symbol?" and the insert (one API process)."""


async def _get_position(position_id: uuid.UUID) -> dict[str, Any]:
    pos = await store.get_position(position_id)
    if pos is None:
        raise HTTPException(404, f"no position {position_id}")
    found: dict[str, Any] = pos
    return found


@router.get("/positions", response_model=list[PositionOut])
async def positions(
    status: Literal["open", "closed"] | None = Query(None),
) -> list[dict[str, Any]]:
    """The user's positions, newest first (only ``open`` or ``closed`` ones when asked)."""
    rows = await store.list_positions(status)
    names = await _asset_names()
    cached = await context_within(0)  # never waits: a cold cache starts its build and shows none
    ctx = None if cached is None else cached[0]
    return [_json(_position_out(p, ctx, names)) for p in rows]


@router.post(
    "/positions",
    status_code=201,
    response_model=PositionOut,
    responses={409: {"model": DetailOut}},
)
async def create_position(body: PositionIn) -> dict[str, Any]:
    """Record a trade the user placed, then advise it at once on the cached context.

    The position is saved with the ``trail_atr`` its exit rules follow: the linked idea's setup's
    pre-registered one, or the breakout family's when no idea is linked
    (:func:`~swing_engine.positions.exit_params`)."""
    symbol = _symbol(body.symbol)
    if symbol not in await _asset_names(symbol):
        raise _invalid("symbol", f"unknown symbol {symbol}: it is not in the asset list", symbol)
    stop = body.initial_stop
    idea = None
    if body.idea_id is not None:
        idea = await store.get_idea(body.idea_id)
        if idea is None:
            raise _invalid("idea_id", f"no idea {body.idea_id}", str(body.idea_id))
        if str(idea["symbol"]).upper() != symbol:
            raise _invalid("idea_id", f"that idea is for {idea['symbol']}, not {symbol}")
        if stop is None:
            stop = as_float(idea.get("stop"))
    # The trail the idea was narrated and is tracked with: its setup's pre-registered one, or the
    # breakout family's for a trade recorded without an idea (never the code's default).
    trail_atr = exit_params(idea, repo_root()).trail_atr
    if stop is None:
        raise _invalid("initial_stop", "give the stop, or the idea the trade came from")
    if stop >= body.entry_price:
        raise _invalid("initial_stop", "the stop must be below the entry price", stop)
    async with _CREATE_LOCK:  # one open position per symbol: the check and the insert together
        if any(str(p["symbol"]).upper() == symbol for p in await store.list_positions("open")):
            raise HTTPException(409, f"you already have an open position in {symbol}")
        created = await store.create_position(
            {
                "symbol": symbol,
                "opened_on": body.opened_on,
                "entry_price": body.entry_price,
                "shares": body.shares,
                "initial_stop": stop,
                "current_stop": stop,
                "trail_atr": trail_atr,
                "idea_id": body.idea_id,
                "notes": body.notes,
            }
        )
    ctx = await _write_context()
    return await _position_response(await _advise(created, ctx), ctx)


@router.patch("/positions/{position_id}", response_model=PositionOut)
async def update_position(position_id: uuid.UUID, body: PositionPatch) -> dict[str, Any]:
    """Change a position's stop, shares or notes. A changed stop is re-advised at once."""
    pos = await _get_position(position_id)
    fields = body.model_dump(exclude_unset=True)
    updated = await store.update_position(position_id, fields) if fields else pos
    stop_changed = "current_stop" in fields and fields["current_stop"] != pos.get("current_stop")
    ctx = await _write_context()
    if stop_changed and updated.get("status") == "open":
        # The new stop was set after the close: tonight's low says nothing about it.
        updated = await _advise(updated, ctx, check_stop=False)
    return await _position_response(updated, ctx)


@router.post(
    "/positions/{position_id}/close",
    response_model=PositionOut,
    responses={404: {"model": DetailOut}, 409: {"model": DetailOut}},
)
async def close_position(position_id: uuid.UUID, body: CloseIn) -> dict[str, Any]:
    """Close a position at the price the user got; ``r_multiple`` is the result in R."""
    pos = await _get_position(position_id)
    if pos.get("status") != "open":
        raise HTTPException(409, f"position {position_id} is already closed")
    if body.closed_on < pos["opened_on"]:
        raise _invalid("closed_on", "a position can't close before it opened", str(body.closed_on))
    ctx = await _write_context()
    fields, exit_price = _on_todays_scale(pos, ctx, body.closed_on, body.exit_price)
    pos = {**pos, **fields}
    entry, stop = float(pos["entry_price"]), float(pos["initial_stop"])
    r_multiple = (exit_price - entry) / (entry - stop) if entry > stop else None
    closed = await store.update_position(
        position_id,
        {
            **fields,
            "status": "closed",
            "closed_on": body.closed_on,
            "exit_price": exit_price,
            "exit_reason": body.exit_reason,
            "r_multiple": r_multiple,
        },
    )
    return await _position_response(closed, ctx)


def _on_todays_scale(
    pos: Mapping[str, Any], ctx: ScanContext | None, closed_on: date, exit_price: float
) -> tuple[dict[str, float], float]:
    """The position's rescaled prices (empty when no split) and the exit price, both on the
    cached panel's adjusted scale, so the R of a trade across a split is on one scale.

    The same split check the nightly job applies (:func:`~swing_engine.positions.split_ratio`):
    the entry against the adjusted close on ``opened_on`` and the exit against the one on
    ``closed_on``. Without a context (none yet, or not within the write's wait), for a symbol
    the cached panel lacks, or when the position was advised for a later session than the
    context's (the same guard as :func:`_advise`), both stay as given.
    """
    if ctx is None or str(pos["symbol"]) not in ctx.panel.close.columns:
        return {}, exit_price
    last = pos.get("last_action_on")
    if last is not None and ctx.session.date() < pd.Timestamp(last).date():
        # The job advised (and split-adjusted) it for a later session than the cache holds: the
        # older bars may predate a split it already applied, and would undo it.
        return {}, exit_price
    ratio = split_ratio(pos, ctx)
    fields = rescaled(pos, ratio) if ratio != 1.0 else {}
    sold = {"symbol": pos["symbol"], "opened_on": closed_on, "entry_price": exit_price}
    return fields, exit_price * split_ratio(sold, ctx)


# --- routes: track record ---------------------------------------------------------------------


@router.get("/track-record", response_model=TrackRecordOut)
async def track_record() -> dict[str, Any]:
    """Per setup family: the validation verdict, in-sample, out-of-sample and live stats, the
    random-entry baseline and the equity curve against SPY, all read from what is stored."""
    settings = await store.get_settings()
    families = read_families(repo_root())
    out = [
        family_out(families[family], await store.idea_rows(family), settings) for family in FAMILIES
    ]
    return _json({"families": out})


# --- routes: settings -------------------------------------------------------------------------


@router.get("/settings", response_model=SwingSettings)
async def get_settings() -> dict[str, Any]:
    """The stored settings (the engine defaults when none were saved)."""
    return _json((await store.get_settings()).model_dump())


@router.put("/settings", response_model=SwingSettings)
async def put_settings(body: SettingsIn) -> dict[str, Any]:
    """Save the settings. Fields left out keep their stored values; unknown fields and values
    outside the bounds are a 422 and nothing is saved."""
    current = await store.get_settings()
    try:
        merged = SwingSettings.model_validate(
            {**current.model_dump(), **body.model_dump(exclude_unset=True)}
        )
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in errors]
        ) from None
    saved = await store.put_settings(merged)
    if saved.universe() != current.universe():
        invalidate_context(during_job=job.is_running())
    return _json(saved.model_dump())


@router.post("/settings/telegram-test", response_model=TelegramTestOut)
async def telegram_test() -> dict[str, Any]:
    """Send one fixed test message to the configured Telegram chat; never raises."""
    delivery = await send_digest(TELEGRAM_TEST_TEXT)
    detail = str(redact_secrets(delivery.detail or ""))[:_SECRET_DETAIL_CHARS]
    return _json({"ok": bool(delivery.ok), "detail": detail})


# --- routes: run scan now ---------------------------------------------------------------------

_BACKGROUND: set[asyncio.Task[Any]] = set()
"""Running "Run scan now" jobs (a strong reference, so a task is not collected mid-run)."""


class _JobIdRecorder:
    """The job's product store, noting the id ``start_job`` returns (so the route can answer
    with it) and passing every other call through unchanged."""

    def __init__(self, inner: ProductStore) -> None:
        self._inner = inner
        self.job_id: uuid.UUID | None = None
        self.started = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def start_job(self, kind: str, session_date: date | None) -> uuid.UUID:
        self.job_id = await self._inner.start_job(kind, session_date)
        self.started.set()  # only once the run is recorded: a failure ends the task instead
        return self.job_id


async def _manual_job(deps: JobDeps) -> dict[str, Any]:
    try:
        row: dict[str, Any] = await run_job(deps, "manual")
        return row
    finally:
        close = getattr(deps.client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


def _finished(task: asyncio.Task[Any]) -> None:
    _BACKGROUND.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, job.JobBusy):
        log.warning("swing.api.manual_scan_failed", error=safe_reason(exc))


@router.post(
    "/scan/run",
    status_code=202,
    response_model=ScanRunOut,
    responses={409: {"model": DetailOut}, 503: {"model": DetailOut}},
)
async def run_scan_now() -> Any:
    """Start the nightly job now ("Run scan now"), in the background.

    202 ``{job_id, starting: false}`` once the job has recorded its run, or ``{job_id: null,
    starting: true}`` when it is still starting after :data:`JOB_ID_WAIT_S` (a slow calendar; it
    goes on, and the system status shows it). 409 while a scan is running; 503 with the reason
    when the job cannot be set up (no Alpaca keys) or fails before recording its run. The job is
    the scheduler's own: re-running a session is safe (scans are upserts, one digest a session).
    """
    if job.is_running():
        return JSONResponse(status_code=409, content={"detail": "a scan is already running"})
    try:
        deps = make_swing_deps()
    except Exception as exc:
        return JSONResponse(
            status_code=503, content={"detail": f"Can't start a scan: {safe_reason(exc)}"}
        )
    recorder = _JobIdRecorder(deps.store)
    task = asyncio.create_task(
        _manual_job(dataclasses.replace(deps, store=cast(ProductStore, recorder)))
    )
    _BACKGROUND.add(task)
    task.add_done_callback(_finished)
    started = asyncio.create_task(recorder.started.wait())
    try:
        await asyncio.wait({task, started}, timeout=JOB_ID_WAIT_S, return_when="FIRST_COMPLETED")
    finally:
        started.cancel()
    if recorder.job_id is not None:
        return _json({"job_id": str(recorder.job_id), "starting": False})
    if task.done() and not task.cancelled():
        error = task.exception()
        if isinstance(error, job.JobBusy):
            return JSONResponse(status_code=409, content={"detail": "a scan is already running"})
        reason = "it ended before recording its run" if error is None else safe_reason(error)
        return JSONResponse(
            status_code=503, content=_json({"detail": f"The scan could not start: {reason}"})
        )
    # Alive but not recorded yet (the calendar is slow): it is genuinely running, so this is a
    # start, not an error. Stopping it mid-insert could leave a run marked running forever.
    return _json({"job_id": None, "starting": True})


# --- routes: status ---------------------------------------------------------------------------


def _configured(*names: str) -> bool:
    """Whether every one of ``names`` is set in the environment (the values are never read out)."""
    return all(bool(os.environ.get(name)) for name in names)


@router.get("/system/status", response_model=StatusOut)
async def system_status() -> dict[str, Any]:
    """Data as of, the last job and its steps, the next scan, backfill progress, configuration."""
    scan = await store.latest_scan()
    run = await store.latest_job()
    state = scheduler_state()
    last = None
    if run is not None:
        last = {
            "session": run.get("session"),
            "kind": str(run.get("kind")),
            "started_at": run.get("started_at"),
            "generated_at": run.get("finished_at"),
            "status": str(run.get("status")),
            "steps": dict(run.get("steps") or {}),
            "error": run.get("error"),
        }
    return _json(
        {
            "data_as_of": None if scan is None else scan["session"],
            "last_scan": last,
            "next_scan_at": state["next_scan_at"],
            "backfill": _backfill(run),
            "alpaca_configured": _configured("ALPACA_API_KEY", "ALPACA_API_SECRET"),
            "telegram_configured": _configured("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
            "auto_execution": _AUTO_EXECUTION,
            "scheduler_running": bool(state["running"]),
            "disabled_reason": state["disabled_reason"],
        }
    )
