"""Swing engine: the nightly job (``run_job``) and ``python -m swing_engine job``.

The job runs after each close: refresh the asset list (weekly), update the bars (or backfill them
the very first time), scan every session since the last stored scan in order, forward-track the
published ideas, work out tonight's action for each position the user holds, and send one Telegram
digest. These tests pin what the review asked about:

* **The Mac slept through two sessions.** Catch-up scans each missed session in order, marks the
  ideas of the sessions whose next open has already passed ``reconstructed``, and sends exactly one
  digest naming the missed days. A stop hit on a missed day is not lost behind tonight's hold.
* **Two runs for one session** (the schedule and a "Run scan now"): one set of ideas, no second
  digest, and a concurrent run is refused with ``JobBusy``.
* **A step fails** (Alpaca down): the job is ``failed`` at that step with a reason that carries no
  secret, the later steps don't run, the previous scan stays as it was, and the failure notice is
  sent at most once per session.
* **The API stays up while the job computes.** The CPU-bound steps (the per-session context and
  scan, the idea tracking and the position walk) run in a worker thread, so the event loop that
  serves the dashboard is not frozen for tens of seconds.
* The controller's rulings: the context is built once per session and reused, open risk is
  ``shares x max(0, entry - current stop)``, the asset list is refreshed weekly, unservable symbols
  don't fail the bars step, positions are split-adjusted before their action, raised stops are
  stored to the cent, and the digest gets the held symbols, the validated-setup flag and the count
  of unproven ideas.

Nothing here touches the network, Telegram or a database: Alpaca's ingest calls are replaced by
recorders, the product store is an in-memory fake that refuses the same columns the real one does,
and the bars come from ``synthetic_universe_panel`` re-dated so its scan day is Mon 19 Oct 2026.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import io
import itertools
import math
import threading
import time as clock_time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import swing_engine.__main__ as cli
from alert_service.channels.base import DeliveryResult
from swing_engine import job, narrate, product_store, scheduler, store
from swing_engine.calendar import Session, TradingCalendar
from swing_engine.config import BreakoutParams, PullbackParams, UniverseFilter
from swing_engine.digest import format_failure, send_digest
from swing_engine.ingest import HISTORY_START, IngestResult
from swing_engine.job import STEPS, JobBusy, JobDeps, format_job, run_job
from swing_engine.loader import load_scan_inputs
from swing_engine.panel import Panel, truncate
from swing_engine.positions import ActionKind, position_action
from swing_engine.product_store import _POSITION_WRITABLE, _TRACK_COLUMNS
from swing_engine.regime import Regime
from swing_engine.scan import ScanContext, ScanError, SetupSpec, build_context, scan_to_dict
from swing_engine.settings import SwingSettings
from swing_engine.testing import synthetic_kinds, synthetic_universe_panel

ET = ZoneInfo("America/New_York")
DEFAULT_TRAIL = BreakoutParams().trail_atr  # what an empty repo's (unvalidated) breakout uses
TARGET = date(2026, 10, 19)  # Mon 19 Oct 2026
NEXT = date(2026, 10, 20)
THU, FRI, WED = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 14)
MON_19_OCT_1700_ET = datetime(2026, 10, 19, 17, 0, tzinfo=ET)
_FIELDS = ("open", "high", "low", "close", "volume")

# Shaped like a real bot token so redact_secrets' pattern applies to it.
FAKE_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _redated(panel: Panel) -> Panel:
    """``panel`` with row 450 (the synthetic scan day) on Mon 19 Oct 2026, business days around."""
    index = pd.bdate_range(end=pd.Timestamp(TARGET), periods=451).append(
        pd.bdate_range(start=pd.Timestamp(NEXT), periods=len(panel.dates()) - 451)
    )
    frames = {f: getattr(panel, f).set_axis(index, axis=0) for f in _FIELDS}
    raw = None if panel.raw_close is None else panel.raw_close.set_axis(index, axis=0)
    return Panel(**frames, raw_close=raw)


PANEL = _redated(synthetic_universe_panel(seed=5))
KINDS = synthetic_kinds(PANEL)
CAL = TradingCalendar([Session(d.date(), time(9, 30), time(16, 0)) for d in PANEL.dates()])
CTX = build_context(PANEL, KINDS, TARGET)


def _downtrend(panel: Panel) -> Panel:
    """A copy whose SPY falls all the way, so the target session is a downtrend."""
    n = len(panel.dates())
    noise = 0.004 * np.random.default_rng(3).standard_normal(n)
    path = 100.0 * np.exp(np.cumsum(np.full(n, -0.002) + noise))
    frames = {f: getattr(panel, f).copy() for f in _FIELDS}
    for f in ("open", "high", "low", "close"):
        frames[f]["SPY"] = path
    return Panel(**frames, raw_close=frames["close"].copy())


def _close(symbol: str, day: date) -> float:
    return float(PANEL.close.at[pd.Timestamp(day), symbol])


def _low(symbol: str, day: date) -> float:
    return float(PANEL.low.at[pd.Timestamp(day), symbol])


# ---------------------------------------------------------------------------- fakes


class FakeProductStore:
    """The product store's functions over dicts, refusing the columns the real store refuses."""

    def __init__(
        self,
        scanned: Sequence[date] = (),
        *,
        settings: SwingSettings | None = None,
        positions: Sequence[Mapping[str, Any]] = (),
        jobs: Sequence[Mapping[str, Any]] = (),
        clock: datetime = MON_19_OCT_1700_ET,
    ) -> None:
        self.settings = settings or SwingSettings()
        self.clock = clock
        self.scans: dict[date, dict[str, Any]] = {
            s: {"session": s, "target_session": CAL.next_session(s), "payload": {"old": True}}
            for s in scanned
        }
        self.saved_sessions: list[date] = []
        self.reconstructed: dict[date, bool] = {}
        self.ideas: dict[tuple[date, str, str], dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        for p in positions:
            row = {
                "id": uuid.uuid4(),
                "trail_atr": None,
                "status": "open",
                "last_action": None,
                "last_action_text": None,
                "last_action_on": None,
                **p,
            }
            row.setdefault("current_stop", row["initial_stop"])
            self.positions[str(row["id"])] = row
        self.position_writes: list[tuple[str, dict[str, Any]]] = []
        self.jobs: list[dict[str, Any]] = [dict(j) for j in jobs]
        self.step_writes: list[dict[str, Any]] = []
        self.digests: dict[tuple[date, str], dict[str, Any]] = {}

    async def get_settings(self) -> SwingSettings:
        return self.settings

    async def save_scan(
        self, scan, target_session, published_at, reconstructed, narration=None
    ) -> None:
        if published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        self.saved_sessions.append(scan.session)
        first = self.reconstructed.setdefault(scan.session, reconstructed)
        self.scans[scan.session] = {
            "session": scan.session,
            "target_session": target_session,
            "reconstructed": first,
            "payload": product_store.scan_payload(scan, narration),  # as the real store writes it
        }
        for ideas, parked in ((scan.ideas, False), (scan.watch_only, True)):
            for idea in ideas:
                key = (scan.session, idea.symbol, idea.setup)
                fresh = {"live": idea.live, "watch_only": parked, "rank": idea.rank}
                if key in self.ideas:
                    self.ideas[key].update(fresh)
                    continue
                self.ideas[key] = {
                    "id": uuid.uuid4(),
                    "session": scan.session,
                    "target_session": target_session,
                    "symbol": idea.symbol,
                    "setup": idea.setup,
                    "version": idea.version,
                    "trigger": idea.trigger,
                    "stop": idea.stop,
                    "atr14": idea.atr14,
                    "published_at": published_at,
                    "reconstructed": reconstructed,
                    "track_status": "pending",
                    **fresh,
                }

    async def latest_scan(self) -> dict[str, Any] | None:
        return dict(self.scans[max(self.scans)]) if self.scans else None

    async def scanned_sessions(self, since: date) -> set[date]:
        return {s for s in self.scans if s >= since}

    async def trackable_ideas(self) -> list[dict[str, Any]]:
        rows = sorted(self.ideas.values(), key=lambda r: (r["session"], r["symbol"], r["setup"]))
        return [dict(r) for r in rows if r["track_status"] in ("pending", "open")]

    async def update_idea_tracks(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            unknown = set(row) - {"id", *_TRACK_COLUMNS}
            if unknown:
                raise ValueError(f"an idea track row has no writable column(s) {sorted(unknown)}")
        by_id = {str(r["id"]): r for r in self.ideas.values()}
        for row in rows:
            by_id[str(row["id"])].update({k: v for k, v in row.items() if k != "id"})

    async def get_idea(self, idea_id) -> dict[str, Any] | None:
        return next((dict(r) for r in self.ideas.values() if str(r["id"]) == str(idea_id)), None)

    async def list_positions(self, status: str | None) -> list[dict[str, Any]]:
        return [dict(p) for p in self.positions.values() if status in (None, p["status"])]

    async def update_position(self, position_id, fields: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(fields) - set(_POSITION_WRITABLE)
        if unknown:
            raise ValueError(f"a position has no writable column(s) {sorted(unknown)}")
        self.position_writes.append((str(position_id), dict(fields)))
        self.positions[str(position_id)].update(fields)
        return dict(self.positions[str(position_id)])

    async def start_job(self, kind: str, session_date: date | None) -> uuid.UUID:
        job_id = uuid.uuid4()
        self.jobs.append(
            {
                "id": job_id,
                "kind": kind,
                "session": session_date,
                "started_at": self.clock,
                "finished_at": None,
                "status": "running",
                "steps": None,
                "error": None,
            }
        )
        return job_id

    def _job(self, job_id) -> dict[str, Any]:
        return next(j for j in self.jobs if j["id"] == job_id)

    async def update_job_steps(self, job_id, steps: Any) -> None:
        self.step_writes.append(copy.deepcopy(steps))
        self._job(job_id)["steps"] = copy.deepcopy(steps)

    async def finish_job(self, job_id, status: str, steps: Any, error: str | None) -> None:
        self._job(job_id).update(
            status=status, steps=copy.deepcopy(steps), error=error, finished_at=self.clock
        )

    async def latest_job(self) -> dict[str, Any] | None:
        return dict(max(self.jobs, key=lambda j: j["started_at"])) if self.jobs else None

    async def last_step_ok(self, step: str) -> datetime | None:
        done = [
            j["started_at"]
            for j in self.jobs
            if ((j.get("steps") or {}).get(step) or {}).get("status") == "ok"
        ]
        return max(done) if done else None

    async def digest_sent(self, session_date: date, kind: str) -> bool:
        return bool(self.digests.get((session_date, kind), {}).get("ok"))

    async def mark_digest(self, session_date: date, kind: str, ok: bool, detail) -> None:
        if not await self.digest_sent(session_date, kind):
            self.digests[(session_date, kind)] = {"ok": ok, "detail": detail}


class FakeBarsStore:
    """The two reads the job makes of the bars store: the newest bar dates and the asset list."""

    def __init__(self, *, spy: bool = True, inactive: Sequence[str] = ()) -> None:
        self.last = {s: TARGET for s in PANEL.symbols()} if spy else {}
        symbols = PANEL.symbols()
        self.assets = pd.DataFrame(
            {
                "symbol": symbols,
                "kind": [KINDS[s].value for s in symbols],
                "status": ["inactive" if s in inactive else "active" for s in symbols],
            }
        )

    async def last_bar_dates(self, resolution: str = "1d") -> dict[str, date]:
        return dict(self.last)

    async def load_assets(self) -> pd.DataFrame:
        return self.assets.copy()


class FakeIngest:
    """Replaces the three Alpaca ingest calls the job makes and records how they were called."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result: IngestResult | None = None
        self.raises: BaseException | None = None
        self.backfill_total = 10
        monkeypatch.setattr(job, "refresh_assets", self.refresh_assets)
        monkeypatch.setattr(job, "backfill", self.backfill)
        monkeypatch.setattr(job, "incremental_update", self.incremental_update)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def refresh_assets(self, client, bars_store) -> list[dict[str, Any]]:
        self.calls.append(("refresh_assets", {"client": client, "store": bars_store}))
        return [{"symbol": s} for s in PANEL.symbols()]

    async def incremental_update(self, client, bars_store, *, now) -> IngestResult:
        self.calls.append(
            ("incremental_update", {"client": client, "store": bars_store, "now": now})
        )
        if self.raises is not None:
            raise self.raises
        return self.result or IngestResult(41, 41, 41, [])

    async def backfill(
        self, client, bars_store, *, start, now, progress=None, **kw
    ) -> IngestResult:
        self.calls.append(
            ("backfill", {"client": client, "store": bars_store, "start": start, "now": now, **kw})
        )
        if self.raises is not None:
            raise self.raises
        total = self.backfill_total
        for done in range(total + 1):
            if progress is not None:
                progress(done, total)
            await asyncio.sleep(0)  # a real backfill awaits Alpaca between chunks
        return self.result or IngestResult(total, total, 10 * total, [])


class Harness:
    """Fakes for every dependency of one job, and the ``JobDeps`` that wires them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
        self.monkeypatch = monkeypatch
        self.repo = repo
        self.store = FakeProductStore()
        self.bars = FakeBarsStore()
        self.ingest = FakeIngest(monkeypatch)
        self.client = object()
        self.panel = PANEL
        self.sent: list[str] = []
        self.delivery = DeliveryResult(ok=True, detail="sent")
        self.loads: list[tuple[date | None, UniverseFilter]] = []
        self.extras: list[list[str]] = []
        self.contexts: list[ScanContext] = []

    async def calendar(self) -> TradingCalendar:
        return CAL

    async def send(self, text: str) -> DeliveryResult:
        self.sent.append(text)
        return self.delivery

    async def loader(
        self, as_of: date | None, universe: UniverseFilter, extra_symbols: Sequence[str] = ()
    ):
        self.loads.append((as_of, universe))
        self.extras.append(list(extra_symbols))
        assert as_of is not None
        return truncate(self.panel, pd.Timestamp(as_of)), KINDS, as_of

    def deps(
        self,
        now: datetime = MON_19_OCT_1700_ET,
        clock: Callable[[], datetime] | None = None,
        **over: Any,
    ) -> JobDeps:
        """The deps for a run started at ``now``; ``clock`` replaces the clock when time moves."""
        self.store.clock = now
        fields: dict[str, Any] = {
            "client": self.client,
            "calendar": self.calendar,
            "bars_store": self.bars,
            "store": self.store,
            "send": self.send,
            "repo": self.repo,
            "now": clock or (lambda: now),
            "loader": self.loader,
            "on_context": self.contexts.append,
        }
        fields.update(over)
        return JobDeps(**fields)

    def spy(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any], Any]]:
        """Record ``(args, kwargs, result)`` of each call the job makes to ``job.<name>``, still
        calling the real function."""
        real = getattr(job, name)
        calls: list[tuple[tuple[Any, ...], dict[str, Any], Any]] = []

        def recorder(*args: Any, **kwargs: Any) -> Any:
            result = real(*args, **kwargs)
            calls.append((args, kwargs, result))
            return result

        self.monkeypatch.setattr(job, name, recorder)
        return calls


@pytest.fixture
def h(monkeypatch, tmp_path) -> Harness:
    """A harness in an empty repo: both setups unvalidated, defaults from the code."""
    return Harness(monkeypatch, tmp_path)


def _position(symbol: str, opened_on: date, entry: float, stop: float, **kw: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "opened_on": opened_on,
        "entry_price": entry,
        "shares": kw.pop("shares", 10.0),
        "initial_stop": stop,
        "current_stop": kw.pop("current_stop", stop),
        **kw,
    }


def _only_position(store: FakeProductStore) -> dict[str, Any]:
    (row,) = store.positions.values()
    return row


def _refreshed_yesterday() -> dict[str, Any]:
    """A successful run a day ago that refreshed the asset list: tonight's assets step skips."""
    return _job_row(MON_19_OCT_1700_ET - timedelta(days=1), "ok", "ok")


# ---------------------------------------------------------------------------- the brief's tests


async def test_catch_up_scans_each_missed_session_in_order_and_sends_one_digest(h):
    h.store = FakeProductStore(scanned=[WED])
    run = await run_job(h.deps(now=MON_19_OCT_1700_ET), "catchup")

    assert run["status"] == "ok" and run["kind"] == "catchup" and run["session"] == TARGET
    assert h.store.saved_sessions == [THU, FRI, TARGET]
    assert h.store.reconstructed == {THU: True, FRI: True, TARGET: False}
    assert len(h.sent) == 1 and "Missed sessions caught up: Thu 15 Oct, Fri 16 Oct" in h.sent[0]
    assert "ideas for Tue 20 Oct" in h.sent[0]
    assert h.store.digests[(TARGET, "digest")] == {"ok": True, "detail": "sent"}
    assert [run["steps"][s]["status"] for s in STEPS] == ["ok"] * 6
    assert run["steps"]["scan"]["sessions"] == [
        THU.isoformat(),
        FRI.isoformat(),
        TARGET.isoformat(),
    ]
    # the published day of each scan's ideas is the next session; all share the run's time
    assert {s: h.store.scans[s]["target_session"] for s in (THU, FRI, TARGET)} == {
        THU: FRI,
        FRI: TARGET,
        TARGET: NEXT,
    }
    assert {r["published_at"] for r in h.store.ideas.values()} == {MON_19_OCT_1700_ET}
    assert h.loads == [(TARGET, SwingSettings().universe())]  # one load for the whole catch-up


async def test_second_run_same_session_sends_no_second_digest(h):
    first = await run_job(h.deps(), "nightly")
    published = {k: r["published_at"] for k, r in h.store.ideas.items()}
    later = MON_19_OCT_1700_ET + timedelta(hours=2)
    second = await run_job(h.deps(now=later), "manual")

    assert first["status"] == second["status"] == "ok"
    assert len(h.sent) == 1
    assert second["steps"]["digest"]["status"] == "skipped"
    assert h.store.saved_sessions == [TARGET, TARGET]  # re-scanned, upserted in place
    assert {k: r["published_at"] for k, r in h.store.ideas.items()} == published


async def test_concurrent_run_raises_job_busy(h):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_loader(as_of, universe, extra_symbols=()):
        entered.set()
        await release.wait()
        return await h.loader(as_of, universe, extra_symbols)

    first = asyncio.create_task(run_job(h.deps(loader=slow_loader), "nightly"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert job.is_running()
        with pytest.raises(JobBusy):
            await run_job(h.deps(), "manual")
    finally:
        release.set()
        run = await first
    assert run["status"] == "ok" and not job.is_running()
    assert len(h.store.jobs) == 1  # the refused run never started a job row


async def test_failed_bars_step_keeps_previous_scan_and_notifies_once(h):
    h.store = FakeProductStore(scanned=[FRI])
    before = await h.store.latest_scan()
    h.ingest.raises = RuntimeError("alpaca 403")

    run = await run_job(h.deps(), "nightly")

    assert run["status"] == "failed" and run["steps"]["bars"]["status"] == "failed"
    assert "scan" not in run["steps"] and set(run["steps"]) == {"assets", "bars"}
    assert run["steps"]["bars"]["error"] == "alpaca 403" and run["error"] == "bars: alpaca 403"
    assert h.store.saved_sessions == [] and await h.store.latest_scan() == before
    assert h.sent == [format_failure(TARGET, "bars", "alpaca 403")]

    again = await run_job(h.deps(), "nightly")
    assert again["status"] == "failed"
    assert len(h.sent) == 1  # one failure notice per session
    assert h.store.digests[(TARGET, "failure")]["ok"] is True
    assert (TARGET, "digest") not in h.store.digests


async def test_first_ever_run_scans_only_target_session(h):
    run = await run_job(h.deps(), "nightly")
    assert h.store.saved_sessions == [TARGET] and h.store.reconstructed == {TARGET: False}
    assert run["steps"]["scan"]["sessions"] == [TARGET.isoformat()]
    assert len(h.sent) == 1 and "Missed sessions" not in h.sent[0]


def _trending_symbol() -> str:
    """The leader at the target session that rose most over the last 40 sessions."""
    leaders = [s for s, on in CTX.leaders.loc[CTX.session].items() if on]
    gain = CTX.panel.close[leaders].iloc[-1] / CTX.panel.close[leaders].iloc[-41]
    return str(gain.idxmax())


async def test_raise_stop_action_updates_position_stop(h):
    symbol = _trending_symbol()
    opened = CAL.sessions_between(TARGET - timedelta(days=40), TARGET)[0]
    entry = _close(symbol, opened)
    pos = _position(symbol, opened, entry, stop=round(entry * 0.7, 2), last_action_on=FRI)
    expected = position_action({"id": "x", "trail_atr": DEFAULT_TRAIL, **pos}, CTX)
    assert expected.kind is ActionKind.RAISE_STOP, "premise: the trail rises tonight"
    assert expected.stop is not None and expected.stop != round(expected.stop, 2), "premise"
    h.store = FakeProductStore(positions=[pos])

    run = await run_job(h.deps(), "nightly")

    row = _only_position(h.store)
    assert run["steps"]["positions"]["status"] == "ok"
    assert row["current_stop"] == round(expected.stop, 2)  # to the cent, as the user is told
    assert row["last_action"] == "raise_stop" and row["last_action_on"] == TARGET
    assert (
        row["last_action_text"]
        == expected.text
        == f"raise stop to {row['current_stop']:.2f} (trail)"
    )
    assert row["initial_stop"] == pos["initial_stop"] and row["entry_price"] == entry
    assert f"• {symbol} {expected.text}" in h.sent[0]


async def test_a_position_saved_without_a_trail_follows_its_setups_preregistered_trail(h):
    """Trailing-stop parity: a row saved before ``trail_atr`` was stored (NULL) is advised with
    the pre-registered trail of the idea it came from, or the breakout family's when it has none,
    read from the real committed preregs (3.5), never the code default of 3.0."""
    h.repo = Path(__file__).resolve().parents[1]
    unlinked = _position("S03", TARGET - timedelta(days=30), 100.0, 90.0)
    linked = _position("S04", TARGET - timedelta(days=30), 100.0, 90.0, idea_id="idea-1")
    h.store = FakeProductStore(positions=[unlinked, linked])
    h.store.ideas[(FRI, "S04", "pullback")] = {
        "id": "idea-1", "symbol": "S04", "setup": "pullback", "version": "pullback-v1",
        "session": FRI, "track_status": "closed",
    }  # fmt: skip
    walked = h.spy("nightly_update")

    run = await run_job(h.deps(), "nightly")

    assert run["steps"]["positions"]["status"] == "ok"
    assert sorted((args[0]["symbol"], args[0]["trail_atr"], args[2]) for args, _, _ in walked) == [
        ("S03", 3.5, 30),
        ("S04", 3.5, 30),
    ]


async def test_a_cancelled_run_is_closed_as_failed_interrupted_and_the_cancel_propagates(h):
    """The incident: the API was stopped while a job ran and its row stayed ``running`` for
    good, which locked "Run scan now" and Today's progress. A cancelled run now closes its row
    as failed (the step it was in says "interrupted"), shielded from the cancellation, and the
    cancellation still propagates."""
    h.store = FakeProductStore(jobs=[_refreshed_yesterday()])
    entered, hang = asyncio.Event(), asyncio.Event()

    async def stuck(client, bars_store, *, now) -> IngestResult:
        entered.set()
        await hang.wait()
        raise AssertionError("never reached")

    h.monkeypatch.setattr(job, "incremental_update", stuck)
    task = asyncio.create_task(run_job(h.deps(), "nightly"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await h.store.latest_job()
    assert row is not None and row["status"] == "failed" and row["finished_at"] is not None
    assert row["steps"]["bars"] == {"status": "failed", "error": job.INTERRUPTED}
    assert row["error"] == f"bars: {job.INTERRUPTED}"
    assert row["error"].startswith("bars: interrupted")
    assert not job.is_running()  # the lock is released: the next run can start


# ---------------------------------------------------------------------------- the context (PR2)


async def test_one_context_per_session_is_reused_for_scan_tracking_positions_and_the_cache(h):
    built = h.spy("build_context")
    scans = h.spy("run_scan")
    tracked = h.spy("track_ideas")
    acted = h.spy("nightly_update")
    h.store = FakeProductStore(
        scanned=[WED],
        positions=[_position("S03", TARGET - timedelta(days=30), 100.0, 90.0)],
    )

    await run_job(h.deps(), "catchup")

    assert [args[2] for args, _, _ in built] == [THU, FRI, TARGET]  # once each, no rebuilds
    contexts = [ctx for _, _, ctx in built]
    assert len(scans) == len(contexts)
    assert all(kw["ctx"] is ctx for (_, kw, _), ctx in zip(scans, contexts, strict=True))
    target_ctx = contexts[-1]
    assert len(h.contexts) == 1 and h.contexts[0] is target_ctx  # the API cache, once
    assert tracked[0][0][1] is target_ctx
    assert acted and all(args[1] is target_ctx for args, _, _ in acted)


async def test_a_failing_cache_hook_does_not_fail_the_job(h):
    def broken(ctx):
        raise RuntimeError("cache unavailable")

    run = await run_job(h.deps(on_context=broken), "nightly")
    assert run["status"] == "ok"


# ---------------------------------------------------------------------------- open risk (PR4)


async def test_open_risk_is_shares_times_entry_minus_current_stop_never_negative(h):
    opened = TARGET - timedelta(days=30)
    h.store = FakeProductStore(
        positions=[
            _position("S01", opened, 100.0, 90.0, shares=10.0, current_stop=95.0),  # $50 at risk
            _position("S02", opened, 50.0, 40.0, shares=5.0, current_stop=55.0),  # stop above entry
        ]
    )
    scans = h.spy("run_scan")

    await run_job(h.deps(), "nightly")

    (args, _, _), *_ = scans
    assert sorted(args[5]) == [("S01", 50.0), ("S02", 0.0)]
    capacity = h.store.scans[TARGET]["payload"]["capacity"]
    assert capacity["open_positions"] == 2 and capacity["open_risk_dollars"] == 50.0


async def test_the_scan_uses_the_users_universe_risk_setups_and_ranking(h):
    settings = SwingSettings(equity=50_000.0, risk_pct=0.02, min_price=12.0)
    h.store = FakeProductStore(settings=settings)
    scans = h.spy("run_scan")

    await run_job(h.deps(), "nightly")

    (args, kwargs, _), *_ = scans
    assert args[3] == settings.risk() and kwargs["universe"] == settings.universe()
    assert kwargs["rank_by"] == "rs" and [s.params.version for s in args[4]] == [
        "breakout-v1",
        "pullback-v1",
    ]
    assert h.loads == [(TARGET, settings.universe())]


# ---------------------------------------------------------------------------- assets and bars (PR8)


def _job_row(started: datetime, status: str, assets: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "kind": "nightly",
        "session": started.date(),
        "started_at": started,
        "finished_at": started,
        "status": status,
        "steps": {"assets": {"status": assets}},
        "error": None,
    }


@pytest.mark.parametrize(
    ("history", "refreshed"),
    [
        pytest.param([], True, id="never-refreshed"),
        pytest.param([(3, "ok", "ok")], False, id="3-days-ago"),
        pytest.param([(8, "ok", "ok")], True, id="8-days-ago"),
        pytest.param([(1, "failed", "ok")], False, id="a-failed-run-that-refreshed-counts"),
        pytest.param([(9, "ok", "ok"), (1, "ok", "skipped")], True, id="skipped-is-not-a-refresh"),
    ],
)
async def test_the_asset_list_is_refreshed_weekly(h, history, refreshed):
    jobs = [_job_row(MON_19_OCT_1700_ET - timedelta(days=d), s, a) for d, s, a in history]
    h.store = FakeProductStore(jobs=jobs)

    run = await run_job(h.deps(), "nightly")

    assert ("refresh_assets" in h.ingest.names()) is refreshed
    assert run["steps"]["assets"]["status"] == ("ok" if refreshed else "skipped")
    if refreshed:
        _, kwargs = h.ingest.calls[0]
        assert kwargs == {"client": h.client, "store": h.bars}


async def test_a_weekly_refresh_is_remembered_by_the_next_run(h):
    await run_job(h.deps(), "nightly")
    await run_job(h.deps(now=MON_19_OCT_1700_ET + timedelta(days=1)), "nightly")
    assert h.ingest.names().count("refresh_assets") == 1


async def test_bars_are_updated_incrementally_when_spy_is_current_and_assets_unchanged(h):
    h.store = FakeProductStore(jobs=[_refreshed_yesterday()])
    run = await run_job(h.deps(), "nightly")
    assert h.ingest.names() == ["incremental_update"]
    _, kwargs = h.ingest.calls[0]
    assert kwargs == {"client": h.client, "store": h.bars, "now": MON_19_OCT_1700_ET}
    bars = run["steps"]["bars"]
    assert bars["mode"] == "update" and bars["trigger"] is None and bars["failed"] == []


async def test_a_weekly_asset_refresh_is_followed_by_a_backfill_to_load_new_listings(h):
    """The incident: the update only maintains symbols already stored, so a new listing (or a
    symbol whose first load failed) was never fetched at all."""
    run = await run_job(h.deps(), "nightly")  # SPY is current; the asset list is refreshed
    assert h.ingest.names() == ["refresh_assets", "backfill"]
    _, kwargs = h.ingest.calls[1]
    assert kwargs["start"] == HISTORY_START and kwargs["store"] is h.bars
    assert "refresh_assets" not in kwargs  # the assets step just did it
    bars = run["steps"]["bars"]
    assert bars["mode"] == "backfill" and bars["trigger"] == "asset list refreshed"


@pytest.mark.parametrize(
    ("behind", "mode"),
    [pytest.param(31, "backfill", id="31-sessions"), pytest.param(30, "update", id="30-sessions")],
)
async def test_bars_more_than_30_sessions_behind_are_backfilled(h, behind, mode):
    h.store = FakeProductStore(jobs=[_refreshed_yesterday()])
    h.bars.last["SPY"] = PANEL.dates()[450 - behind].date()
    assert len(CAL.sessions_between(h.bars.last["SPY"], TARGET)) == behind  # premise

    run = await run_job(h.deps(), "nightly")

    bars = run["steps"]["bars"]
    assert bars["mode"] == mode
    assert h.ingest.names() == (["backfill"] if mode == "backfill" else ["incremental_update"])
    if mode == "backfill":
        assert bars["trigger"] == f"SPY is {behind} sessions behind"


async def test_unservable_symbols_are_recorded_and_do_not_fail_the_bars_step(h):
    h.ingest.result = IngestResult(41, 44, 41, ["AAPL"], failed=["ZZZA", "ZZZB", "ZZZC"])

    run = await run_job(h.deps(), "nightly")

    assert run["status"] == "ok" and run["steps"]["bars"]["status"] == "ok"
    assert run["steps"]["bars"]["failed"] == ["ZZZA", "ZZZB", "ZZZC"]
    assert run["steps"]["bars"]["refetched"] == ["AAPL"]
    assert h.store.saved_sessions == [TARGET]  # the scan still ran


async def test_first_run_backfills_from_2016_and_reports_progress_as_it_goes(h):
    h.bars = FakeBarsStore(spy=False)

    h.ingest.backfill_total = 1000

    run = await run_job(h.deps(), "nightly")

    assert h.ingest.names() == ["refresh_assets", "backfill"]
    assert run["steps"]["bars"]["trigger"] == "no SPY bars stored"
    _, kwargs = h.ingest.calls[1]
    assert kwargs["start"] == HISTORY_START == date(2016, 1, 1)
    assert kwargs["now"] == MON_19_OCT_1700_ET and kwargs["store"] is h.bars
    progress = [
        w["bars"]
        for w in h.store.step_writes
        if w.get("bars", {}).get("status") == "running" and "done" in w["bars"]
    ]
    done = [p["done"] for p in progress]
    assert all(p["total"] == 1000 for p in progress)
    assert done[0] == 0 and done[-1] == 1000 and done == sorted(done)
    assert max(b - a for a, b in itertools.pairwise(done)) <= 50  # every 5% or less
    assert len(done) <= 25  # throttled, not one write per symbol
    assert run["status"] == "ok" and run["steps"]["bars"]["mode"] == "backfill"
    assert run["steps"]["bars"]["status"] == "ok"


async def test_each_step_is_saved_as_it_starts_so_a_running_job_shows_where_it_is(h):
    await run_job(h.deps(), "nightly")
    running = [
        next(name for name, info in w.items() if info.get("status") == "running")
        for w in h.store.step_writes
        if any(info.get("status") == "running" for info in w.values())
    ]
    assert list(dict.fromkeys(running)) == list(STEPS)


# ---------------------------------------------------------------------------- tracking


async def test_tracking_runs_at_the_target_with_watch_only_ideas_and_inactive_symbols(h):
    h.panel = _downtrend(PANEL)
    h.bars = FakeBarsStore(inactive=["S07"])
    tracked = h.spy("track_ideas")

    run = await run_job(h.deps(), "nightly")

    payload = h.store.scans[TARGET]["payload"]
    assert payload["regime"] == Regime.DOWNTREND.value and payload["watch_only"], "premise"
    (args, _, _), *_ = tracked
    ideas, ctx, delisted, repo = args
    assert ctx.session.date() == TARGET and delisted == {"S07"} and repo == h.repo
    assert {r["symbol"] for r in ideas if r["watch_only"]} == {
        i["symbol"] for i in payload["watch_only"]
    }
    assert run["steps"]["tracking"] == {
        "status": "ok",
        "ideas": len(ideas),
        "by_status": {"pending": len(ideas)},
        "untracked": 0,
    }


async def test_tracking_results_are_written_without_the_price_scale(h):
    h.store = FakeProductStore(scanned=[WED])
    await run_job(h.deps(), "catchup")  # Thu and Fri ideas exist by Mon

    tracked = [r for r in h.store.ideas.values() if r["session"] in (THU, FRI)]
    assert tracked and all("price_scale" not in r for r in h.store.ideas.values())
    assert {r["track_status"] for r in tracked} <= {
        "pending",
        "not_triggered",
        "open",
        "closed",
    }


async def test_an_open_idea_whose_symbol_tonights_panel_lacks_keeps_its_stored_tracking(h):
    """The incident: tracking wrote a row of Nones (``pending``) for every idea it could not
    track, so an open idea whose stock dropped out of tonight's panel (it went thin, under the
    loader's liquidity prefilter) lost its stored entry date and price. The job now loads every
    tracked idea's symbol whatever its liquidity, and an idea that still cannot be tracked gets
    no row, so its stored fields stay. A brand-new idea published tonight stays pending."""
    h.store = FakeProductStore(scanned=[FRI])
    entry_day = date(2026, 10, 13)
    h.store.ideas[(WED, "GONE", "breakout")] = {
        "id": uuid.uuid4(), "session": WED, "target_session": THU, "symbol": "GONE",
        "setup": "breakout", "version": "breakout-v1", "trigger": 50.0, "stop": 45.0,
        "atr14": 2.0, "live": True, "watch_only": False, "reconstructed": False,
        "published_at": MON_19_OCT_1700_ET - timedelta(days=5), "track_status": "open",
        "entry_date": entry_day, "entry_price": 50.05, "current_stop": 46.0,
    }  # fmt: skip

    run = await run_job(h.deps(), "nightly")

    assert "GONE" in h.extras[0]  # asked for whatever its liquidity
    kept = h.store.ideas[(WED, "GONE", "breakout")]
    assert (kept["track_status"], kept["entry_date"], kept["entry_price"]) == (
        "open",
        entry_day,
        50.05,
    )
    assert kept["current_stop"] == 46.0
    assert run["steps"]["tracking"]["untracked"] == 1
    fresh = [r for r in h.store.ideas.values() if r["session"] == TARGET]
    assert fresh and all(r["track_status"] == "pending" for r in fresh)
    assert all(r.get("entry_date") is None for r in fresh)


# ---------------------------------------------------------------------------- positions


def _opened(days_before: int = 30) -> date:
    return CAL.sessions_between(TARGET - timedelta(days=days_before), TARGET)[0]


@pytest.mark.parametrize(
    ("ratio", "snapped", "label"),
    [
        pytest.param(0.25, 0.25, "0.25", id="4-for-1"),
        pytest.param(0.30, 1 / 3, "0.333333", id="near-3-for-1"),
        pytest.param(2.1, 2.0, "2", id="1-for-2-reverse"),
    ],
)
async def test_a_split_since_the_purchase_rescales_the_position_first(h, ratio, snapped, label):
    symbol = _trending_symbol()
    opened = _opened()
    close = _close(symbol, opened)
    entry = close / ratio  # the price the user paid, before the split rescaled history
    h.store = FakeProductStore(
        positions=[_position(symbol, opened, entry, stop=entry * 0.9, shares=12.0)]
    )

    await run_job(h.deps(), "nightly")

    row = _only_position(h.store)
    assert row["entry_price"] == pytest.approx(entry * snapped)
    assert row["initial_stop"] == pytest.approx(entry * 0.9 * snapped)
    assert row["shares"] == pytest.approx(12.0 / snapped)
    assert row["last_action_text"].startswith(f"adjusted for a split (\u00d7{label}); ")
    (_, fields), *_ = h.store.position_writes
    assert {"entry_price", "initial_stop", "current_stop", "shares", "last_action"} <= set(fields)


async def test_no_split_inside_the_band_and_no_bar_on_the_open_day_leave_prices_alone(h):
    symbol = _trending_symbol()
    opened = _opened()
    inside = _position(symbol, opened, _close(symbol, opened) / 1.3, stop=10.0)  # r = 1.3
    saturday = _position(symbol, date(2026, 9, 19), 1000.0, stop=10.0)  # no bar that day
    unknown = _position("ZZZZ", opened, 1000.0, stop=10.0)  # not in the panel at all
    h.store = FakeProductStore(positions=[inside, saturday, unknown])

    await run_job(h.deps(), "nightly")

    for _, fields in h.store.position_writes:
        assert not {"entry_price", "shares", "initial_stop"} & set(fields)
        assert not str(fields["last_action_text"]).startswith("adjusted for a split")
    (missing,) = [p for p in h.store.positions.values() if p["symbol"] == "ZZZZ"]
    assert missing["last_action"] == "no_data"
    assert missing["last_action_text"].startswith("no price data for ZZZZ")


def _dip_on(day: date, others: Sequence[date]) -> tuple[str, float]:
    """A symbol whose low on ``day`` is under its low on each of ``others``, and a stop between."""
    for symbol in PANEL.symbols():
        if symbol == "SPY":
            continue
        dip, rest = _low(symbol, day), min(_low(symbol, d) for d in others)
        if dip < rest * 0.995:
            return symbol, (dip + rest) / 2
    raise AssertionError(f"premise: some symbol dips on {day}")


async def test_a_stop_hit_on_a_missed_session_is_reported_not_hidden_by_tonights_hold(h):
    symbol, stop = _dip_on(FRI, [THU, TARGET])
    opened = _opened(20)
    pos = _position(
        symbol, opened, _close(symbol, opened), stop=stop, trail_atr=50.0, last_action_on=WED
    )
    tonight_alone = position_action({"id": "x", "trail_atr": DEFAULT_TRAIL, **pos}, CTX)
    assert tonight_alone.kind is not ActionKind.STOP_HIT, "premise: Monday alone looks fine"
    h.store = FakeProductStore(scanned=[WED], positions=[pos])

    await run_job(h.deps(), "catchup")

    row = _only_position(h.store)
    assert row["last_action"] == "stop_hit"
    assert row["last_action_text"] == f"Fri 16 Oct: stop hit, exit (stop {stop:.2f})"
    assert row["last_action_on"] == TARGET

    # A second run for the same session keeps what the catch-up found.
    before = dict(row)
    await run_job(h.deps(now=MON_19_OCT_1700_ET + timedelta(hours=1)), "manual")
    assert _only_position(h.store) == before


async def test_the_same_session_twice_gives_the_same_actions_and_stops(h):
    """The incident: a re-run tested tonight's low against the stop the first run had just raised,
    so "raise stop to 424.95 (trail)" became "stop hit, exit (stop 424.95)"."""
    trail = 0.5
    for symbol in PANEL.symbols():
        opened = _opened()
        highest = float(CTX.panel.close[symbol].loc[pd.Timestamp(opened) :].max())
        raised = highest - trail * float(CTX.ind.atr14.at[CTX.session, symbol])
        low = _low(symbol, TARGET)
        if symbol != "SPY" and raised > low + 0.05:
            break
    else:
        raise AssertionError("premise: some trail rises above tonight's low")
    stop = round(low - 0.01, 2)
    pos = _position(
        symbol, opened, _close(symbol, opened), stop=stop, trail_atr=trail, last_action_on=FRI
    )
    h.store = FakeProductStore(positions=[pos])
    updates = h.spy("nightly_update")

    await run_job(h.deps(), "nightly")
    first = dict(_only_position(h.store))
    await run_job(h.deps(now=MON_19_OCT_1700_ET + timedelta(hours=2)), "manual")

    (_, _, one), (_, _, two) = updates
    assert one.action.kind is ActionKind.RAISE_STOP and one.action.stop == round(raised, 2)
    assert two.reused and two.action == one.action
    assert _only_position(h.store) == first


async def test_after_a_failed_catch_up_positions_are_walked_from_their_last_advice(h):
    symbol, stop = _dip_on(THU, [FRI, TARGET])
    opened = _opened(20)
    pos = _position(
        symbol, opened, _close(symbol, opened), stop=stop, trail_atr=50.0, last_action_on=WED
    )
    h.store = FakeProductStore(scanned=[WED], positions=[pos])
    real_run_scan = job.run_scan

    def friday_fails(panel, kinds, as_of, *args, **kwargs):
        if as_of == FRI:
            raise RuntimeError("the scan crashed on Friday")
        return real_run_scan(panel, kinds, as_of, *args, **kwargs)

    h.monkeypatch.setattr(job, "run_scan", friday_fails)
    failed = await run_job(h.deps(), "catchup")
    assert failed["steps"]["scan"]["status"] == "failed" and h.store.saved_sessions == [THU]
    assert _only_position(h.store)["last_action_on"] == WED  # not advised yet

    h.monkeypatch.setattr(job, "run_scan", real_run_scan)
    done = await run_job(h.deps(now=MON_19_OCT_1700_ET + timedelta(hours=1)), "catchup")

    assert done["steps"]["scan"]["sessions"] == [FRI.isoformat(), TARGET.isoformat()]
    row = _only_position(h.store)  # Thursday was scanned by the failed run, yet still walked
    assert row["last_action_text"] == f"Thu 15 Oct: stop hit, exit (stop {stop:.2f})"


async def test_a_trail_raised_on_a_missed_session_is_kept_and_told(h):
    symbol = _trending_symbol()
    opened = _opened(40)
    entry = _close(symbol, opened)
    pos = _position(symbol, opened, entry, stop=round(entry * 0.7, 2), last_action_on=WED)
    h.store = FakeProductStore(scanned=[WED], positions=[pos])

    await run_job(h.deps(), "catchup")

    row = _only_position(h.store)
    assert row["current_stop"] > pos["current_stop"] and row["current_stop"] == round(
        row["current_stop"], 2
    )
    assert row["last_action"] == "raise_stop"
    assert row["last_action_text"] == f"raise stop to {row['current_stop']:.2f} (trail)"


# ---------------------------------------------------------------------------- what a scan sees


async def test_the_live_label_uses_the_clock_when_the_scan_is_saved(h):
    """The incident: a run started at 09:00 and saved at 09:31 marked its ideas live, though the
    session they were for had already opened when they were published."""
    start = datetime(2026, 10, 20, 9, 0, tzinfo=ET)  # Tue: Monday's ideas, before the open
    saved = datetime(2026, 10, 20, 9, 31, tzinfo=ET)
    ticks = iter([start])
    h.store.clock = start

    run = await run_job(h.deps(now=start, clock=lambda: next(ticks, saved)), "nightly")

    assert run["session"] == TARGET  # the target comes from the start
    assert h.store.reconstructed == {TARGET: True}
    assert {r["published_at"] for r in h.store.ideas.values()} == {saved}


async def test_a_past_sessions_book_holds_only_positions_open_by_then(h):
    old = _position("S01", _opened(), 100.0, 90.0, shares=10.0)  # $100 at risk
    friday = _position("S02", FRI, 50.0, 40.0, shares=5.0)  # $50, bought on Friday
    h.store = FakeProductStore(scanned=[WED], positions=[old, friday])
    scans = h.spy("run_scan")

    await run_job(h.deps(), "catchup")

    books = {args[2]: sorted(args[5]) for args, _, _ in scans}
    assert books == {
        THU: [("S01", 100.0)],
        FRI: [("S01", 100.0), ("S02", 50.0)],
        TARGET: [("S01", 100.0), ("S02", 50.0)],
    }


async def test_open_position_symbols_are_always_loaded(h):
    opened = _opened()
    h.store = FakeProductStore(
        positions=[
            _position("S03", opened, _close("S03", opened), 1.0),
            _position("ZZZZ", opened, 10.0, 9.0),
        ]
    )
    await run_job(h.deps(), "nightly")
    assert h.extras == [["S03", "ZZZZ"]]


# ---------------------------------------------------------------------------- digest inputs


async def test_the_digest_gets_held_symbols_live_flag_unproven_count_and_missed_days(h):
    specs = [SetupSpec(BreakoutParams(), live=True), SetupSpec(PullbackParams(), live=False)]
    h.monkeypatch.setattr(job, "default_setups", lambda repo=None: specs)
    digests = h.spy("format_digest")
    opened = _opened()
    h.store = FakeProductStore(
        scanned=[WED],
        positions=[
            _position("S05", opened, _close("S05", opened), stop=1.0),
            _position("S09", opened, _close("S09", opened), stop=1.0),
        ],
    )

    await run_job(h.deps(), "catchup")

    (args, kwargs, _), *_ = digests
    scan = args[0]
    assert scan.session == TARGET and args[1] == NEXT
    assert sorted(a.symbol for a in args[2]) == ["S05", "S09"]
    assert list(args[3]) == [THU, FRI]
    unproven = [i for i in scan.ideas if not i.live]
    assert unproven and args[4] == len(unproven)
    assert sorted(kwargs["held"]) == ["S05", "S09"] and kwargs["any_live_setup"] is True


async def test_watch_only_ideas_never_count_as_unproven_ideas(h):
    h.panel = _downtrend(PANEL)
    digests = h.spy("format_digest")

    await run_job(h.deps(), "nightly")

    (args, kwargs, _), *_ = digests
    assert args[0].watch_only and not args[0].ideas, "premise: a downtrend parks every idea"
    assert args[4] == 0 and kwargs["any_live_setup"] is False


async def test_no_digest_when_it_is_turned_off(h):
    h.store = FakeProductStore(settings=SwingSettings(telegram_digest=False))
    run = await run_job(h.deps(), "nightly")
    assert h.sent == [] and run["steps"]["digest"]["status"] == "skipped"
    assert run["status"] == "ok"


async def test_a_failed_send_is_recorded_and_left_open_for_a_retry(h):
    h.delivery = DeliveryResult(ok=False, detail="telegram not configured")
    run = await run_job(h.deps(), "nightly")
    assert run["status"] == "ok"
    assert run["steps"]["digest"] == {
        "status": "ok",
        "sent": False,
        "detail": "telegram not configured",
    }
    assert await h.store.digest_sent(TARGET, "digest") is False

    h.delivery = DeliveryResult(ok=True, detail="sent")
    await run_job(h.deps(), "manual")
    assert len(h.sent) == 2 and await h.store.digest_sent(TARGET, "digest") is True


# ---------------------------------------------------------------------------- failures


async def test_no_failure_notice_when_the_digest_is_turned_off(h):
    h.store = FakeProductStore(settings=SwingSettings(telegram_digest=False))
    h.ingest.raises = RuntimeError("alpaca 403")
    run = await run_job(h.deps(), "nightly")
    assert run["status"] == "failed" and h.sent == []


async def test_a_failure_reason_never_carries_a_secret(h, monkeypatch):
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-alpaca-value")
    h.ingest.raises = RuntimeError(
        f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed; "
        "key s3cr3t-alpaca-value refused\n[SQL: SELECT 1]"
    )

    run = await run_job(h.deps(), "nightly")

    error = run["steps"]["bars"]["error"]
    assert FAKE_TOKEN.split(":")[1] not in error and "s3cr3t-alpaca-value" not in error
    assert "[SQL:" not in error  # the first line only
    assert FAKE_TOKEN.split(":")[1] not in h.sent[0] and "s3cr3t-alpaca-value" not in h.sent[0]


async def test_a_scan_failure_stops_before_tracking(h):
    async def no_spy(as_of, universe, extra_symbols=()):
        raise ScanError("no SPY bar on 2026-10-19: the market regime can't be read")

    run = await run_job(h.deps(loader=no_spy), "nightly")

    assert run["status"] == "failed" and run["steps"]["scan"]["status"] == "failed"
    assert "tracking" not in run["steps"] and h.contexts == []
    assert h.sent == [format_failure(TARGET, "scan", run["steps"]["scan"]["error"])]


async def test_a_calendar_failure_is_recorded_without_a_session(h):
    async def offline() -> TradingCalendar:
        raise RuntimeError("network error on /v2/calendar")

    run = await run_job(h.deps(calendar=offline), "nightly")

    assert run["status"] == "failed" and run["session"] is None
    assert run["steps"] == {
        "calendar": {"status": "failed", "error": "network error on /v2/calendar"}
    }
    assert h.sent == [] and h.ingest.calls == []


async def test_an_unknown_kind_is_refused(h):
    with pytest.raises(ValueError, match="kind"):
        await run_job(h.deps(), "weekly")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- the CLI


def test_the_job_command_parses():
    assert cli.build_parser().parse_args(["job"]).cmd == "job"


class _FakeClient:
    instances: ClassVar[list[_FakeClient]] = []

    def __init__(self) -> None:
        self.calendar_calls: list[tuple[date, date]] = []
        self.closed = False
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def calendar(self, start: date, end: date) -> list[Session]:
        self.calendar_calls.append((start, end))
        return [Session(d.date(), time(9, 30), time(16, 0)) for d in PANEL.dates()]


def _cli_job(monkeypatch, status: str = "ok") -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(cli, "AlpacaData", _FakeClient)
    _FakeClient.instances = []

    async def fake_run_job(deps: JobDeps, kind: str) -> dict[str, Any]:
        seen["deps"], seen["kind"] = deps, kind
        seen["calendar"] = await deps.calendar()
        seen["now"] = deps.now()
        steps = {name: {"status": "ok"} for name in STEPS}
        if status == "failed":
            steps = {"assets": {"status": "skipped"}, "bars": {"status": "failed", "error": "403"}}
        return {"kind": kind, "session": TARGET, "status": status, "steps": steps, "error": None}

    monkeypatch.setattr(cli, "run_job", fake_run_job)
    return seen


def test_the_job_command_runs_one_manual_job_with_the_real_dependencies(monkeypatch):
    seen = _cli_job(monkeypatch)

    with redirect_stdout(io.StringIO()) as out:
        cli.main(["job"])

    deps = seen["deps"]
    (client,) = _FakeClient.instances
    assert seen["kind"] == "manual" and deps.client is client and client.closed
    assert deps.store is product_store and deps.bars_store is store
    assert deps.send is send_digest and deps.loader is load_scan_inputs
    assert deps.repo == Path.cwd() and deps.on_context is None
    now = seen["now"]
    assert now.tzinfo is not None and abs(now - datetime.now(UTC)) < timedelta(minutes=1)
    ((start, end),) = client.calendar_calls
    today = datetime.now(UTC).astimezone(ET).date()
    assert (start, end) == (today - timedelta(days=400), today + timedelta(days=30))
    assert isinstance(seen["calendar"], TradingCalendar)
    text = out.getvalue()
    assert text.splitlines()[0] == "job manual for 2026-10-19: ok"
    assert all(f"  {name}" in text for name in STEPS)


def test_a_failed_job_prints_the_failed_step_and_exits_1(monkeypatch):
    _cli_job(monkeypatch, status="failed")

    with redirect_stdout(io.StringIO()) as out, pytest.raises(SystemExit) as exit_:
        cli.main(["job"])

    assert exit_.value.code == 1
    lines = out.getvalue().splitlines()
    assert lines[0] == "job manual for 2026-10-19: failed"
    assert any(line.split() == ["bars", "failed:", "403"] for line in lines)
    assert any(line.split() == ["scan", "not", "run"] for line in lines)


def test_format_job_names_extra_steps_and_reasons():
    run = {
        "kind": "nightly",
        "session": None,
        "status": "failed",
        "steps": {"calendar": {"status": "failed", "error": "offline"}},
    }
    text = format_job(run)
    assert text.splitlines()[0] == "job nightly for no session: failed"
    assert text.splitlines()[1].split() == ["calendar", "failed:", "offline"]
    skipped = format_job(
        {
            "kind": "manual",
            "session": TARGET,
            "status": "ok",
            "steps": {"digest": {"status": "skipped", "reason": "already sent"}},
        }
    )
    assert any(
        line.split() == ["digest", "skipped", "(already", "sent)"] for line in skipped.splitlines()
    )


# ---------------------------------------------------------------------------- the event loop stays free


def _where(real: Callable[..., Any], seen: list[tuple[bool, bool]]) -> Callable[..., Any]:
    """``real`` wrapped to note, per call, whether it ran on the event loop and on the main thread."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:  # no running loop in this thread
            on_loop = False
        seen.append((on_loop, threading.current_thread() is threading.main_thread()))
        return real(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize("name", ["build_context", "run_scan", "track_ideas", "nightly_update"])
async def test_cpu_bound_steps_run_off_the_event_loop(h, name):
    """Each of these takes seconds on a real universe; run inline they would freeze the API."""
    seen: list[tuple[bool, bool]] = []
    h.monkeypatch.setattr(job, name, _where(getattr(job, name), seen))
    h.store = FakeProductStore(
        scanned=[WED],
        positions=[_position("S03", TARGET - timedelta(days=30), 100.0, 90.0)],
    )

    run = await run_job(h.deps(), "catchup")

    assert run["status"] == "ok"
    assert seen, f"{name} was never called"
    assert seen == [(False, False)] * len(seen)  # no running loop there, and not the main thread


async def test_the_event_loop_keeps_ticking_while_a_session_is_scanned(h):
    """A blocking scan must not stop other coroutines (the API's requests) from running."""
    real = job.run_scan
    ticks: list[float] = []
    stop = asyncio.Event()

    def slow_scan(*args: Any, **kwargs: Any) -> Any:
        clock_time.sleep(0.3)  # stands in for seconds of pandas work
        return real(*args, **kwargs)

    async def heartbeat() -> None:
        while not stop.is_set():
            ticks.append(clock_time.monotonic())
            await asyncio.sleep(0.01)

    h.monkeypatch.setattr(job, "run_scan", slow_scan)
    beat = asyncio.create_task(heartbeat())
    try:
        run = await run_job(h.deps(), "nightly")
    finally:
        stop.set()
        await beat

    assert run["status"] == "ok"
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert len(ticks) > 10 and max(gaps) < 0.2  # a blocked loop would show one gap of >= 0.3 s


async def test_the_context_hook_still_gets_the_target_context_on_the_loop(h):
    """``on_context`` is the API's cache setter: same ctx object, called from the event loop."""
    where: list[bool] = []

    def hook(ctx: ScanContext) -> None:
        try:
            asyncio.get_running_loop()
            where.append(True)
        except RuntimeError:
            where.append(False)
        h.contexts.append(ctx)

    built = h.spy("build_context")
    await run_job(h.deps(on_context=hook), "nightly")

    assert where == [True]
    assert h.contexts == [built[-1][2]]


def test_the_test_panel_scans_monday_19_october_in_an_uptrend():
    assert CTX.session.date() == TARGET and math.isfinite(_close("SPY", TARGET))
    assert Regime(CTX.regime.loc[CTX.session]) is Regime.UPTREND
    assert [d.date() for d in PANEL.dates()[448:452]] == [THU, FRI, TARGET, NEXT]


# ---------------------------------------------------------------------------- Task 8 rulings


def _narrated(idea: Any, ctx: ScanContext, target: date) -> dict[str, str]:
    params = {"breakout": BreakoutParams(), "pullback": PullbackParams()}[idea.setup]
    return {"why": narrate.why(idea, ctx, params), "cancels": narrate.cancels(idea, target, params)}


async def test_each_stored_idea_carries_why_it_was_picked_and_what_cancels_it(h):
    """PR13: Today reads the narration from the stored scan, so it never needs a ScanContext. The
    job writes it at save time, with the target session's context and the pre-registered params
    (the code defaults in this empty repo)."""
    scans = h.spy("run_scan")

    await run_job(h.deps(), "nightly")

    (_, _, result), *_ = scans
    ctx = h.contexts[0]
    payload = h.store.scans[TARGET]["payload"]
    assert result.ideas and result.watch_only == [], "premise: an uptrend scan with ideas"
    for row, idea in zip(payload["ideas"], result.ideas, strict=True):
        assert (row["symbol"], row["setup"]) == (idea.symbol, idea.setup)
        assert {"why": row["why"], "cancels": row["cancels"]} == _narrated(idea, ctx, NEXT)
        assert row["why"].startswith(idea.symbol) and "Tue 20 Oct" in row["cancels"]
    stripped = {
        **payload,
        "ideas": [
            {k: v for k, v in row.items() if k not in ("why", "cancels")}
            for row in payload["ideas"]
        ],
    }
    assert stripped == scan_to_dict(result)  # nothing else about the payload changed


async def test_watch_only_ideas_are_narrated_too(h):
    """A stock page for a watch-only name (downtrend) still says why it was flagged."""
    h.panel = _downtrend(PANEL)
    scans = h.spy("run_scan")

    await run_job(h.deps(), "nightly")

    (_, _, result), *_ = scans
    payload = h.store.scans[TARGET]["payload"]
    assert result.watch_only, "premise: a downtrend parks the gated setups' ideas"
    for row, idea in zip(payload["watch_only"], result.watch_only, strict=True):
        assert {"why": row["why"], "cancels": row["cancels"]} == _narrated(
            idea, h.contexts[0], NEXT
        )


async def test_an_idea_that_cannot_be_narrated_is_stored_without_narration(h):
    """Narration is copy, not the trade decision: a template that fails leaves null, never a
    failed scan."""

    def broken(*args: Any, **kwargs: Any) -> str:
        raise ZeroDivisionError("float division by zero")

    h.monkeypatch.setattr(narrate, "why", broken)
    run = await run_job(h.deps(), "nightly")

    assert run["status"] == "ok"
    rows = h.store.scans[TARGET]["payload"]["ideas"]
    assert rows and all(row["why"] is None and row["cancels"] for row in rows)


async def test_the_target_waits_for_the_scan_delay_not_the_feed_delay(h):
    """A run between close + 16 min (the bars exist) and close + 45 min (the nightly's time) must
    not publish or digest tonight's session on bars that are not settled yet."""
    assert job.SCAN_DELAY_MINUTES == scheduler.SCAN_DELAY_MINUTES == 45  # one shared constant

    early = await run_job(h.deps(now=datetime(2026, 10, 19, 16, 30, tzinfo=ET)), "manual")
    assert early["session"] == FRI and h.store.saved_sessions == [FRI]
    assert "ideas for Mon 19 Oct" in h.sent[-1]

    due = await run_job(h.deps(now=datetime(2026, 10, 19, 16, 45, tzinfo=ET)), "manual")
    assert due["session"] == TARGET and h.store.saved_sessions == [FRI, TARGET]


async def test_a_never_advised_position_is_judged_on_the_target_session_only(h):
    """Ruling: a position the job has never advised is evaluated at the target session only. The
    user records the stop as it is today; testing it against the lows since the open would
    report stop hits from before ATLAS knew of the position."""
    symbol, stop = _dip_on(FRI, [TARGET])
    opened = _opened(20)
    pos = _position(symbol, opened, _close(symbol, opened), stop=stop, trail_atr=50.0)
    walked = position_action(
        {"id": "x", **pos}, dataclasses.replace(CTX, session=pd.Timestamp(FRI))
    )
    assert walked.kind is ActionKind.STOP_HIT, "premise: a walk from the open would hit on Friday"
    tonight = position_action({"id": "x", "trail_atr": DEFAULT_TRAIL, **pos}, CTX)
    assert tonight.kind is ActionKind.HOLD, "premise: Monday alone holds"
    h.store = FakeProductStore(positions=[pos])

    await run_job(h.deps(), "nightly")

    row = _only_position(h.store)
    assert (row["last_action"], row["last_action_text"]) == ("hold", tonight.text)
    assert row["last_action_on"] == TARGET and row["current_stop"] == stop
