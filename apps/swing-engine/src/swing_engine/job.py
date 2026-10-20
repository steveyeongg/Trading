"""The nightly job: after each close, bring the data up to date, scan, track, advise, tell the user.

:func:`run_job` is the one orchestrator behind the after-close schedule, the startup catch-up, the
"Run scan now" button and ``python -m swing_engine job``. Its target is the last completed
session (the close plus the 16 minutes Alpaca's free plan holds SIP data back). It runs
:data:`STEPS` in order and records each in the job's ``swing_job_runs`` row as it goes:

1. ``assets``: re-fetch Alpaca's active and inactive asset lists when the last successful run that
   refreshed them is more than :data:`ASSET_REFRESH_EVERY` old (new listings and delistings).
2. ``bars``: the very first time (no SPY bars stored) a full backfill from
   :data:`~swing_engine.ingest.HISTORY_START`, saving ``{"done": n, "total": m}`` progress about
   every 5%; afterwards the incremental update. Symbols the data provider cannot serve (several
   hundred placeholder tickers never can) are listed in the step, not counted as a failure.
3. ``scan``: every session after the last stored scan through the target, oldest first, or just
   the target when nothing was ever scanned or the target already was (a re-run refreshes it).
   One context is built per session and reused for its scan; the target's is kept for the steps
   below and handed to ``JobDeps.on_context`` (the API's cache). The ideas of a session are for the
   next one; they are ``reconstructed`` when that session had already opened by now (a catch-up
   after the Mac slept), so they never count toward the live record.
4. ``tracking``: every pending or open idea, watch-only ones included, re-simulated to the target.
5. ``positions``: tonight's action for each open position the user recorded. A position bought
   before a split is first rescaled to the adjusted prices the bars use. Over a catch-up the
   actions are worked out session by session, so a stop hit on a missed day is reported instead
   of tonight's hold, and a raised stop carries forward.
6. ``digest``: one Telegram digest per session, only when the user has it on.

A failed step records ``{"status": "failed", "error": <reason>}`` (first line only, secrets
removed), the steps after it do not run, the job is ``failed`` and the previous scan stays as it
was. The user gets one failure notice per session at most (when the digest is on). Re-running is
always safe: scans are upserts that keep the first ``published_at``, tracking is a pure function
of the bars, and the digest goes out once per session.

Only one job runs at a time in a process: :func:`run_job` raises :class:`JobBusy` instead of
waiting. Every outside dependency (Alpaca, the calendar, both stores, Telegram, the bar loader and
the clock) comes in through :class:`JobDeps`, so the whole job runs on fakes in tests. Nothing here
places an order.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
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
from swing_engine.config import UniverseFilter
from swing_engine.data.alpaca import AlpacaData
from swing_engine.data.assets import AssetKind
from swing_engine.digest import format_digest, format_failure, send_digest
from swing_engine.ingest import (
    HISTORY_START,
    BarSource,
    BarStore,
    IngestResult,
    backfill,
    incremental_update,
    refresh_assets,
)
from swing_engine.loader import load_scan_inputs
from swing_engine.narrate import day_label
from swing_engine.panel import Panel
from swing_engine.positions import Action, ActionKind, position_action
from swing_engine.scan import (
    BENCHMARK,
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

SPLIT_BAND = (0.75, 1.33)
"""A position whose opening-day adjusted close is within this ratio of its entry was not split."""

SPLIT_RATIOS = (1 / 10, 1 / 8, 1 / 5, 1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 2, 2, 3, 4, 5, 8, 10)
"""The split ratios a position is snapped to (adjusted price / price paid)."""

DIGEST = "digest"
FAILURE = "failure"

_SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "TELEGRAM_BOT_TOKEN")
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


ScanLoader = Callable[
    [date | None, UniverseFilter], Awaitable[tuple[Panel, dict[str, AssetKind], date]]
]


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
        return {"status": "ok", "assets": len(rows)}

    async def _bars(self) -> dict[str, Any]:
        stored = await self.deps.bars_store.last_bar_dates()
        if BENCHMARK in stored:
            result = await incremental_update(self.deps.client, self.deps.bars_store, now=self.now)
            return _ingest_info("update", result)
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
            "done": progress.done,
            "total": progress.total,
        }

    async def _scan(self) -> dict[str, Any]:
        assert self.settings is not None and self.calendar is not None and self.target is not None
        cal, target, now = self.calendar, self.target, self.now
        universe, risk = self.settings.universe(), self.settings.risk()
        setups = default_setups(self.deps.repo)
        rank_by = ranking_choice(self.deps.repo)
        latest = await self.store.latest_scan()
        sessions = _sessions_to_scan(cal, None if latest is None else latest["session"], target)
        panel, kinds, _ = await self.deps.loader(target, universe)
        book = [(str(p["symbol"]), _open_risk(p)) for p in await self.store.list_positions("open")]

        ctx: ScanContext | None = None
        result: ScanResult | None = None
        for session in sessions:
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
            ideas_for = cal.next_session(session)
            await self.store.save_scan(result, ideas_for, now, not is_live(now, ideas_for))
        assert ctx is not None and result is not None  # sessions always ends with the target
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
        rows = track_ideas(ideas, self.tonight.ctx, delisted, self.deps.repo)
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
        actions: list[Action] = []
        adjusted: list[str] = []
        for pos in positions:
            ratio = split_ratio(pos, self.tonight.ctx)
            fields: dict[str, Any] = {}
            if ratio != 1.0:
                fields = _rescaled(pos, ratio)
                pos = {**pos, **fields}
                adjusted.append(str(pos["symbol"]))
            action, stop = _catch_up_action(pos, self.tonight.ctx, self.tonight.sessions)
            if ratio != 1.0:
                text = f"adjusted for a split (\u00d7{ratio:g}); {action.text}"
                action = dataclasses.replace(action, text=text)
            fields.update(
                last_action=action.kind.value,
                last_action_text=action.text,
                last_action_on=self.target,
            )
            if stop != _current_stop(pos):
                fields["current_stop"] = stop
            await self.store.update_position(pos["id"], fields)
            actions.append(action)
        self.actions = actions
        self.held = [str(p["symbol"]) for p in positions]
        return {
            "status": "ok",
            "positions": len(actions),
            "actions": dict(Counter(a.kind.value for a in actions)),
            "split_adjusted": adjusted,
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


def _sessions_to_scan(cal: TradingCalendar, last_scanned: date | None, target: date) -> list[date]:
    """Each session after ``last_scanned`` through ``target``, or ``[target]`` when that is none
    (nothing scanned yet, or the target already was)."""
    if last_scanned is None:
        return [target]
    return cal.sessions_between(last_scanned, target) or [target]


def _current_stop(pos: Mapping[str, Any]) -> float:
    stop = pos.get("current_stop")
    return float(pos["initial_stop"] if stop is None else stop)


def _open_risk(pos: Mapping[str, Any]) -> float:
    """What a position still has at stake if it were stopped out now (never negative)."""
    return float(pos["shares"]) * max(0.0, float(pos["entry_price"]) - _current_stop(pos))


def _inactive(assets: pd.DataFrame) -> set[str]:
    """The symbols whose asset status is inactive (delisted)."""
    if assets.empty or "status" not in assets.columns:
        return set()
    return {str(s) for s in assets.loc[assets["status"] == "inactive", "symbol"]}


def split_ratio(pos: Mapping[str, Any], ctx: ScanContext) -> float:
    """The factor from the price the user paid to tonight's adjusted prices, 1.0 for no split.

    It is the adjusted close on ``opened_on`` over ``entry_price``. Inside :data:`SPLIT_BAND` it is
    an ordinary day's move from the fill and 1.0 is returned; outside it, it is snapped to the
    nearest of :data:`SPLIT_RATIOS` (nearest by ratio, so 7 snaps to 8 and 0.3 to 1/3). When the
    symbol has no bar on ``opened_on`` (or is not in the panel) nothing can be measured: 1.0.
    """
    symbol = str(pos["symbol"])
    entry = float(pos["entry_price"])
    opened = pd.Timestamp(pos["opened_on"]).normalize()
    close = ctx.panel.close
    if symbol not in close.columns or opened not in close.index or not entry > 0:
        return 1.0
    adjusted = float(close.at[opened, symbol])
    if not (math.isfinite(adjusted) and adjusted > 0):
        return 1.0
    ratio = adjusted / entry
    low, high = SPLIT_BAND
    if low <= ratio <= high:
        return 1.0
    return float(min(SPLIT_RATIOS, key=lambda r: abs(math.log(ratio / r))))


def _rescaled(pos: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    """The position's prices times ``ratio`` and its shares over it (the same money at risk)."""
    fields = {
        "entry_price": float(pos["entry_price"]) * ratio,
        "initial_stop": float(pos["initial_stop"]) * ratio,
        "shares": float(pos["shares"]) / ratio,
    }
    if pos.get("current_stop") is not None:
        fields["current_stop"] = float(pos["current_stop"]) * ratio
    return fields


def _at(ctx: ScanContext, session: date) -> ScanContext:
    """``ctx`` read as of an earlier ``session``.

    The position rules read the bars up to their session only, and indicators are causal, so the
    target's context answers for any session it contains.
    """
    return dataclasses.replace(ctx, session=pd.Timestamp(session))


def _to_the_cent(action: Action) -> Action:
    """A raised stop as the user is told it (``raise stop to 95.01``), to the cent."""
    if action.kind is ActionKind.RAISE_STOP and action.stop is not None:
        return dataclasses.replace(action, stop=round(action.stop, 2))
    return action


def _catch_up_action(
    pos: Mapping[str, Any], ctx: ScanContext, sessions: Sequence[date]
) -> tuple[Action, float]:
    """Tonight's action and the stop to keep, after walking any missed sessions, oldest first.

    A stop hit on a missed session ends the walk and is the action, its text led by the day. A
    stop raised on a missed session carries forward, so tonight's rules run from it, and if
    tonight itself would only hold, the action is still to raise the stop to that level. With no
    missed session this is :func:`~swing_engine.positions.position_action` at the target. A
    raised stop is kept to the cent, as the user is told it.
    """
    target = ctx.session.date()
    state = dict(pos)
    for session in sessions:
        if session >= target:
            continue
        missed = _to_the_cent(position_action(state, _at(ctx, session)))
        if missed.kind is ActionKind.STOP_HIT:
            text = f"{day_label(session)}: {missed.text}"
            return dataclasses.replace(missed, text=text), _current_stop(state)
        if missed.kind is ActionKind.RAISE_STOP and missed.stop is not None:
            state["current_stop"] = missed.stop
    action = _to_the_cent(position_action(state, ctx))
    carried = _current_stop(state)
    if action.kind is ActionKind.RAISE_STOP and action.stop is not None:
        return action, action.stop
    if action.kind is ActionKind.HOLD and carried > _current_stop(pos):
        text = f"raise stop to {carried:.2f} (trail)"
        return dataclasses.replace(action, kind=ActionKind.RAISE_STOP, text=text), carried
    return action, carried
