"""The nightly job: after each close, bring the data up to date, scan, track, advise, tell the user.

:func:`run_job` is the one orchestrator behind the after-close schedule, the startup catch-up, the
"Run scan now" button and ``python -m swing_engine job``. Its target is the last completed
session (the close plus the 16 minutes Alpaca's free plan holds SIP data back). It runs
:data:`STEPS` in order and records each in the job's ``swing_job_runs`` row as it goes:

1. ``assets``: re-fetch Alpaca's active and inactive asset lists when the last run that refreshed
   them did so more than :data:`ASSET_REFRESH_EVERY` ago (new listings and delistings).
2. ``bars``: the resumable backfill from :data:`~swing_engine.ingest.HISTORY_START` when SPY has no
   bars (the first run), when the assets step refreshed the list in this run (it loads the new
   listings and retries symbols whose load failed) or when SPY's newest bar is more than
   :data:`~swing_engine.ingest.MAX_STALE_SESSIONS` sessions behind (the update maintains only
   recent symbols); it saves ``{"done": n, "total": m}`` progress about every 5%. Otherwise the
   incremental update. Symbols the data provider cannot serve (several hundred placeholder
   tickers never can) are listed in the step, not counted as a failure.
3. ``scan``: every session after the last stored scan through the target, oldest first, or just
   the target when nothing was ever scanned or the target already was (a re-run refreshes it).
   The stored bars are loaded once, with the held symbols whatever their liquidity. One context
   is built per session and reused for its scan; the target's is kept for the steps below and
   handed to ``JobDeps.on_context`` (the API's cache). A session's book holds only the positions
   opened by then. The ideas of a session are for the next one; they are ``reconstructed`` when
   that session had already opened at the moment they are saved (a catch-up after the Mac slept,
   or a slow run), so they never count toward the live record.
4. ``tracking``: every pending or open idea, watch-only ones included, re-simulated to the target.
5. ``positions``: :func:`~swing_engine.positions.nightly_update` for each open position: split
   adjustment, then the rules walked over every session since its last advice, so a stop hit on
   a missed day is reported instead of tonight's hold. A position already advised for the target
   keeps its stored action, so running a session twice says the same thing.
6. ``digest``: one Telegram digest per session, only when the user has it on.

A failed step records ``{"status": "failed", "error": <reason>}`` (first line only, secrets
removed), the steps after it do not run, the job is ``failed`` and the previous scan stays as it
was. The user gets one failure notice per session at most (when the digest is on). Re-running is
always safe: scans are upserts that keep the first ``published_at``, tracking is a pure function
of the bars, and the digest goes out once per session.

The CPU-bound work (the per-session context and scan, the idea tracking and the position walk,
and the loader's panel build) runs in a worker thread, so the event loop that serves the API is
not frozen for the tens of seconds a full universe takes. Everything that touches the stores, the
network or ``on_context`` stays on the loop.

Only one job runs at a time in a process: :func:`run_job` raises :class:`JobBusy` instead of
waiting. Every outside dependency (Alpaca, the calendar, both stores, Telegram, the bar loader and
the clock) comes in through :class:`JobDeps`, so the whole job runs on fakes in tests. Nothing here
places an order.
"""

from __future__ import annotations

import asyncio
import copy
import math
import os
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import pandas as pd
from alert_service.channels.base import DeliveryResult, redact_secrets
from atlas_shared.logging import get_logger

from swing_engine import product_store, store
from swing_engine.calendar import ET, TradingCalendar
from swing_engine.config import RiskSettings, UniverseFilter
from swing_engine.data.alpaca import AlpacaData
from swing_engine.data.assets import AssetKind
from swing_engine.digest import format_digest, format_failure, send_digest
from swing_engine.ingest import (
    _SECRET_ENV_VARS as _ALPACA_SECRETS,
)
from swing_engine.ingest import (
    HISTORY_START,
    MAX_STALE_SESSIONS,
    BarSource,
    BarStore,
    IngestResult,
    backfill,
    incremental_update,
    refresh_assets,
)
from swing_engine.loader import load_scan_inputs
from swing_engine.panel import Panel
from swing_engine.positions import Action, PositionUpdate, current_stop, nightly_update
from swing_engine.scan import (
    BENCHMARK,
    RankBy,
    ScanContext,
    ScanResult,
    SetupSpec,
    build_context,
    default_setups,
    run_scan,
)
from swing_engine.settings import SwingSettings
from swing_engine.tracking import is_live, track_ideas
from swing_engine.validation.run import ranking_choice

log = get_logger("swing.job")

STEPS = ("assets", "bars", "scan", "tracking", "positions", "digest")
"""The job's steps, in the order they run."""

JobKind = Literal["nightly", "catchup", "manual"]
_KINDS = ("nightly", "catchup", "manual")

SETTLE_MINUTES = 16
"""A session is complete this long after its close (Alpaca's free plan delays SIP data 15)."""

ASSET_REFRESH_EVERY = timedelta(days=7)
"""How old the last asset-list refresh may be before the job fetches the lists again."""

PROGRESS_EVERY = 0.05
"""A first backfill saves its progress each time it advances this share of the symbols."""

DIGEST = "digest"
FAILURE = "failure"

_SECRET_ENV_VARS = (*_ALPACA_SECRETS, "TELEGRAM_BOT_TOKEN")
_REASON_CHARS = 300


class ProductStore(Protocol):
    """The part of :mod:`swing_engine.product_store` the job uses (the module satisfies it)."""

    async def get_settings(self) -> SwingSettings: ...

    async def save_scan(
        self, scan: ScanResult, target_session: date, published_at: datetime, reconstructed: bool
    ) -> None: ...

    async def latest_scan(self) -> dict[str, Any] | None: ...

    async def trackable_ideas(self) -> list[dict[str, Any]]: ...

    async def update_idea_tracks(self, rows: Sequence[Mapping[str, Any]]) -> None: ...

    async def list_positions(self, status: str | None) -> list[dict[str, Any]]: ...

    async def update_position(
        self, position_id: uuid.UUID | str, fields: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def start_job(self, kind: str, session_date: date | None) -> uuid.UUID: ...

    async def finish_job(
        self, job_id: uuid.UUID | str, status: str, steps: Any, error: str | None
    ) -> None: ...

    async def update_job_steps(self, job_id: uuid.UUID | str, steps: Any) -> None: ...

    async def latest_job(self) -> dict[str, Any] | None: ...

    async def last_step_ok(self, step: str) -> datetime | None: ...

    async def digest_sent(self, session_date: date, kind: str) -> bool: ...

    async def mark_digest(
        self, session_date: date, kind: str, ok: bool, detail: str | None
    ) -> None: ...


class ScanLoader(Protocol):
    """Reads the stored bars a scan needs (:func:`~swing_engine.loader.load_scan_inputs`)."""

    def __call__(
        self, as_of: date | None, universe: UniverseFilter, extra_symbols: Sequence[str] = ()
    ) -> Awaitable[tuple[Panel, dict[str, AssetKind], date]]: ...


@dataclass
class JobDeps:
    """Everything the job reaches outside itself.

    ``client`` is the Alpaca data client (:class:`~swing_engine.data.alpaca.AlpacaData`),
    ``calendar`` returns the exchange calendar around now, ``bars_store`` is
    :mod:`swing_engine.store` and ``store`` is :mod:`swing_engine.product_store` (or fakes with
    the same functions). ``send`` delivers a Telegram text, ``repo`` is where ``ml/validation``
    lives, ``now`` is the clock, ``loader`` reads the stored bars for a scan
    (:func:`~swing_engine.loader.load_scan_inputs`) and ``on_context``, when given, receives the
    target session's scan context after the scan (the API's cache).
    """

    client: BarSource
    calendar: Callable[[], Awaitable[TradingCalendar]]
    bars_store: BarStore
    store: ProductStore
    send: Callable[[str], Awaitable[DeliveryResult]]
    repo: Path
    now: Callable[[], datetime]
    loader: ScanLoader = load_scan_inputs
    on_context: Callable[[ScanContext], None] | None = None


class JobBusy(RuntimeError):  # noqa: N818  (the name the API and scheduler use)
    """A job is already running in this process."""


_LOCK = asyncio.Lock()


def is_running() -> bool:
    """True while a job runs in this process."""
    return _LOCK.locked()


async def run_job(deps: JobDeps, kind: JobKind) -> dict[str, Any]:
    """Run the job once and return its finished ``swing_job_runs`` row (``steps`` parsed).

    ``kind`` is recorded with the run: ``nightly`` (the schedule), ``catchup`` (startup after a
    missed close) or ``manual`` (the button or the CLI); the work is the same. Raises
    :class:`JobBusy` at once, without waiting, when another job is running; step failures are
    recorded in the row instead of raised. Only a store that cannot record the run at all (the
    database is down) makes it raise.
    """
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}, not {kind!r}")
    if _LOCK.locked():
        raise JobBusy("a scan is already running")
    async with _LOCK:
        return await _Run(deps, kind).run()


def live_deps(client: AlpacaData, repo: Path) -> JobDeps:
    """The real dependencies: ``client`` for Alpaca, the database stores, Telegram, the clock.

    The calendar is fetched from Alpaca on every call, from 400 days back to 30 days ahead.
    """

    async def calendar() -> TradingCalendar:
        today = datetime.now(UTC).astimezone(ET).date()
        sessions = await client.calendar(today - timedelta(days=400), today + timedelta(days=30))
        return TradingCalendar(sessions)

    return JobDeps(
        client=client,
        calendar=calendar,
        bars_store=store,
        store=product_store,
        send=send_digest,
        repo=repo,
        now=_utc_now,
        loader=load_scan_inputs,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def format_job(run: Mapping[str, Any]) -> str:
    """A job row as text: the kind, session and status, then one line per step."""
    steps: Mapping[str, Mapping[str, Any]] = run.get("steps") or {}
    session = run.get("session")
    day = session.isoformat() if isinstance(session, date) else "no session"
    lines = [f"job {run['kind']} for {day}: {run['status']}"]
    names = [*(name for name in steps if name not in STEPS), *STEPS]
    width = max(len(name) for name in names)
    for name in names:
        info = steps.get(name)
        if info is None:
            state = "not run"
        else:
            state = str(info.get("status", "unknown"))
            if info.get("error"):
                state += f": {info['error']}"
            elif info.get("reason"):
                state += f" ({info['reason']})"
            if info.get("failed"):
                state += f", {len(info['failed'])} symbols could not be fetched"
        lines.append(f"  {name:<{width}}  {state}")
    return "\n".join(lines)


# --- the run ---------------------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    """An exception as one short line for the job log and the notice, with no secret in it."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    text = lines[0] if lines else type(exc).__name__
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return _redact(text)[:_REASON_CHARS]


def _redact(text: str) -> str:
    """``text`` with any bot token or credential in it masked (``redact_secrets``)."""
    return str(redact_secrets(text))


@dataclass(frozen=True)
class _Failure:
    """Where the run stopped and why (the reason is already safe to show)."""

    step: str
    reason: str

    def __str__(self) -> str:
        return f"{self.step}: {self.reason}"


@dataclass
class _Tonight:
    """What the scan step leaves for the steps after it."""

    calendar: TradingCalendar
    sessions: list[date]
    setups: list[SetupSpec]
    ctx: ScanContext
    result: ScanResult


class _Run:
    """One run of the job: the step log, the shared state and the order of the steps."""

    def __init__(self, deps: JobDeps, kind: JobKind) -> None:
        self.deps = deps
        self.kind = kind
        self.store = deps.store
        self.now = deps.now()
        self.steps: dict[str, dict[str, Any]] = {}
        self.job_id: uuid.UUID | None = None
        self.target: date | None = None
        self.settings: SwingSettings | None = None
        self.calendar: TradingCalendar | None = None
        self.tonight: _Tonight | None = None
        self.assets_refreshed = False
        self.actions: list[Action] = []
        self.held: list[str] = []
        self._pending: asyncio.Task[None] | None = None

    async def run(self) -> dict[str, Any]:
        failure: _Failure | None = None
        try:
            self.calendar = await self.deps.calendar()
            self.target = self.calendar.last_completed_session(self.now, SETTLE_MINUTES)
        except Exception as exc:
            failure = _Failure("calendar", _reason(exc))
        self.job_id = await self.store.start_job(self.kind, self.target)
        if failure is None:
            try:
                self.settings = await self.store.get_settings()
            except Exception as exc:
                failure = _Failure("settings", _reason(exc))
        if failure is None:
            steps = (
                self._assets,
                self._bars,
                self._scan,
                self._tracking,
                self._positions,
                self._digest,
            )
            for name, work in zip(STEPS, steps, strict=True):
                failure = await self._step(name, work)
                if failure is not None:
                    break
        else:
            self.steps[failure.step] = {"status": "failed", "error": failure.reason}

        if failure is not None:
            log.warning("swing.job.failed", step=failure.step, reason=failure.reason)
            await self._notify_failure(failure)
        status = "ok" if failure is None else "failed"
        error = None if failure is None else str(failure)
        await self.store.finish_job(self.job_id, status, self.steps, error)
        log.info("swing.job.finished", kind=self.kind, session=str(self.target), status=status)
        return await self._row(status, error)

    async def _step(
        self, name: str, work: Callable[[], Awaitable[dict[str, Any]]]
    ) -> _Failure | None:
        self.steps[name] = {"status": "running"}
        await self._persist()
        try:
            info = await work()
        except Exception as exc:
            reason = _reason(exc)
            self.steps[name] = {"status": "failed", "error": reason}
            log.debug("swing.job.step_failed", step=name, exc_info=True)
            return _Failure(name, reason)
        finally:
            await self._flush()
        self.steps[name] = info
        await self._persist()
        return None

    async def _row(self, status: str, error: str | None) -> dict[str, Any]:
        """The finished row as stored, or the same fields when the store shows a newer run."""
        row = await self.store.latest_job()
        if row is not None and str(row.get("id")) == str(self.job_id):
            return row
        return {
            "id": self.job_id,
            "kind": self.kind,
            "session": self.target,
            "started_at": None,
            "finished_at": None,
            "status": status,
            "steps": copy.deepcopy(self.steps),
            "error": error,
        }

    # --- saving progress -----------------------------------------------------------------

    async def _persist(self) -> None:
        """Save the step log so far. A failed save is logged: progress is not worth a failure."""
        if self.job_id is None:
            return
        try:
            await self.store.update_job_steps(self.job_id, copy.deepcopy(self.steps))
        except Exception as exc:
            log.warning("swing.job.progress_not_saved", error=_reason(exc))

    def persist_soon(self) -> None:
        """Save the step log from a synchronous callback, after any save still in flight."""
        previous = self._pending

        async def write() -> None:
            if previous is not None:
                await previous
            await self._persist()

        self._pending = asyncio.get_running_loop().create_task(write())

    async def _flush(self) -> None:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            await pending

    # --- the steps -----------------------------------------------------------------------

    async def _assets(self) -> dict[str, Any]:
        last = await self.store.last_step_ok("assets")
        if last is not None and self.now - last <= ASSET_REFRESH_EVERY:
            return {"status": "skipped", "reason": f"refreshed {last.date().isoformat()}"}
        rows = await refresh_assets(self.deps.client, self.deps.bars_store)
        self.assets_refreshed = True
        return {"status": "ok", "assets": len(rows)}

    async def _bars(self) -> dict[str, Any]:
        trigger = await self._backfill_trigger()
        if trigger is None:
            result = await incremental_update(self.deps.client, self.deps.bars_store, now=self.now)
            return {**_ingest_info("update", result), "trigger": None}
        progress = _Progress(self)
        result = await backfill(
            self.deps.client,
            self.deps.bars_store,
            start=HISTORY_START,
            now=self.now,
            progress=progress,
        )
        return {
            **_ingest_info("backfill", result),
            "trigger": trigger,
            "done": progress.done,
            "total": progress.total,
        }

    async def _backfill_trigger(self) -> str | None:
        """Why the bars need the backfill tonight rather than the update, or None.

        The update only maintains symbols already stored and at most
        :data:`~swing_engine.ingest.MAX_STALE_SESSIONS` behind, so new listings, symbols whose
        first load failed and a store left behind for weeks need the (resumable) backfill.
        """
        assert self.calendar is not None and self.target is not None
        newest = (await self.deps.bars_store.last_bar_dates()).get(BENCHMARK)
        if newest is None:
            return "no SPY bars stored"
        if self.assets_refreshed:
            return "asset list refreshed"
        behind = len(self.calendar.sessions_between(newest, self.target))
        if behind > MAX_STALE_SESSIONS:
            return f"SPY is {behind} sessions behind"
        return None

    async def _scan(self) -> dict[str, Any]:
        assert self.settings is not None and self.calendar is not None and self.target is not None
        cal, target = self.calendar, self.target
        universe, risk = self.settings.universe(), self.settings.risk()
        setups = default_setups(self.deps.repo)
        rank_by = ranking_choice(self.deps.repo)
        latest = await self.store.latest_scan()
        sessions = _sessions_to_scan(cal, None if latest is None else latest["session"], target)
        positions = await self.store.list_positions("open")
        held = sorted({str(p["symbol"]) for p in positions})
        panel, kinds, _ = await self.deps.loader(target, universe, extra_symbols=held)

        scanned: tuple[ScanContext, ScanResult] | None = None
        for session in sessions:
            scanned = await asyncio.to_thread(
                _scan_session,
                panel,
                kinds,
                session,
                universe,
                risk,
                setups,
                _book(positions, session),
                rank_by,
            )
            ideas_for = cal.next_session(session)
            saved_at = self.deps.now()  # the moment they are published, not the run's start
            await self.store.save_scan(
                scanned[1], ideas_for, saved_at, not is_live(saved_at, ideas_for)
            )
        assert scanned is not None  # sessions always ends with the target
        ctx, result = scanned
        self.tonight = _Tonight(cal, sessions, setups, ctx, result)
        if self.deps.on_context is not None:
            try:
                self.deps.on_context(ctx)
            except Exception as exc:
                log.warning("swing.job.context_hook_failed", error=_reason(exc))
        return {
            "status": "ok",
            "sessions": [s.isoformat() for s in sessions],
            "regime": result.regime.value,
            "ideas": len(result.ideas),
            "watch_only": len(result.watch_only),
        }

    async def _tracking(self) -> dict[str, Any]:
        assert self.tonight is not None
        ideas = await self.store.trackable_ideas()
        delisted = _inactive(await self.deps.bars_store.load_assets())
        rows = await asyncio.to_thread(
            track_ideas, ideas, self.tonight.ctx, delisted, self.deps.repo
        )
        # price_scale says how the published levels were rescaled; it is not a stored column.
        await self.store.update_idea_tracks(
            [{k: v for k, v in row.items() if k != "price_scale"} for row in rows]
        )
        return {
            "status": "ok",
            "ideas": len(rows),
            "by_status": dict(Counter(str(row["track_status"]) for row in rows)),
        }

    async def _positions(self) -> dict[str, Any]:
        assert self.tonight is not None and self.target is not None
        positions = await self.store.list_positions("open")
        updates = await asyncio.to_thread(_walk_positions, positions, self.tonight.ctx)
        for pos, update in zip(positions, updates, strict=True):
            if update.fields:
                await self.store.update_position(pos["id"], update.fields)
        self.actions = [u.action for u in updates]
        self.held = [str(p["symbol"]) for p in positions]
        return {
            "status": "ok",
            "positions": len(updates),
            "actions": dict(Counter(u.action.kind.value for u in updates)),
            "already_advised": sum(u.reused for u in updates),
            "split_adjusted": [
                str(p["symbol"]) for p, u in zip(positions, updates, strict=True) if u.ratio != 1
            ],
        }

    async def _digest(self) -> dict[str, Any]:
        assert self.tonight is not None and self.settings is not None and self.target is not None
        if not self.settings.telegram_digest:
            return {"status": "skipped", "reason": "the Telegram digest is off in Settings"}
        if await self.store.digest_sent(self.target, DIGEST):
            return {"status": "skipped", "reason": "already sent for this session"}
        tonight = self.tonight
        text = format_digest(
            tonight.result,
            tonight.calendar.next_session(self.target),
            self.actions,
            [s for s in tonight.sessions if s != self.target],
            sum(1 for idea in tonight.result.ideas if not idea.live),
            held=self.held,
            any_live_setup=any(spec.live for spec in tonight.setups),
        )
        delivery = await self.deps.send(text)
        detail = _redact(delivery.detail)
        await self.store.mark_digest(self.target, DIGEST, delivery.ok, detail or None)
        # A send that did not go through (no bot configured, Telegram down) leaves the digest
        # unsent for a later run to retry; the scan itself did not fail.
        return {"status": "ok", "sent": bool(delivery.ok), "detail": detail}

    async def _notify_failure(self, failure: _Failure) -> None:
        """Tell the user the scan failed, once per session, when the digest is on."""
        if self.target is None or self.settings is None or not self.settings.telegram_digest:
            return
        try:
            if await self.store.digest_sent(self.target, FAILURE):
                return
            delivery = await self.deps.send(
                format_failure(self.target, failure.step, failure.reason)
            )
            detail = _redact(delivery.detail)
            await self.store.mark_digest(self.target, FAILURE, delivery.ok, detail or None)
        except Exception as exc:
            log.warning("swing.job.failure_notice_failed", error=_reason(exc))


class _Progress:
    """The backfill's ``progress(done, total)`` callback: saves the bars step about every 5%."""

    def __init__(self, run: _Run) -> None:
        self.run = run
        self.done = 0
        self.total = 0
        self._saved: int | None = None

    def __call__(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        self.run.steps["bars"] = {"status": "running", "done": done, "total": total}
        stride = max(1, math.ceil(total * PROGRESS_EVERY))
        if self._saved is None or done >= total or done - self._saved >= stride:
            self._saved = done
            self.run.persist_soon()


# --- helpers ---------------------------------------------------------------------------


def _ingest_info(mode: str, result: IngestResult) -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": mode,
        "symbols_done": result.symbols_done,
        "symbols_total": result.symbols_total,
        "rows": result.rows,
        "raw_rows": result.raw_rows,
        "refetched": list(result.refetched),
        "failed": list(result.failed),
    }


def _scan_session(
    panel: Panel,
    kinds: Mapping[str, AssetKind],
    session: date,
    universe: UniverseFilter,
    risk: RiskSettings,
    setups: Sequence[SetupSpec],
    book: Sequence[tuple[str, float]],
    rank_by: RankBy,
) -> tuple[ScanContext, ScanResult]:
    """One session's context and scan: pure pandas work, run in a worker thread."""
    ctx = build_context(panel, kinds, session, universe)
    result = run_scan(
        panel,
        kinds,
        session,
        risk,
        setups,
        book,
        universe=universe,
        rank_by=rank_by,
        ctx=ctx,
    )
    return ctx, result


def _walk_positions(
    positions: Sequence[Mapping[str, Any]], ctx: ScanContext
) -> list[PositionUpdate]:
    """Tonight's update for each open position (no store access), run in a worker thread."""
    return [nightly_update(pos, ctx) for pos in positions]


def _sessions_to_scan(cal: TradingCalendar, last_scanned: date | None, target: date) -> list[date]:
    """Each session after ``last_scanned`` through ``target``, or ``[target]`` when that is none
    (nothing scanned yet, or the target already was)."""
    if last_scanned is None:
        return [target]
    return cal.sessions_between(last_scanned, target) or [target]


def _open_risk(pos: Mapping[str, Any]) -> float:
    """What a position still has at stake if it were stopped out now (never negative)."""
    return float(pos["shares"]) * max(0.0, float(pos["entry_price"]) - current_stop(pos))


def _book(positions: Sequence[Mapping[str, Any]], session: date) -> list[tuple[str, float]]:
    """``(symbol, open risk)`` of the positions already opened on ``session``."""
    return [
        (str(p["symbol"]), _open_risk(p))
        for p in positions
        if pd.Timestamp(p["opened_on"]).date() <= session
    ]


def _inactive(assets: pd.DataFrame) -> set[str]:
    """The symbols whose asset status is inactive (delisted)."""
    if assets.empty or "status" not in assets.columns:
        return set()
    return {str(s) for s in assets.loc[assets["status"] == "inactive", "symbol"]}
