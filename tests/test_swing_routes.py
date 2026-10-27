"""Swing API routes (Phase 2 Task 8): Today, a stock, positions, track record, settings, scans, status.

The dashboard's pages read these routes, so the tests pin the shapes and the rules behind them:

* **Today** reads the latest stored scan only (no ``ScanContext``): live ideas, unproven ideas
  and watch-only names apart, each with its narration from the stored payload, its track record
  and whether the user already holds it; the position actions the job stored; the capacity; and
  a stated reason whenever the ideas area would be empty (first run with backfill progress, a
  downtrend, nothing found, or a failed last scan that still shows the previous ideas).
* **The context cache** (``set_context``/``get_context``) is filled by the job, or built once,
  lazily, off the event loop and under a lock, and rebuilt only when the universe filters
  change. Requests never rebuild it themselves.
* **Positions** are validated (stop under entry, positive shares, one open position per symbol,
  a symbol the assets table knows) and advised at once on the cached context, the way the
  nightly job does it.
* **The track record** reads the committed validation reports exactly as Phase 1 writes them
  and never recomputes a backtest; the live record comes from the stored ideas.
* **Run scan now** is 202 with the job id, or 409 while a scan runs, or 503 without Alpaca keys.
* **Status** says whether Alpaca and Telegram are configured without ever returning a value,
  and that automatic order placement is off.
* A database that cannot be reached is a 503 with a readable reason, never a blank page.

Nothing here reaches a database, Alpaca or Telegram: the product store and the bars store are
in-memory fakes, the job and the Telegram send are replaced, and the context is a real
``ScanContext`` built from ``synthetic_universe_panel``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from alert_service.channels.base import DeliveryResult
from fastapi import FastAPI
from fastapi.testclient import TestClient
from signal_service import main as service
from signal_service import swing_context, swing_deps, swing_routes
from swing_engine import job, narrate, scheduler
from swing_engine.config import BreakoutParams, PullbackParams, RiskSettings, UniverseFilter
from swing_engine.indicators import compute_indicators
from swing_engine.job import JobBusy, JobDeps
from swing_engine.panel import Panel, build_panel
from swing_engine.positions import ActionKind, nightly_update, position_action
from swing_engine.product_store import _POSITION_WRITABLE, scan_payload
from swing_engine.scan import ScanContext, SetupSpec, build_context, run_scan
from swing_engine.settings import SwingSettings
from swing_engine.testing import synthetic_kinds, synthetic_universe_panel
from swing_engine.tracking import live_stats
from swing_engine.validation.prereg import Prereg, write_draft
from swing_engine.validation.protocol import protocol_block

REPO = Path(__file__).resolve().parents[1]
WEB = {"X-ATLAS-Client": "web"}  # what the dashboard sends with every write
BUILDS = "/v1/stocks/S05"  # a request that waits for the context (GET /v1/positions never does)
FAKE_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # shaped like a real bot token

PANEL = synthetic_universe_panel(seed=5)
KINDS = synthetic_kinds(PANEL)
SESSION = PANEL.dates()[450].date()  # Thu 23 Sep 2021: an uptrend with ideas from both setups
NEXT = PANEL.dates()[451].date()
CTX = build_context(PANEL, KINDS, SESSION)
PREV = PANEL.dates()[449].date()  # the session before
CTX_PREV = build_context(PANEL, KINDS, PREV)
SETUPS = [SetupSpec(BreakoutParams(), live=True), SetupSpec(PullbackParams(), live=False)]
RESULT = run_scan(PANEL, KINDS, SESSION, RiskSettings(), SETUPS, ctx=CTX)
NOW = datetime(2021, 9, 23, 21, 0, tzinfo=UTC)


def _downtrend(panel: Panel) -> Panel:
    """A copy whose SPY falls all the way, so the scan session is a downtrend."""
    n = len(panel.dates())
    noise = 0.004 * np.random.default_rng(3).standard_normal(n)
    path = 100.0 * np.exp(np.cumsum(np.full(n, -0.002) + noise))
    frames = {f: getattr(panel, f).copy() for f in ("open", "high", "low", "close", "volume")}
    for f in ("open", "high", "low", "close"):
        frames[f]["SPY"] = path
    return Panel(**frames, raw_close=frames["close"].copy())


def _narration(result: Any, ctx: ScanContext) -> dict[tuple[str, str], dict[str, str | None]]:
    return job._narration(result, ctx, NEXT, SETUPS)


def _scan_row(result: Any = RESULT, ctx: ScanContext = CTX, narrated: bool = True) -> dict:
    """The stored scan row as ``product_store.latest_scan`` returns it."""
    payload = scan_payload(result, _narration(result, ctx) if narrated else None)
    return {
        "session": result.session,
        "target_session": NEXT,
        "generated_at": NOW,
        "regime": result.regime.value,
        "breadth": result.breadth,
        "universe_size": result.universe_size,
        "reconstructed": False,
        "payload": json.loads(json.dumps(payload)),  # through JSON, as JSONB gives it back
    }


def _idea_rows(result: Any = RESULT) -> list[dict[str, Any]]:
    """The ``swing_ideas`` rows ``save_scan`` writes for ``result``, with ids."""
    rows = []
    for ideas, parked in ((result.ideas, False), (result.watch_only, True)):
        for idea in ideas:
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "session": result.session,
                    "target_session": NEXT,
                    "symbol": idea.symbol,
                    "setup": idea.setup,
                    "version": idea.version,
                    "live": idea.live,
                    "watch_only": parked,
                    "trigger": idea.trigger,
                    "stop": idea.stop,
                    "atr14": idea.atr14,
                    "reconstructed": False,
                    "track_status": "pending",
                    "entry_date": None,
                    "known_date": None,
                    "r_multiple": None,
                }
            )
    return rows


def _job(status: str, steps: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "kind": "nightly",
        "session": SESSION,
        "started_at": NOW,
        "finished_at": None if status == "running" else NOW + timedelta(minutes=2),
        "status": status,
        "steps": dict(steps or {}),
        "error": kw.get("error"),
    }


# ---------------------------------------------------------------------------- fakes


class FakeStore:
    """The product store's functions the routes use, over dicts (the real one's column rules)."""

    def __init__(
        self,
        *,
        scan: dict[str, Any] | None = None,
        jobs: Sequence[dict[str, Any]] = (),
        positions: Sequence[Mapping[str, Any]] = (),
        settings: SwingSettings | None = None,
        ideas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self.scan = scan
        self.jobs = list(jobs)
        self.settings = settings or SwingSettings()
        self.ideas = list(ideas if ideas is not None else (_idea_rows() if scan else []))
        self.positions: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.calls: list[str] = []
        self.slow = False  # yield to the loop inside the reads and writes (race tests)
        for p in positions:
            self._add(dict(p))

    def _add(self, row: dict[str, Any]) -> dict[str, Any]:
        full = {
            "id": uuid.uuid4(),
            "status": "open",
            "trail_atr": None,
            "idea_id": None,
            "closed_on": None,
            "exit_price": None,
            "exit_reason": None,
            "r_multiple": None,
            "last_action": None,
            "last_action_text": None,
            "last_action_on": None,
            "notes": None,
            "created_at": NOW,
            **row,
        }
        if full.get("current_stop") is None:
            full["current_stop"] = full["initial_stop"]
        self.positions[str(full["id"])] = full
        return dict(full)

    async def get_settings(self) -> SwingSettings:
        self.calls.append("get_settings")
        return self.settings

    async def put_settings(self, settings: SwingSettings) -> SwingSettings:
        self.settings = settings
        return settings

    async def latest_scan(self) -> dict[str, Any] | None:
        self.calls.append("latest_scan")
        return None if self.scan is None else dict(self.scan)

    async def latest_scan_session(self) -> date | None:
        return None if self.scan is None else self.scan["session"]

    async def latest_job(self) -> dict[str, Any] | None:
        return dict(self.jobs[-1]) if self.jobs else None

    async def list_positions(self, status: str | None) -> list[dict[str, Any]]:
        self.calls.append(f"list_positions:{status}")
        if self.slow:
            await asyncio.sleep(0.01)
        return [dict(p) for p in self.positions.values() if status in (None, p["status"])]

    async def get_position(self, position_id: Any) -> dict[str, Any] | None:
        row = self.positions.get(str(position_id))
        return None if row is None else dict(row)

    async def create_position(self, row: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(row) - set(_POSITION_WRITABLE)
        if unknown:
            raise ValueError(f"a position has no writable column(s) {sorted(unknown)}")
        if self.slow:
            await asyncio.sleep(0.01)
        return self._add(dict(row))

    async def update_position(self, position_id: Any, fields: Mapping[str, Any]) -> dict:
        unknown = set(fields) - set(_POSITION_WRITABLE)
        if unknown:
            raise ValueError(f"a position has no writable column(s) {sorted(unknown)}")
        if str(position_id) not in self.positions:
            raise KeyError(position_id)
        self.writes.append((str(position_id), dict(fields)))
        self.positions[str(position_id)].update(fields)
        return dict(self.positions[str(position_id)])

    async def session_ideas(self, session: date) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ideas if r["session"] == session]

    async def get_idea(self, idea_id: Any) -> dict[str, Any] | None:
        return next((dict(r) for r in self.ideas if str(r["id"]) == str(idea_id)), None)

    async def idea_rows(self, setup: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ideas if setup in (None, r["setup"])]


def _tiny_bars() -> pd.DataFrame:
    """Daily bars for TINY, a stock too thin for the liquidity prefilter (so not in the cached
    panel), on the panel's sessions with a 15-session hole (a segment break) 80 sessions back."""
    dates = PANEL.dates()[:451]
    close = 20.0 + 0.01 * np.arange(len(dates))
    frame = pd.DataFrame(
        {
            "ts": pd.DatetimeIndex(dates).tz_localize("UTC"),
            "symbol": "TINY",
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 50_000.0,  # about $1M a day
        }
    )
    hole = frame.index[(frame.index >= len(dates) - 95) & (frame.index < len(dates) - 80)]
    return frame.drop(index=hole).reset_index(drop=True)


class FakeBars:
    """The two reads the routes make of the bars store: the asset list and one stock's bars."""

    def __init__(self) -> None:
        symbols = [*PANEL.symbols(), "TINY"]
        self.assets = pd.DataFrame(
            {
                "symbol": symbols,
                "name": [f"{s} Holdings Inc" for s in symbols],
                "kind": ["etf" if s == "SPY" else "common" for s in symbols],
                "status": "active",
            }
        )
        self.bars = _tiny_bars()
        self.loads: list[tuple[list[str], date, date]] = []

    async def load_assets(self) -> pd.DataFrame:
        return self.assets.copy()

    async def load_daily_bars(
        self, symbols: Sequence[str], start: date, end: date, resolution: str = "1d"
    ) -> pd.DataFrame:
        self.loads.append((list(symbols), start, end))
        day = self.bars["ts"].dt.tz_convert(None).dt.date
        keep = self.bars["symbol"].isin(list(symbols)) & (day >= start) & (day <= end)
        return self.bars.loc[keep].reset_index(drop=True)


class Api:
    """Fakes for every dependency of the routes, and a client for an app with only them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
        self.monkeypatch = monkeypatch
        self.repo = repo
        self.store = FakeStore()
        self.bars = FakeBars()
        self.state: dict[str, Any] = {
            "running": False,
            "next_scan_at": None,
            "disabled_reason": "Automatic scans are off: the API is running under pytest.",
        }
        self.loads: list[tuple[date | None, UniverseFilter, list[str]]] = []
        self.panel = PANEL
        monkeypatch.setattr(swing_context, "_CACHE", swing_context._Cache())
        monkeypatch.setattr(swing_context, "_CACHE_LOCK", asyncio.Lock())
        monkeypatch.setattr(swing_context, "_BUILD", swing_context._Build())
        monkeypatch.setattr(swing_routes, "_CREATE_LOCK", asyncio.Lock())
        monkeypatch.setattr(swing_routes, "_ASSETS", swing_routes._AssetNames())
        monkeypatch.setattr(swing_routes, "store", _Forward(self, "store"))
        monkeypatch.setattr(swing_context, "store", _Forward(self, "store"))
        monkeypatch.setattr(swing_routes, "bars_store", _Forward(self, "bars"))
        monkeypatch.setattr(swing_routes, "repo_root", lambda: self.repo)
        monkeypatch.setattr(swing_routes, "scheduler_state", lambda: dict(self.state))
        monkeypatch.setattr(swing_context, "loader", self.loader)
        self.app = FastAPI()
        self.app.include_router(swing_routes.router)
        self.client = TestClient(self.app, headers=WEB)

    async def loader(
        self, as_of: date | None, universe: UniverseFilter, extra_symbols: Sequence[str] = ()
    ) -> tuple[Panel, dict[str, Any], date]:
        self.loads.append((as_of, universe, list(extra_symbols)))
        day = as_of or self.panel.dates()[-1].date()
        return self.panel, KINDS, day

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get(path, **kw)


class _Forward:
    """Reads ``owner.<name>`` at each call, so a test can swap the fake after the patch."""

    def __init__(self, owner: Api, name: str) -> None:
        self._owner, self._name = owner, name

    def __getattr__(self, attr: str) -> Any:
        return getattr(getattr(self._owner, self._name), attr)


@pytest.fixture
def api(monkeypatch, tmp_path) -> Api:
    """The routes over fakes, in an empty repo (no validation files: both setups untested)."""
    return Api(monkeypatch, tmp_path)


def _live(repo: Path, family: str = "breakout", **report: Any) -> None:
    """A passing v1 report whose sealed pre-registration was registered under today's rules."""
    params = {"breakout": BreakoutParams(), "pullback": PullbackParams()}[family]
    folder = repo / "ml" / "validation" / family
    prereg = Prereg.create(
        family=family, version=f"{family}-v1", params=dataclasses.asdict(params), gate={},
        is_window=[], oos_start="2023-01-01", hypothesis="", created_at="2026-10-19",
        protocol=protocol_block(family),
    )  # fmt: skip
    write_draft(folder / f"{family}-v1.prereg.json", prereg)
    doc = {
        "family": family, "version": f"{family}-v1", "run_index": 1, "freeze_date": "2026-09-30",
        "prereg_sha256": prereg.sha256, "verdict": {"passed": True, "failures": []},
        "regime_filter": {"helps": True}, "ranking": {"rs_helps": True},
        "oos_trades": {"n": 412, "n_distinct": 180, "win_rate": 0.52, "expectancy_r": 0.41},
        **report,
    }  # fmt: skip
    (folder / f"{family}-v1.report.json").write_text(json.dumps(doc))


def _today(api: Api) -> dict[str, Any]:
    response = api.get("/v1/today")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------- the brief's tests


def test_today_first_run_has_empty_reason_and_backfill_progress(api):
    bars = {"status": "running", "done": 380, "total": 1000}
    api.store = FakeStore(jobs=[_job("running", {"assets": {"status": "ok"}, "bars": bars})])

    j = _today(api)

    assert j["empty_reason"] == "first_run" and j["backfill"] == {"done": 380, "total": 1000}
    assert j["regime"] is None and j["ideas"] == j["unproven"] == j["watch_only"] == []
    assert j["freshness"] == {
        "data_as_of": None,
        "ideas_for": None,
        "next_scan_at": None,
        "last_scan_status": "running",
        "failed_step": None,
        "failed_reason": None,
    }


def test_today_tells_a_first_scan_that_is_running_from_one_that_is_downloading(api):
    """D1, the controller's live run: Today said "downloading 10 years of daily data (0%)"
    while the first job was still refreshing the asset list, and kept the download line up
    while the scan ran. ``backfill`` is now only the bars step's progress while it runs, and
    ``running_step`` says which step the run is in."""
    assets = _job("running", {"assets": {"status": "running"}})
    api.store = FakeStore(jobs=[assets])
    j = _today(api)
    assert (j["running_step"], j["backfill"], j["empty_reason"]) == ("assets", None, "first_run")

    scanning = _job("running", {"bars": {"status": "ok", "mode": "backfill", "done": 9,
                                         "total": 9}, "scan": {"status": "running"}})  # fmt: skip
    api.store = FakeStore(jobs=[scanning])
    assert (_today(api)["running_step"], _today(api)["backfill"]) == ("scan", None)

    downloading = _job("running", {"bars": {"status": "running", "done": 3, "total": 9}})
    api.store = FakeStore(jobs=[downloading])
    assert (_today(api)["running_step"], _today(api)["backfill"]) == ("bars", {"done": 3,
                                                                               "total": 9})  # fmt: skip

    api.store = FakeStore(jobs=[_job("ok", {})])
    assert _today(api)["running_step"] is None


def test_today_says_why_automatic_scans_are_off(api):
    api.store = FakeStore()
    j = _today(api)
    assert j["freshness"]["last_scan_status"] == "never"
    assert j["disabled_reason"] == "Automatic scans are off: the API is running under pytest."
    api.state = {"running": True, "next_scan_at": None, "disabled_reason": None}
    assert _today(api)["disabled_reason"] is None


def test_today_splits_live_and_unproven(api):
    _live(api.repo, "breakout")
    held = next(i.symbol for i in RESULT.ideas if i.setup == "breakout")
    api.store = FakeStore(
        scan=_scan_row(),
        positions=[{"symbol": held, "opened_on": SESSION, "entry_price": 1.0, "shares": 1.0,
                    "initial_stop": 0.5}],
    )  # fmt: skip

    j = _today(api)

    assert {i["setup"] for i in j["ideas"]} == {"breakout"} and j["ideas"]
    assert {i["setup"] for i in j["unproven"]} == {"pullback"} and j["unproven"]
    assert all(i["live"] for i in j["ideas"]) and not any(i["live"] for i in j["unproven"])
    assert j["watch_only"] == [] and j["empty_reason"] is None
    assert [i["symbol"] for i in j["ideas"] + j["unproven"]] == [
        i.symbol for i in sorted(RESULT.ideas, key=lambda i: (not i.live, i.rank))
    ]

    first = j["ideas"][0]
    idea = next(i for i in RESULT.ideas if i.symbol == first["symbol"] and i.setup == "breakout")
    assert first["buy_above"] == pytest.approx(idea.trigger)
    assert first["target_2r"] == pytest.approx(idea.trigger + 2 * (idea.trigger - idea.stop))
    assert first["setup_label"] == "Breakout" and first["version"] == "breakout-v1"
    assert first["name"] == f"{idea.symbol} Holdings Inc"
    assert first["shares"] == idea.sizing.shares and first["rank"] == idea.rank
    assert first["why"] == narrate.why(idea, CTX, BreakoutParams())
    assert first["cancels"] == narrate.cancels(idea, NEXT, BreakoutParams())
    stored = next(
        r for r in api.store.ideas if (r["symbol"], r["setup"]) == (idea.symbol, "breakout")
    )
    assert first["id"] == str(stored["id"])
    assert [i["already_held"] for i in j["ideas"]] == [i["symbol"] == held for i in j["ideas"]]
    assert first["track_record"] == {
        "verdict": "live",
        "verdict_text": "Live",
        "win_rate": 0.52,
        "expectancy_r": 0.41,
        "trades": 412,
        "trades_distinct": 180,
        "source": "oos",
    }
    assert j["unproven"][0]["track_record"] == {
        "verdict": "not_tested",
        "verdict_text": "Research only: not yet tested",
        "win_rate": None,
        "expectancy_r": None,
        "trades": None,
        "trades_distinct": None,
        "source": None,
    }


def test_today_says_when_the_latest_scan_was_saved_from_partial_data(api):
    """The incident: a scan saved while most of tonight's bars were missing looked complete.
    Today now carries ``partial_data`` from the job's scan step, for the scan it saved."""
    scan_step = {"status": "ok", "partial": True, "coverage": 0.6, "missing": 400,
                 "expected": 1000}  # fmt: skip
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("ok", {"scan": scan_step})])
    assert _today(api)["partial_data"] == {"missing": 400, "expected": 1000}

    complete = {**scan_step, "partial": False, "coverage": 1.0, "missing": 0}
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("ok", {"scan": complete})])
    assert _today(api)["partial_data"] is None
    other_day = {**_job("ok", {"scan": scan_step}), "session": PREV}  # not the scan shown
    api.store = FakeStore(scan=_scan_row(), jobs=[other_day])
    assert _today(api)["partial_data"] is None


def test_today_downtrend_empty_reason(api):
    panel = _downtrend(PANEL)
    ctx = build_context(panel, KINDS, SESSION)
    result = run_scan(panel, KINDS, SESSION, RiskSettings(), SETUPS, ctx=ctx)
    assert result.watch_only and not result.ideas, "premise: every setup is gated"
    api.store = FakeStore(scan=_scan_row(result, ctx), ideas=_idea_rows(result))

    j = _today(api)

    assert j["empty_reason"] == "downtrend"
    assert j["regime"]["state"] == "downtrend" and j["regime"]["factor"] == 0.0
    assert j["regime"]["label"] == "DOWNTREND, stand aside: no new buy ideas"
    assert j["ideas"] == j["unproven"] == []
    assert [i["symbol"] for i in j["watch_only"]] == [i.symbol for i in result.watch_only]
    assert all(i["watch_only"] and i["shares"] == 0 for i in j["watch_only"])


def test_today_last_scan_failed_still_returns_previous_ideas(api):
    failed = _job(
        "failed",
        {"assets": {"status": "skipped"}, "bars": {"status": "failed", "error": "Alpaca is down"}},
        error="bars: Alpaca is down",
    )
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("ok"), failed])

    j = _today(api)

    assert j["empty_reason"] == "last_scan_failed"
    assert j["freshness"]["last_scan_status"] == "failed"
    assert (j["freshness"]["failed_step"], j["freshness"]["failed_reason"]) == (
        "bars",
        "Alpaca is down",
    )
    assert len(j["ideas"]) + len(j["unproven"]) == len(RESULT.ideas)  # the previous scan's
    assert j["freshness"]["data_as_of"] == SESSION.isoformat()
    assert j["freshness"]["ideas_for"] == NEXT.isoformat()


@pytest.mark.parametrize("step", ["tracking", "positions", "digest"])
def test_a_failure_after_the_scan_was_saved_still_shows_tonights_scan(api, step):
    """The incident: a failure in tracking, positions or the digest read "Showing the previous
    scan" although tonight's scan was saved and is what Today shows. ``last_scan_failed`` is
    now only for a run that stopped before saving its scan; the failed step is still in
    ``freshness`` for the page's separate line."""
    steps = {"scan": {"status": "ok"}, step: {"status": "failed", "error": "Telegram is down"}}
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("failed", steps)])

    j = _today(api)

    assert j["empty_reason"] is None and j["ideas"] + j["unproven"]
    assert j["freshness"]["last_scan_status"] == "failed"
    assert (j["freshness"]["failed_step"], j["freshness"]["failed_reason"]) == (
        step,
        "Telegram is down",
    )


def test_a_post_scan_failure_keeps_the_downtrend_notice(api):
    panel = _downtrend(PANEL)
    ctx = build_context(panel, KINDS, SESSION)
    result = run_scan(panel, KINDS, SESSION, RiskSettings(), SETUPS, ctx=ctx)
    steps = {"positions": {"status": "failed", "error": "boom"}}
    api.store = FakeStore(scan=_scan_row(result, ctx), ideas=_idea_rows(result),
                          jobs=[_job("failed", steps)])  # fmt: skip
    assert _today(api)["empty_reason"] == "downtrend"


@pytest.mark.parametrize("step", ["calendar", "settings", "assets", "bars", "scan"])
def test_a_failure_before_the_scan_was_saved_is_last_scan_failed(api, step):
    steps = {step: {"status": "failed", "error": "down"}}
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("failed", steps)])
    assert _today(api)["empty_reason"] == "last_scan_failed"


def test_scan_run_conflict_returns_409(api, monkeypatch):
    built: list[str] = []

    async def busy(deps: JobDeps, kind: str) -> dict[str, Any]:
        raise JobBusy("a scan is already running")

    monkeypatch.setattr(swing_routes, "make_swing_deps", lambda: _fake_deps(built))
    monkeypatch.setattr(swing_routes, "run_job", busy)

    response = api.client.post("/v1/scan/run")

    assert response.status_code == 409
    assert response.json() == {"detail": "a scan is already running"}


def _position_body(**over: Any) -> dict[str, Any]:
    body = {"symbol": "S13", "opened_on": SESSION.isoformat(), "entry_price": 100.0, "shares": 10}
    body.update({"initial_stop": 95.0, **over})
    return body


@pytest.mark.parametrize(
    ("over", "field"),
    [
        pytest.param({"initial_stop": 100.0}, None, id="stop-at-entry"),
        pytest.param({"initial_stop": 101.0}, None, id="stop-above-entry"),
        pytest.param({"shares": 0}, "shares", id="zero-shares"),
        pytest.param({"shares": -5}, "shares", id="negative-shares"),
        pytest.param({"entry_price": 0}, "entry_price", id="zero-entry"),
        pytest.param({"initial_stop": None}, "initial_stop", id="no-stop-and-no-idea"),
        pytest.param({"symbol": "NOPE"}, "symbol", id="unknown-symbol"),
        pytest.param({"broker": "x"}, "broker", id="unknown-field"),
    ],
)
def test_position_validation_422(api, over, field):
    response = api.client.post("/v1/positions", json=_position_body(**over))

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail  # one error shape for every 422
    if field is not None:
        assert any(field in err["loc"] for err in detail), detail
    assert api.store.positions == {}


def test_close_position_computes_r(api):
    opened = SESSION - timedelta(days=14)
    entry = float(PANEL.close.at[pd.Timestamp(opened), "S13"])  # on the panel's price scale
    pid = _add_position(api, entry=entry, stop=entry * 0.95, opened=opened)

    response = api.client.post(
        f"/v1/positions/{pid}/close",
        json={"closed_on": SESSION.isoformat(), "exit_price": entry * 1.125},
    )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "closed" and out["closed_on"] == SESSION.isoformat()
    assert out["exit_price"] == pytest.approx(entry * 1.125)
    assert out["r_multiple"] == pytest.approx((1.125 - 1.0) / (1.0 - 0.95)) == 2.5
    assert api.store.positions[pid]["r_multiple"] == pytest.approx(2.5)


def test_settings_put_rejects_zero_risk(api):
    response = api.client.put("/v1/settings", json={**SwingSettings().model_dump(), "risk_pct": 0})

    assert response.status_code == 422
    assert any("risk_pct" in err["loc"] for err in response.json()["detail"])
    assert api.store.settings == SwingSettings()  # nothing saved


def test_stock_unknown_symbol_404(api):
    swing_context.set_context(CTX, UniverseFilter())
    response = api.get("/v1/stocks/NOPE")
    assert response.status_code == 404
    assert "NOPE" in response.json()["detail"]


def test_system_status_reports_auto_execution_false(api):
    api.store = FakeStore(scan=_scan_row(), jobs=[_job("ok", {"scan": {"status": "ok"}})])

    j = api.get("/v1/system/status").json()

    assert j["auto_execution"] is False
    assert j["data_as_of"] == SESSION.isoformat()
    assert j["last_scan"]["status"] == "ok" and j["last_scan"]["steps"] == {
        "scan": {"status": "ok"}
    }
    assert j["scheduler_running"] is False and "pytest" in j["disabled_reason"]


# ---------------------------------------------------------------------------- Today (rulings)


def test_unproven_and_watch_only_rows_stay_apart_and_watch_only_never_count_as_unproven(api):
    panel = _downtrend(PANEL)
    ctx = build_context(panel, KINDS, SESSION)
    ungated = [SetupSpec(BreakoutParams(), live=False, regime_gate=False), SETUPS[1]]
    result = run_scan(panel, KINDS, SESSION, RiskSettings(), ungated, ctx=ctx)
    assert result.ideas and result.watch_only, "premise: an ungated setup still lists ideas"
    api.store = FakeStore(scan=_scan_row(result, ctx), ideas=_idea_rows(result))

    j = _today(api)

    assert [i["symbol"] for i in j["unproven"]] == [i.symbol for i in result.ideas]
    assert all(i["regime_warning"] for i in j["unproven"])  # a non-uptrend market
    assert [i["symbol"] for i in j["watch_only"]] == [i.symbol for i in result.watch_only]
    assert j["empty_reason"] == "downtrend"


def test_today_with_nothing_found_says_so(api):
    empty = dataclasses.replace(RESULT, ideas=[], watch_only=[])
    api.store = FakeStore(scan=_scan_row(empty), ideas=[])
    assert _today(api)["empty_reason"] == "no_candidates"


def test_today_reports_whether_any_setup_is_live(api):
    """The dashboard says "No setup has passed testing yet" only when no family's verdict is Live.

    Incident: it said so whenever there were no live ideas, which is untrue on a night a Live
    family simply found nothing. ``any_live_setup`` is the family verdict, not the idea count.
    """
    # No family has passed: ideas exist but none is live, and so is the first run (no scan).
    api.store = FakeStore(scan=_scan_row())
    j = _today(api)
    assert j["any_live_setup"] is False and j["ideas"] == [] and j["unproven"]
    api.store = FakeStore()
    assert _today(api)["any_live_setup"] is False

    # Breakout is Live but tonight's only ideas are the unproven pullbacks: nothing live to show.
    _live(api.repo, "breakout")
    pullbacks = [i for i in RESULT.ideas if i.setup == "pullback"]
    only_pullback = dataclasses.replace(RESULT, ideas=pullbacks, watch_only=[])
    api.store = FakeStore(scan=_scan_row(only_pullback))
    j = _today(api)
    assert j["any_live_setup"] is True and j["ideas"] == [] and j["unproven"]

    # Breakout is Live and has ideas tonight.
    api.store = FakeStore(scan=_scan_row())
    j = _today(api)
    assert j["any_live_setup"] is True and j["ideas"]

    # Breakout is Live and found nothing tonight.
    empty = dataclasses.replace(RESULT, ideas=[], watch_only=[])
    api.store = FakeStore(scan=_scan_row(empty), ideas=[])
    j = _today(api)
    assert j["any_live_setup"] is True and j["empty_reason"] == "no_candidates"


def test_an_older_payload_without_narration_reads_null(api):
    api.store = FakeStore(scan=_scan_row(narrated=False))
    j = _today(api)
    assert j["unproven"] and all(i["why"] is None and i["cancels"] is None for i in j["unproven"])


def test_today_never_needs_a_context(api, monkeypatch):
    async def must_not_build(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Today read the context")

    monkeypatch.setattr(swing_routes, "context_within", must_not_build)
    monkeypatch.setattr(swing_context, "cached_context", must_not_build)
    api.store = FakeStore(scan=_scan_row())
    assert _today(api)["unproven"]


def test_first_run_only_when_no_scan_and_backfill_only_while_the_job_runs(api):
    finished = _job("ok", {"bars": {"status": "ok", "done": 1000, "total": 1000}})
    api.store = FakeStore(jobs=[finished])
    j = _today(api)
    assert j["empty_reason"] == "first_run" and j["backfill"] is None
    assert j["freshness"]["last_scan_status"] == "ok"

    running = _job("running", {"bars": {"status": "running", "done": 5, "total": 10}})
    api.store = FakeStore(scan=_scan_row(), jobs=[running])
    j = _today(api)
    assert j["empty_reason"] is None and j["backfill"] == {"done": 5, "total": 10}

    api.store = FakeStore()
    assert _today(api)["freshness"]["last_scan_status"] == "never"


def test_today_lists_the_stored_position_actions_and_the_capacity(api):
    api.store = FakeStore(
        scan=_scan_row(),
        settings=SwingSettings(equity=50_000.0, max_positions=5, max_total_risk_pct=0.05),
        positions=[
            {"symbol": "S01", "opened_on": SESSION, "entry_price": 100.0, "shares": 10.0,
             "initial_stop": 90.0, "current_stop": 95.0, "last_action": "raise_stop",
             "last_action_text": "raise stop to 95.00 (trail)", "last_action_on": SESSION},
            {"symbol": "S02", "opened_on": SESSION, "entry_price": 50.0, "shares": 4.0,
             "initial_stop": 40.0, "current_stop": 55.0},  # above entry: nothing at risk
            {"symbol": "S03", "opened_on": SESSION, "entry_price": 20.0, "shares": 1.0,
             "initial_stop": 18.0, "status": "closed"},
        ],
    )  # fmt: skip
    state_time = datetime(2021, 9, 24, 20, 45, tzinfo=UTC)
    api.state = {"running": True, "next_scan_at": state_time, "disabled_reason": None}

    j = _today(api)

    (action,) = j["positions"]  # S02 was never advised: no action to show
    assert action["symbol"] == "S01" and action["kind"] == "raise_stop"
    assert action["stop"] == 95.0 and action["text"] == "raise stop to 95.00 (trail)"
    assert uuid.UUID(action["position_id"])
    assert j["capacity"] == {
        "open_positions": 2,
        "max_positions": 5,
        "open_risk_dollars": 50.0,  # 10 x (100 - 95) + 4 x max(0, 50 - 55)
        "max_risk_dollars": 2500.0,
    }
    assert datetime.fromisoformat(j["freshness"]["next_scan_at"]) == state_time


def test_nan_values_reach_the_dashboard_as_null(api):
    api.store = FakeStore(scan=_scan_row(dataclasses.replace(RESULT, breadth=float("nan"))))
    assert _today(api)["regime"]["breadth"] is None


def test_an_unreachable_database_is_a_503_with_a_reason(api):
    class Down(FakeStore):
        async def latest_scan(self) -> dict[str, Any] | None:
            raise ConnectionRefusedError("[Errno 61] Connect call failed ('127.0.0.1', 5432)")

    api.store = Down()

    response = api.get("/v1/today")

    assert response.status_code == 503
    assert response.json()["detail"].startswith("Can't reach the ATLAS database: ")
    assert "5432" in response.json()["detail"]


# ---------------------------------------------------------------------------- the context cache


def test_the_context_is_built_once_lazily_off_the_loop_with_the_users_universe(api, monkeypatch):
    settings = SwingSettings(min_price=12.0)
    api.store = FakeStore(
        scan=_scan_row(),
        settings=settings,
        positions=[{"symbol": "S05", "opened_on": SESSION, "entry_price": 50.0, "shares": 1.0,
                    "initial_stop": 45.0}],
    )  # fmt: skip
    on_loop: list[bool] = []
    real = swing_context.build_context

    def watched(*args: Any, **kwargs: Any) -> ScanContext:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:  # no running loop in this thread: a worker thread
            on_loop.append(False)
        return real(*args, **kwargs)

    monkeypatch.setattr(swing_context, "build_context", watched)

    with TestClient(api.app, headers=WEB) as client:  # one event loop, as the real API has
        first = client.get("/v1/positions").json()
        assert first[0]["last_close"] is None  # a cold cache: no wait, the build starts behind it
        for _ in range(500):
            if swing_context._CACHE.ctx is not None:
                break
            threading.Event().wait(0.01)
        for _ in range(2):
            assert client.get("/v1/positions").json()[0]["last_close"] is not None

    assert api.loads == [(SESSION, settings.universe(), ["S05"])]  # once, at the latest scan
    assert on_loop == [False]  # built in a worker thread, never on the event loop


async def test_concurrent_first_requests_build_the_context_once(api, monkeypatch):
    api.store = FakeStore(scan=_scan_row())
    started = asyncio.Event()

    async def slow_loader(as_of, universe, extra_symbols=()):
        api.loads.append((as_of, universe, list(extra_symbols)))
        started.set()
        await asyncio.sleep(0.05)
        return PANEL, KINDS, SESSION

    monkeypatch.setattr(swing_context, "loader", slow_loader)

    got = await asyncio.gather(*(swing_context.get_context() for _ in range(5)))

    assert len(api.loads) == 1 and all(ctx is got[0] for ctx in got)
    assert got[0].session.date() == SESSION


async def test_the_job_context_is_used_and_a_universe_change_rebuilds(api):
    api.store = FakeStore(scan=_scan_row())
    swing_context.set_context(CTX)  # what JobDeps.on_context does after a scan

    assert await swing_context.get_context() is CTX and api.loads == []

    api.store.settings = SwingSettings(min_dollar_volume=30_000_000.0)
    rebuilt = await swing_context.get_context()

    assert rebuilt is not CTX and api.loads == [(SESSION, api.store.settings.universe(), [])]
    assert await swing_context.get_context() is rebuilt and len(api.loads) == 1


def test_changing_the_universe_in_settings_drops_the_cached_context(api):
    swing_context.set_context(CTX, UniverseFilter())
    body = {**SwingSettings().model_dump(), "min_price": 15.0}

    assert api.client.put("/v1/settings", json=body).status_code == 200

    assert swing_context._CACHE.ctx is None
    swing_context.set_context(CTX, UniverseFilter())
    assert api.client.put("/v1/settings", json={**body, "equity": 30_000.0}).status_code == 200
    assert swing_context._CACHE.ctx is CTX  # sizing changes keep it


def test_a_job_that_started_before_a_universe_change_does_not_refill_the_cache(api, monkeypatch):
    monkeypatch.setattr(swing_routes.job, "is_running", lambda: True)
    body = {**SwingSettings().model_dump(), "min_price": 15.0}
    assert api.client.put("/v1/settings", json=body).status_code == 200

    swing_context.set_context(CTX)  # the running job's context, built with the old filters
    assert swing_context._CACHE.ctx is None
    swing_context.set_context(CTX)  # the next job's
    assert swing_context._CACHE.ctx is CTX


async def test_a_failed_build_returns_none_and_is_not_retried_at_once(api, monkeypatch):
    calls: list[int] = []

    async def broken(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("no assets stored: run `python -m swing_engine backfill` first")

    monkeypatch.setattr(swing_context, "loader", broken)

    assert await swing_context.get_context() is None
    assert await swing_context.get_context() is None
    assert calls == [1]


def test_job_deps_always_feed_the_cache(monkeypatch):
    made: dict[str, Any] = {}
    client = object()

    def fake_live_deps(c: object, repo: Path) -> JobDeps:
        made["client"], made["repo"] = c, repo
        return _fake_deps([])

    monkeypatch.setattr(swing_deps, "AlpacaData", lambda: client)
    monkeypatch.setattr(swing_deps, "live_deps", fake_live_deps)

    deps = swing_deps.make_swing_deps()

    assert deps.on_context is swing_context.set_context
    assert made["client"] is client and made["repo"] == REPO == swing_deps.repo_root()


def test_the_lifespan_warms_the_cache_after_starting_the_scheduler(monkeypatch):
    order: list[str] = []

    async def fake_scheduler(make_deps: object) -> None:
        order.append("scheduler")
        await asyncio.Event().wait()

    async def warm() -> None:
        order.append("warm")

    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.setattr(service, "load_env", lambda: None)
    monkeypatch.setattr(service, "swing_scheduler", fake_scheduler)
    monkeypatch.setattr(service, "warm_swing_context", warm)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)

    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
        for _ in range(100):
            if len(order) == 2:
                break
            threading.Event().wait(0.01)

    assert order == ["scheduler", "warm"]


class _JobRunStore:
    """The one call the lifespan makes of the job-run store at start-up."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.errors: list[str] = []

    async def fail_running_jobs(self, error: str) -> int:
        self.order.append("close interrupted runs")
        self.errors.append(error)
        return 1


def test_the_lifespan_closes_interrupted_runs_before_the_scheduler_starts(monkeypatch):
    """The incident: an API stopped mid-job left its run ``running``, which locked "Run scan
    now" (409) and Today's progress for good. Start-up closes such runs as failed first."""
    order: list[str] = []
    fake = _JobRunStore(order)

    async def fake_scheduler(make_deps: object) -> None:
        order.append("scheduler")
        await asyncio.Event().wait()

    async def warm() -> None:
        pass

    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.setattr(service, "load_env", lambda: None)
    monkeypatch.setattr(service, "swing_scheduler", fake_scheduler)
    monkeypatch.setattr(service, "warm_swing_context", warm)
    monkeypatch.setattr(service, "swing_store", fake)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)

    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
        for _ in range(100):
            if "scheduler" in order:
                break
            threading.Event().wait(0.01)

    assert order == ["close interrupted runs", "scheduler"]
    assert fake.errors == ["interrupted: the API stopped during the run"]


def test_under_pytest_the_lifespan_never_touches_the_real_job_store(monkeypatch):
    from swing_engine import product_store

    called: list[str] = []

    async def real_db(error: str) -> int:
        called.append(error)
        return 0

    monkeypatch.setattr(product_store, "fail_running_jobs", real_db)
    monkeypatch.setattr(service, "load_env", lambda: None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)  # even with the env var gone
    monkeypatch.setattr(service, "swing_scheduler", lambda make_deps: asyncio.sleep(0))
    monkeypatch.setattr(service, "warm_swing_context", lambda: asyncio.sleep(0))
    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
    assert called == []


def test_under_pytest_the_lifespan_never_warms_the_cache(monkeypatch):
    warmed: list[bool] = []

    async def warm() -> None:
        warmed.append(True)

    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.setattr(service, "load_env", lambda: None)
    monkeypatch.setattr(service, "warm_swing_context", warm)
    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
    assert warmed == []


async def test_the_warm_up_fails_soft_when_the_database_is_down(api):
    class Down(FakeStore):
        async def get_settings(self) -> SwingSettings:
            raise ConnectionRefusedError("[Errno 61] Connect call failed")

    api.store = Down()
    await swing_context.warm_context()  # logs, never raises
    assert swing_context._CACHE.ctx is None


# ---------------------------------------------------------------------------- the stock page


def _stock(api: Api, symbol: str) -> dict[str, Any]:
    response = api.get(f"/v1/stocks/{symbol}")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_stock_with_an_idea_has_its_chart_levels_narration_and_checklist(api):
    idea = next(i for i in RESULT.ideas if i.setup == "breakout")
    api.store = FakeStore(scan=_scan_row())
    swing_context.set_context(CTX, UniverseFilter())

    j = _stock(api, idea.symbol.lower())  # any letter case

    assert j["symbol"] == idea.symbol and j["name"] == f"{idea.symbol} Holdings Inc"
    assert j["session"] == SESSION.isoformat()
    closes = PANEL.close[idea.symbol].loc[: pd.Timestamp(SESSION)].dropna()
    assert len(j["bars"]) == 300 and j["bars"][-1]["time"] == SESSION.isoformat()
    assert j["bars"][-1]["close"] == pytest.approx(closes.iloc[-1])
    assert [b["time"] for b in j["bars"]] == [d.date().isoformat() for d in closes.index[-300:]]
    sma50 = CTX.ind.sma50[idea.symbol]
    assert j["lines"]["sma50"][-1] == {
        "time": SESSION.isoformat(),
        "value": pytest.approx(sma50.at[pd.Timestamp(SESSION)]),
    }
    assert set(j["lines"]) == {"sma50", "sma200", "ema20"}
    assert len(j["checklist"]) == 14 and all(c["passed"] for c in j["checklist"][:12])
    assert j["idea"]["symbol"] == idea.symbol and j["idea"]["setup"] == "breakout"
    assert j["why"] == narrate.why(idea, CTX, BreakoutParams()) == j["idea"]["why"]
    assert j["cancels"] == narrate.cancels(idea, NEXT, BreakoutParams())
    assert j["levels"] == {
        "trigger": pytest.approx(idea.trigger),
        "stop": pytest.approx(idea.stop),
        "target_2r": pytest.approx(idea.trigger + 2 * (idea.trigger - idea.stop)),
        "trail": None,
    }


def test_a_held_stock_shows_the_trail(api):
    symbol = "S01"
    assert symbol not in {i.symbol for i in RESULT.ideas}  # premise: no idea for it
    api.store = FakeStore(
        scan=_scan_row(),
        positions=[{"symbol": symbol, "opened_on": SESSION - timedelta(days=30),
                    "entry_price": 100.0, "shares": 3.0, "initial_stop": 90.0,
                    "current_stop": 96.5}],
    )  # fmt: skip
    swing_context.set_context(CTX, UniverseFilter())

    j = _stock(api, symbol)

    assert j["idea"] is None and j["why"] is None and j["cancels"] is None
    assert j["levels"] == {"trigger": None, "stop": 90.0, "target_2r": 120.0, "trail": 96.5}


def test_the_checklist_uses_the_pre_registered_params_and_the_contexts_universe(api):
    """Task 4 note: rules 12-14 must use the params the ideas were scanned with."""
    folder = api.repo / "ml" / "validation" / "breakout"
    params = BreakoutParams(contraction_max=0.7)
    prereg = Prereg.create(
        family="breakout", version="breakout-v1", params=dataclasses.asdict(params), gate={},
        is_window=[], oos_start="2023-01-01", hypothesis="", created_at="2026-10-19",
        protocol=protocol_block("breakout"),
    )  # fmt: skip
    write_draft(folder / "breakout-v1.prereg.json", prereg)
    report = {"family": "breakout", "version": "breakout-v1", "run_index": 1,
              "freeze_date": "2026-09-30", "regime_filter": {"helps": True},
              "ranking": {"rs_helps": True}, "verdict": {"passed": False, "failures": ["x"]}}  # fmt: skip
    (folder / "breakout-v1.report.json").write_text(json.dumps(report))
    universe = dataclasses.replace(UniverseFilter(), min_price=12.0)
    swing_context.set_context(build_context(PANEL, KINDS, SESSION, universe), universe)
    api.store = FakeStore(scan=_scan_row(), settings=SwingSettings(min_price=12.0))

    rules = [c["rule"] for c in _stock(api, "S13")["checklist"]]

    assert "Breakout: quiet stretch (ATR10 / ATR50 at most 0.7)" in rules
    assert "Price at least $12" in rules


def test_a_known_stock_below_the_prefilter_reads_its_stored_bars(api):
    swing_context.set_context(CTX, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())

    j = _stock(api, "TINY")

    assert j["idea"] is None and j["levels"] is None and j["session"] == SESSION.isoformat()
    ((symbols, start, end),) = api.bars.loads
    assert symbols == ["TINY"] and end == SESSION and start < SESSION - timedelta(days=500)
    stored = _tiny_bars()
    assert len(j["bars"]) == 300 and j["bars"][-1]["close"] == pytest.approx(
        stored["close"].iloc[-1]
    )
    (item,) = j["checklist"]
    median = float((stored["close"] * stored["volume"]).iloc[-50:].median())
    assert item["rule"] == "50-day median dollar volume at least $20M" and item["passed"] is False
    assert item["value"] == f"${median / 1e6:,.1f}M" == "$1.2M" and item["threshold"] == "$20.0M"
    # The lines are computed per segment on the market's sessions: the EMA20 starts over after
    # the 15-session hole instead of bridging it.
    ema_days = {p["time"] for p in j["lines"]["ema20"]}
    after_hole = [d.date().isoformat() for d in PANEL.dates()[451 - 80 : 451 - 80 + 19]]
    assert not ema_days & set(after_hole)
    assert PANEL.dates()[450].date().isoformat() in ema_days


def test_the_store_fallback_lines_match_the_indicators_of_the_reindexed_bars(api):
    swing_context.set_context(CTX, UniverseFilter())
    j = _stock(api, "TINY")
    panel = build_panel(_tiny_bars())
    frames = {
        f: getattr(panel, f).reindex(PANEL.dates()[:451])
        for f in ("open", "high", "low", "close", "volume")
    }
    sma50 = compute_indicators(Panel(**frames)).sma50["TINY"].dropna()
    assert j["lines"]["sma50"][-1]["value"] == pytest.approx(sma50.iloc[-1])


def test_the_stock_page_needs_a_context(api, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no SPY bars stored")

    monkeypatch.setattr(swing_context, "loader", broken)
    response = api.get("/v1/stocks/S13")
    assert response.status_code == 503 and "scan" in response.json()["detail"]


# ---------------------------------------------------------------------------- positions


def _add_position(
    api: Api, *, symbol: str = "S13", entry: float, stop: float, opened: date, **kw: Any
) -> str:
    row = api.store._add(
        {"symbol": symbol, "opened_on": opened, "entry_price": entry, "shares": 10.0,
         "initial_stop": stop, **kw}
    )  # fmt: skip
    return str(row["id"])


def test_creating_a_position_advises_it_at_once_on_the_cached_context(api):
    swing_context.set_context(CTX, UniverseFilter())
    opened = PANEL.dates()[430].date()
    entry = float(PANEL.close.at[pd.Timestamp(opened), "S13"])
    stop = round(entry * 0.8, 2)

    response = api.client.post(
        "/v1/positions", json=_position_body(opened_on=opened.isoformat(), entry_price=entry,
                                             initial_stop=stop)
    )  # fmt: skip

    assert response.status_code == 201, response.text
    out = response.json()
    expected = nightly_update(
        {"id": out["id"], "symbol": "S13", "opened_on": opened, "entry_price": entry,
         "shares": 10.0, "initial_stop": stop, "current_stop": stop,
         "trail_atr": BreakoutParams().trail_atr},  # the empty repo: breakout's code defaults
        CTX,
    )  # fmt: skip
    assert out["last_action"] == expected.action.kind.value
    assert out["last_action_text"] == expected.action.text
    assert out["last_action_on"] == SESSION.isoformat()
    assert out["current_stop"] == pytest.approx(expected.fields.get("current_stop", stop))
    assert out["initial_stop"] == stop and out["status"] == "open"
    assert out["name"] == "S13 Holdings Inc"
    last = float(PANEL.close.at[pd.Timestamp(SESSION), "S13"])
    assert out["last_close"] == pytest.approx(last)
    assert out["r_now"] == pytest.approx((last - entry) / (entry - stop))
    held = PANEL.close["S13"].loc[pd.Timestamp(opened) : pd.Timestamp(SESSION)].dropna()
    assert out["held_sessions"] == len(held) == 21


def test_a_position_opened_on_the_scan_session_holds(api):
    swing_context.set_context(CTX, UniverseFilter())
    out = api.client.post("/v1/positions", json=_position_body(opened_on=NEXT.isoformat())).json()
    assert (out["last_action"], out["last_action_text"]) == ("hold", "hold, stop 95.00")
    assert out["current_stop"] == 95.0


def test_without_a_context_the_position_is_stored_without_advice(api, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no SPY bars stored")

    monkeypatch.setattr(swing_context, "loader", broken)

    response = api.client.post("/v1/positions", json=_position_body())

    assert response.status_code == 201
    out = response.json()
    assert out["last_action"] is None and out["current_stop"] == out["initial_stop"] == 95.0
    assert out["r_now"] is None and out["held_sessions"] is None and out["last_close"] is None


def test_a_position_from_an_idea_takes_its_stop(api):
    swing_context.set_context(CTX, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())
    idea = next(r for r in api.store.ideas if r["setup"] == "breakout")
    body = {"symbol": idea["symbol"], "opened_on": NEXT.isoformat(),
            "entry_price": idea["trigger"] + 0.05, "shares": 5, "idea_id": str(idea["id"])}  # fmt: skip

    out = api.client.post("/v1/positions", json=body).json()

    assert out["initial_stop"] == pytest.approx(idea["stop"]) == out["current_stop"]
    assert out["idea_id"] == str(idea["id"])
    other = {**body, "symbol": "S01"}
    response = api.client.post("/v1/positions", json=other)
    assert response.status_code == 422 and "idea_id" in str(response.json()["detail"])


def test_a_position_from_an_idea_stores_the_preregistered_trail(api):
    """Trailing-stop parity (the incident): positions trailed at ``BreakoutParams()``'s 3.0 ATR
    while the sealed pre-registrations, the narration and tracking used 3.5. A create linked to
    an idea stores the trail of that idea's setup and version, read from the real committed
    preregs; one with no idea stores the breakout family's pre-registered trail."""
    api.repo = REPO
    swing_context.set_context(CTX, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())
    idea = next(r for r in api.store.ideas if r["setup"] == "pullback")
    assert idea["version"] == "pullback-v1"  # premise: a version with a committed prereg
    body = {"symbol": idea["symbol"], "opened_on": NEXT.isoformat(),
            "entry_price": idea["trigger"] + 0.05, "shares": 5, "idea_id": str(idea["id"])}  # fmt: skip

    out = api.client.post("/v1/positions", json=body).json()

    assert api.store.positions[out["id"]]["trail_atr"] == 3.5
    unlinked = api.client.post("/v1/positions", json=_position_body(symbol="S01")).json()
    assert api.store.positions[unlinked["id"]]["trail_atr"] == 3.5
    assert BreakoutParams().trail_atr == 3.0  # premise: the code default is not what is stored


def test_the_trail_told_stored_and_tracked_for_one_idea_is_the_same(api, monkeypatch):
    """One idea, three modules: the trail ``narrate.cancels`` tells the user (as the job stores
    it, with ``default_setups``' parameters), the ``trail_atr`` of the position the user records
    from it, and the trail ``track_ideas`` simulates it with are one number."""
    from swing_engine import tracking
    from swing_engine.scan import default_setups

    api.repo = REPO
    swing_context.set_context(CTX, UniverseFilter())
    setups = default_setups(REPO)
    result = run_scan(PANEL, KINDS, SESSION, RiskSettings(), setups, ctx=CTX)
    idea = next(i for i in result.ideas if i.setup == "breakout")
    told = job._narration(result, CTX, NEXT, setups)[(idea.symbol, "breakout")]["cancels"]
    api.store = FakeStore(scan=_scan_row(result), ideas=_idea_rows(result))
    row = next(r for r in api.store.ideas if (r["symbol"], r["setup"]) == (idea.symbol, "breakout"))
    body = {"symbol": idea.symbol, "opened_on": NEXT.isoformat(),
            "entry_price": idea.trigger + 0.05, "shares": 5, "idea_id": str(row["id"])}  # fmt: skip
    stored = api.store.positions[api.client.post("/v1/positions", json=body).json()["id"]]
    trails: list[float] = []

    def spy(frame: Any, signal: Any, trigger: float, stop: float, trail: float, *a: Any, **k: Any):
        trails.append(trail)
        return real(frame, signal, trigger, stop, trail, *a, **k)

    real = tracking.simulate_trade
    monkeypatch.setattr(tracking, "simulate_trade", spy)
    tracking.track_ideas([row], CTX, set(), REPO)

    assert told is not None and f"trails {stored['trail_atr']:.1f} ATR" in told
    assert stored["trail_atr"] == trails[0] == 3.5


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("post", "/v1/positions", None, id="create-position"),
        pytest.param("patch", f"/v1/positions/{uuid.UUID(int=1)}", {"notes": "x"}, id="patch"),
        pytest.param("post", f"/v1/positions/{uuid.UUID(int=1)}/close", None, id="close"),
        pytest.param("put", "/v1/settings", {"equity": 1.0}, id="settings"),
        pytest.param("post", "/v1/settings/telegram-test", None, id="telegram-test"),
        pytest.param("post", "/v1/scan/run", None, id="scan-run"),
    ],
)
def test_a_write_without_the_dashboard_header_is_refused(api, monkeypatch, method, path, body):
    """The incident: the API answered any local page, so a site open in the browser could post
    a cross-site form to localhost and record a position, change the settings or start a scan.
    Writes now need ``X-ATLAS-Client: web``, which a cross-site form cannot set; nothing runs
    without it, not even the body's validation."""
    ran: list[str] = []
    monkeypatch.setattr(swing_routes, "make_swing_deps", lambda: ran.append("deps"))
    monkeypatch.setattr(swing_routes, "send_digest", lambda text: ran.append(text))
    bare = TestClient(api.app)  # no X-ATLAS-Client header

    response = getattr(bare, method)(path, json=body or _position_body())

    assert response.status_code == 403
    assert response.json() == {"detail": "missing X-ATLAS-Client header"}
    assert ran == [] and api.store.positions == {} and api.store.settings == SwingSettings()
    wrong = getattr(bare, method)(path, json=body or _position_body(),
                                  headers={"X-ATLAS-Client": "curl"})  # fmt: skip
    assert wrong.status_code == 403


def test_reads_need_no_header(api):
    bare = TestClient(api.app)
    assert bare.get("/v1/positions").status_code == 200
    assert bare.get("/v1/settings").status_code == 200


def test_the_api_answers_only_the_allowed_host_names(monkeypatch):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 reaches the API with that
    Host. Only ``ATLAS_ALLOWED_HOSTS`` (default localhost, 127.0.0.1, testserver) get through."""
    client = TestClient(service.app)
    assert client.get("/healthz").status_code == 200  # testserver
    assert client.get("/healthz", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/healthz", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400
    assert service.allowed_hosts() == ["localhost", "127.0.0.1", "testserver"]
    monkeypatch.setenv("ATLAS_ALLOWED_HOSTS", " atlas.local , localhost ")
    assert service.allowed_hosts() == ["atlas.local", "localhost"]


def test_one_open_position_per_symbol(api):
    _add_position(api, entry=100.0, stop=95.0, opened=SESSION)
    response = api.client.post("/v1/positions", json=_position_body())
    assert response.status_code == 409 and "S13" in response.json()["detail"]
    assert len(api.store.positions) == 1


def test_positions_list_by_status(api):
    _add_position(api, entry=100.0, stop=95.0, opened=SESSION)
    _add_position(api, symbol="S01", entry=50.0, stop=45.0, opened=SESSION, status="closed",
                  closed_on=SESSION, exit_price=55.0, r_multiple=1.0)  # fmt: skip

    assert [p["symbol"] for p in api.get("/v1/positions?status=open").json()] == ["S13"]
    closed = api.get("/v1/positions?status=closed").json()
    assert [p["symbol"] for p in closed] == ["S01"] and closed[0]["r_now"] is None
    assert len(api.get("/v1/positions").json()) == 2
    assert api.get("/v1/positions?status=maybe").status_code == 422


def test_r_now_and_held_sessions_come_from_the_cached_panel(api):
    swing_context.set_context(CTX, UniverseFilter())
    opened = PANEL.dates()[440].date()
    _add_position(api, entry=100.0, stop=90.0, opened=opened)
    _add_position(api, symbol="ZZZZ", entry=10.0, stop=9.0, opened=opened)  # not in the panel

    rows = {p["symbol"]: p for p in api.get("/v1/positions?status=open").json()}

    last = float(PANEL.close.at[pd.Timestamp(SESSION), "S13"])
    assert rows["S13"]["r_now"] == pytest.approx((last - 100.0) / 10.0)
    assert rows["S13"]["held_sessions"] == 11
    assert rows["ZZZZ"]["r_now"] is None and rows["ZZZZ"]["held_sessions"] is None


def test_patch_validates_and_re_advises_an_edited_stop_without_testing_todays_low(api):
    swing_context.set_context(CTX, UniverseFilter())
    opened = PANEL.dates()[440].date()
    entry = float(PANEL.close.at[pd.Timestamp(opened), "S13"])
    pid = _add_position(api, entry=entry, stop=entry * 0.8, opened=opened, last_action="hold",
                        last_action_text="hold, stop x", last_action_on=SESSION)  # fmt: skip
    low = float(PANEL.low.at[pd.Timestamp(SESSION), "S13"])
    close = float(PANEL.close.at[pd.Timestamp(SESSION), "S13"])
    new_stop = round((low + close) / 2, 2)
    edited = {**api.store.positions[pid], "current_stop": new_stop, "last_action_on": None,
              "trail_atr": BreakoutParams().trail_atr}  # fmt: skip
    assert position_action(edited, CTX).kind is ActionKind.STOP_HIT  # premise: today's low

    for bad in ({"current_stop": 0}, {"shares": 0}, {"shares": -1}, {"status": "closed"}):
        assert api.client.patch(f"/v1/positions/{pid}", json=bad).status_code == 422, bad

    out = api.client.patch(f"/v1/positions/{pid}", json={"current_stop": new_stop}).json()

    assert out["last_action"] == "hold" and out["last_action_text"] == f"hold, stop {new_stop:.2f}"
    assert out["current_stop"] == new_stop and out["last_action_on"] == SESSION.isoformat()
    above_entry = round(entry * 1.01, 2)
    response = api.client.patch(f"/v1/positions/{pid}", json={"current_stop": above_entry})
    assert response.status_code == 200 and response.json()["current_stop"] >= above_entry
    notes = api.client.patch(f"/v1/positions/{pid}", json={"notes": "bought at the open"})
    assert notes.json()["notes"] == "bought at the open"
    assert api.client.patch(f"/v1/positions/{uuid.uuid4()}", json={"notes": "x"}).status_code == 404


def test_closing_validates_the_date_and_refuses_a_second_close(api):
    pid = _add_position(api, entry=100.0, stop=95.0, opened=SESSION)
    early = {"closed_on": (SESSION - timedelta(days=1)).isoformat(), "exit_price": 101.0}
    assert api.client.post(f"/v1/positions/{pid}/close", json=early).status_code == 422
    assert (
        api.client.post(f"/v1/positions/{pid}/close", json={**early, "exit_price": 0}).status_code
        == 422
    )

    done = {"closed_on": NEXT.isoformat(), "exit_price": 93.0, "exit_reason": "stop hit"}
    out = api.client.post(f"/v1/positions/{pid}/close", json=done).json()
    assert out["r_multiple"] == pytest.approx(-1.4) and out["exit_reason"] == "stop hit"
    assert api.client.post(f"/v1/positions/{pid}/close", json=done).status_code == 409


def test_a_closed_position_counts_the_sessions_it_was_held(api):
    swing_context.set_context(CTX, UniverseFilter())
    opened = PANEL.dates()[440].date()
    pid = _add_position(api, entry=100.0, stop=95.0, opened=opened)

    out = api.client.post(
        f"/v1/positions/{pid}/close",
        json={"closed_on": PANEL.dates()[445].date().isoformat(), "exit_price": 101.0},
    ).json()

    assert out["held_sessions"] == 6 and out["r_now"] is None


# ---------------------------------------------------------------------------- track record


def test_the_track_record_reads_the_committed_reports_as_phase_1_writes_them(api):
    """PR11: the real ``ml/validation`` files, read-only. Both families failed out of sample."""
    api.repo = REPO
    report = json.loads((REPO / "ml/validation/breakout/breakout-v1.report.json").read_text())

    j = api.get("/v1/track-record").json()

    breakout, pullback = j["families"]
    assert (breakout["family"], pullback["family"]) == ("breakout", "pullback")
    assert breakout["label"] == "Breakout" and breakout["verdict"] == "research"
    assert breakout["verdict_text"] == f"Research only: {report['verdict']['failures'][0]}"
    assert breakout["versions"] == [
        {"version": "breakout-v1", "run_index": 1, "freeze_date": "2026-10-16", "passed": False}
    ]
    oos = report["oos_trades"]
    assert breakout["oos"]["n"] == oos["n"] and breakout["oos"]["n_distinct"] == oos["n_distinct"]
    assert breakout["oos"]["expectancy_r"] == pytest.approx(oos["expectancy_r"])
    assert breakout["oos_distinct"]["n"] == report["oos_trades_distinct"]["n"]
    assert breakout["insample"]["n"] == report["is_stats"]["n"]
    assert breakout["baseline"] == pytest.approx(report["baseline"])
    strategy = report["portfolio_alone"]["equity_weekly"]
    assert breakout["equity"]["strategy"][0] == {"time": strategy[0][0], "value": strategy[0][1]}
    assert len(breakout["equity"]["spy"]) == len(report["spy"]["equity_weekly"])
    first_spy = report["spy"]["equity_weekly"][0][1]
    assert breakout["equity"]["spy"][0]["value"] == pytest.approx(first_spy * 25_000.0)
    assert breakout["portfolio"]["strategy"]["max_dd"] == pytest.approx(
        report["portfolio_alone"]["max_dd"]
    )
    assert breakout["portfolio"]["combined"] is None
    assert breakout["portfolio"]["combined_note"] == report["portfolio_combined_note"]
    assert pullback["verdict"] == "research" and pullback["oos"]["n"] > 0


def test_the_track_record_lists_every_failed_condition_verbatim(api):
    """The badge names the first failure only; ``failures`` carries all of them, in order, so the
    Track record page can show why a setup is Research only. Live and untested have none."""
    api.repo = REPO

    breakout, pullback = api.get("/v1/track-record").json()["families"]

    for family in (breakout, pullback):
        name = family["family"]
        report = json.loads((REPO / f"ml/validation/{name}/{name}-v1.report.json").read_text())
        assert family["failures"] == report["verdict"]["failures"]
        assert family["verdict_text"] == f"Research only: {family['failures'][0]}"
    assert len(breakout["failures"]) == 3
    assert breakout["failures"][0].startswith("profit factor")
    assert "random entry" in breakout["failures"][1]
    assert breakout["failures"][2].startswith("drawdown")


def test_failures_are_empty_when_a_setup_is_live_or_untested(api):
    _live(api.repo, "breakout")

    breakout, pullback = api.get("/v1/track-record").json()["families"]

    assert breakout["verdict"] == "live" and breakout["failures"] == []
    assert pullback["verdict"] == "not_tested" and pullback["failures"] == []


def test_the_live_record_comes_from_the_stored_ideas_with_both_counts(api):
    ideas = [
        {"id": uuid.uuid4(), "symbol": f"Z{k}", "setup": "breakout", "session": SESSION,
         "track_status": "closed", "entry_date": SESSION, "known_date": NEXT,
         "r_multiple": r, "watch_only": False, "reconstructed": rec}
        for k, (r, rec) in enumerate([(1.0, False), (-1.0, False), (2.0, True)])
    ]  # fmt: skip
    api.store = FakeStore(ideas=ideas)

    breakout = api.get("/v1/track-record").json()["families"][0]

    expected = live_stats(ideas, reconstructed=False)
    assert breakout["live"]["n"] == expected["n"] == 2 and breakout["live"]["n_distinct"] == 2
    assert breakout["live"]["expectancy_r"] == pytest.approx(0.0)
    assert breakout["reconstructed"]["n"] == 1 and breakout["reconstructed"]["open"] == 0
    assert breakout["live"]["avg_hold"] is None  # NaN: no sessions_held stored
    assert breakout["verdict"] == "not_tested" and breakout["insample"] is None
    assert breakout["equity"] == {"strategy": [], "spy": []}
    assert breakout["baseline"] is None and breakout["versions"] == []


def test_track_record_warnings(api):
    _live(api.repo, "breakout")
    losing = [
        {"id": uuid.uuid4(), "symbol": f"Z{k:02d}", "setup": "breakout", "session": SESSION,
         "track_status": "closed", "entry_date": SESSION, "known_date": NEXT,
         "r_multiple": -0.5, "watch_only": False, "reconstructed": False}
        for k in range(30)
    ]  # fmt: skip
    api.store = FakeStore(ideas=losing, settings=SwingSettings(min_price=5.0))

    breakout, pullback = api.get("/v1/track-record").json()["families"]

    assert breakout["warnings"] == [
        "live underperforming: expectancy -0.50R after 30 live ideas",
        "validated with default universe filters; yours differ",
    ]
    assert pullback["warnings"] == []  # never validated: neither applies


def test_a_research_only_family_never_reads_as_live_underperforming(api):
    """The live-underperforming warning compared a family's live record with a validation it
    never passed: a Research-only family's forward-tracked ideas are not a Live record."""
    losing = [
        {"id": uuid.uuid4(), "symbol": f"Z{k:02d}", "setup": "pullback", "session": SESSION,
         "track_status": "closed", "entry_date": SESSION, "known_date": NEXT,
         "r_multiple": -0.5, "watch_only": False, "reconstructed": False}
        for k in range(30)
    ]  # fmt: skip
    api.store = FakeStore(ideas=losing)

    _, pullback = api.get("/v1/track-record").json()["families"]

    assert pullback["verdict"] != "live" and pullback["live"]["n"] == 30
    assert pullback["warnings"] == []


@pytest.mark.parametrize(
    ("setup", "text"),
    [
        pytest.param("failed", "Research only: profit factor 1.05 < 1.20", id="failed-gate"),
        pytest.param("unreadable", "Research only: report unreadable", id="unreadable"),
        pytest.param("rules", "Research only: rules changed since its run", id="rules-changed"),
    ],
)
def test_verdict_text_names_the_first_failure_verbatim(api, setup, text):
    folder = api.repo / "ml" / "validation" / "breakout"
    if setup == "failed":
        folder.mkdir(parents=True)
        doc = {"family": "breakout", "version": "breakout-v1", "run_index": 1,
               "freeze_date": "2026-09-30", "regime_filter": {"helps": True},
               "ranking": {"rs_helps": True},
               "verdict": {"passed": False, "failures": ["profit factor 1.05 < 1.20", "x"]}}  # fmt: skip
        (folder / "breakout-v1.report.json").write_text(json.dumps(doc))
    elif setup == "unreadable":
        folder.mkdir(parents=True)
        (folder / "breakout-v1.report.json").write_text("{not json")
    else:
        _live(api.repo, "breakout")
        prereg = json.loads((folder / "breakout-v1.prereg.json").read_text())
        prereg["protocol"]["universe"]["min_price"] = 5.0  # sealed under other rules
        sealed = Prereg.create(**{k: v for k, v in prereg.items() if k != "sha256"})
        (folder / "breakout-v1.prereg.json").unlink()
        write_draft(folder / "breakout-v1.prereg.json", sealed)
        report = json.loads((folder / "breakout-v1.report.json").read_text())
        report["prereg_sha256"] = sealed.sha256
        (folder / "breakout-v1.report.json").write_text(json.dumps(report))

    breakout = api.get("/v1/track-record").json()["families"][0]

    assert breakout["verdict"] == "research" and breakout["verdict_text"] == text


def test_the_track_record_uses_in_sample_stats_before_any_report(api):
    folder = api.repo / "ml" / "validation" / "breakout"
    folder.mkdir(parents=True)
    shutil.copy(REPO / "ml/validation/breakout/breakout-v1.insample.json", folder)
    chosen = json.loads((folder / "breakout-v1.insample.json").read_text())["chosen"]
    api.store = FakeStore(scan=_scan_row())

    breakout = api.get("/v1/track-record").json()["families"][0]
    idea = next(i for i in _today(api)["unproven"] if i["setup"] == "breakout")

    assert breakout["insample"]["n"] == chosen["n"] and breakout["oos"] is None
    assert breakout["versions"] == [
        {"version": "breakout-v1", "run_index": None, "freeze_date": None, "passed": None}
    ]
    assert idea["track_record"]["source"] == "insample"
    assert idea["track_record"]["trades"] == chosen["n"]
    assert idea["track_record"]["win_rate"] == pytest.approx(chosen["win_rate"])


def test_the_track_record_never_runs_a_backtest(api, monkeypatch):
    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a request recomputed something")

    monkeypatch.setattr(swing_context, "build_context", must_not_run)
    api.repo = REPO
    assert api.get("/v1/track-record").status_code == 200
    assert api.loads == []


# ---------------------------------------------------------------------------- settings


def test_settings_round_trip_and_bounds(api):
    assert api.get("/v1/settings").json() == SwingSettings().model_dump()

    saved = api.client.put("/v1/settings", json={"equity": 40_000.0, "telegram_digest": False})
    assert saved.status_code == 200
    assert saved.json() == SwingSettings(equity=40_000.0, telegram_digest=False).model_dump()
    assert api.store.settings.equity == 40_000.0  # fields left out keep their stored values

    for bad in (
        {"equity": 0},
        {"equity": -1},
        {"risk_pct": 0.06},
        {"max_positions": 0},
        {"max_total_risk_pct": 0.6},
        {"max_position_pct": 0},
        {"min_price": 0.5},
        {"min_dollar_volume": 10},
        {"equity": None},
        {"leverage": 2},
    ):
        response = api.client.put("/v1/settings", json=bad)
        assert response.status_code == 422, bad
        assert any(next(iter(bad)) in err["loc"] for err in response.json()["detail"]), bad
    assert api.store.settings.equity == 40_000.0


def test_telegram_test_sends_one_fixed_text_and_redacts_the_detail(api, monkeypatch):
    sent: list[str] = []

    async def fake_send(text: str) -> DeliveryResult:
        sent.append(text)
        return DeliveryResult(
            ok=False, detail=f"HTTP 401 https://api.telegram.org/bot{FAKE_TOKEN}/"
        )

    monkeypatch.setattr(swing_routes, "send_digest", fake_send)

    j = api.client.post("/v1/settings/telegram-test").json()

    assert len(sent) == 1 and sent[0] == swing_routes.TELEGRAM_TEST_TEXT
    assert "Informational only, not financial advice." in sent[0]
    assert j["ok"] is False and FAKE_TOKEN not in j["detail"] and "REDACTED" in j["detail"]


# ---------------------------------------------------------------------------- run scan now


class _ClosingClient:
    def __init__(self, closed: list[str]) -> None:
        self.closed = closed

    async def aclose(self) -> None:
        await asyncio.sleep(0.05)  # an HTTP client's close awaits its connections
        self.closed.append("closed")


def _fake_deps(log: list[str], store: Any = None) -> JobDeps:
    async def calendar() -> Any:
        raise AssertionError("not used")

    async def send(text: str) -> DeliveryResult:
        raise AssertionError("no Telegram in tests")

    return JobDeps(
        client=_ClosingClient(log),  # type: ignore[arg-type]
        calendar=calendar,
        bars_store=object(),  # type: ignore[arg-type]
        store=store or object(),  # type: ignore[arg-type]
        send=send,
        repo=REPO,
        now=lambda: NOW,
    )


class _JobStore:
    def __init__(self) -> None:
        self.job_id = uuid.uuid4()

    async def start_job(self, kind: str, session_date: date | None) -> uuid.UUID:
        return self.job_id


def test_run_scan_now_starts_a_manual_job_and_returns_its_id(api, monkeypatch):
    log: list[str] = []
    store = _JobStore()
    runs: list[tuple[str, Any]] = []

    async def fake_run(deps: JobDeps, kind: str) -> dict[str, Any]:
        runs.append((kind, deps.on_context))
        await deps.store.start_job(kind, SESSION)
        await asyncio.sleep(0)
        return {"status": "ok"}

    monkeypatch.setattr(
        swing_routes, "make_swing_deps",
        lambda: dataclasses.replace(_fake_deps(log, store), on_context=swing_context.set_context),
    )  # fmt: skip
    monkeypatch.setattr(swing_routes, "run_job", fake_run)

    with TestClient(api.app, headers=WEB) as client:
        response = client.post("/v1/scan/run")
        for _ in range(100):
            if log:
                break
            threading.Event().wait(0.01)

    assert response.status_code == 202
    assert response.json() == {"job_id": str(store.job_id), "starting": False}
    assert runs == [("manual", swing_context.set_context)]
    assert log == ["closed"]  # the Alpaca client is closed once the job is done
    assert not swing_routes._BACKGROUND


def test_run_scan_now_while_a_job_runs_is_409_without_building_anything(api, monkeypatch):
    monkeypatch.setattr(swing_routes.job, "is_running", lambda: True)

    def must_not_build() -> JobDeps:
        raise AssertionError("built deps while busy")

    monkeypatch.setattr(swing_routes, "make_swing_deps", must_not_build)
    response = api.client.post("/v1/scan/run")
    assert response.status_code == 409 and response.json() == {
        "detail": "a scan is already running"
    }


def test_run_scan_now_without_alpaca_keys_is_503(api, monkeypatch):
    def no_keys() -> JobDeps:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET are not configured")

    monkeypatch.setattr(swing_routes, "make_swing_deps", no_keys)
    response = api.client.post("/v1/scan/run")
    assert response.status_code == 503
    assert "ALPACA_API_KEY and ALPACA_API_SECRET are not configured" in response.json()["detail"]


# ---------------------------------------------------------------------------- status


def test_status_says_what_is_configured_and_never_returns_a_value(api, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKSECRETKEY123")
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-alpaca-value")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "424242")
    running = _job("running", {"bars": {"status": "running", "done": 7, "total": 70}})
    api.store = FakeStore(jobs=[running])
    when = datetime(2021, 9, 24, 20, 45, tzinfo=UTC)
    api.state = {"running": True, "next_scan_at": when, "disabled_reason": None}

    response = api.get("/v1/system/status")

    j = response.json()
    assert j["alpaca_configured"] is True and j["telegram_configured"] is False
    for secret in ("PKSECRETKEY123", "s3cr3t-alpaca-value", "424242"):
        assert secret not in response.text
    assert j["backfill"] == {"done": 7, "total": 70} and j["data_as_of"] is None
    assert j["scheduler_running"] is True and j["disabled_reason"] is None
    assert datetime.fromisoformat(j["next_scan_at"]) == when
    assert j["last_scan"]["status"] == "running" and j["last_scan"]["generated_at"] is None


def test_auto_execution_reflects_the_flag_the_api_started_with(api, monkeypatch):
    assert swing_routes._AUTO_EXECUTION is False  # the default: ATLAS_ENABLE_AUTO_EXECUTION unset
    monkeypatch.setattr(swing_routes, "_AUTO_EXECUTION", True)
    assert api.get("/v1/system/status").json()["auto_execution"] is True


def test_the_routes_are_mounted_on_the_api():
    paths = {getattr(r, "path", None) for r in service.app.routes}
    assert {
        "/v1/today",
        "/v1/stocks/{symbol}",
        "/v1/track-record",
        "/v1/positions",
        "/v1/positions/{position_id}",
        "/v1/positions/{position_id}/close",
        "/v1/settings",
        "/v1/settings/telegram-test",
        "/v1/scan/run",
        "/v1/system/status",
    } <= paths


# ---------------------------------------------------------------------------- fix round 1


def _failed_report(repo: Path, family: str = "breakout") -> None:
    folder = repo / "ml" / "validation" / family
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"family": family, "version": f"{family}-v1", "run_index": 1,
           "freeze_date": "2026-09-30", "regime_filter": {"helps": True},
           "ranking": {"rs_helps": True},
           "verdict": {"passed": False, "failures": ["profit factor 1.05 < 1.20"]}}  # fmt: skip
    (folder / f"{family}-v1.report.json").write_text(json.dumps(doc))


def test_an_idea_scanned_live_whose_family_is_no_longer_live_is_unproven(api):
    """I1: the scan stored breakout ideas as Live; the family's verdict today is Research only
    (its rules or files changed since). The current verdict decides where an idea is shown."""
    assert any(i.live and i.setup == "breakout" for i in RESULT.ideas)  # premise
    _failed_report(api.repo)
    api.store = FakeStore(scan=_scan_row())
    swing_context.set_context(CTX, UniverseFilter())

    j = _today(api)

    assert j["ideas"] == []
    breakouts = [i for i in j["unproven"] if i["setup"] == "breakout"]
    assert breakouts and not any(i["live"] for i in breakouts)
    for i in breakouts:
        assert i["track_record"]["verdict"] == "research"
        assert i["track_record"]["verdict_text"].startswith("Research only:")
    page = _stock(api, breakouts[0]["symbol"])
    assert page["idea"]["live"] is False
    assert (
        page["idea"]["track_record"]["verdict_text"] == "Research only: profit factor 1.05 < 1.20"
    )


def test_the_cache_never_goes_back_to_an_earlier_session(api):
    """I2 (a): a context for an earlier session never replaces a later one."""
    swing_context.set_context(CTX)
    swing_context.set_context(CTX_PREV, UniverseFilter())
    assert swing_context._CACHE.ctx is CTX


async def test_a_warm_up_that_finishes_after_the_job_hands_over_does_not_win(api, monkeypatch):
    """I2 (a), the race: the warm-up read the S-1 scan, the job handed over S while it built."""
    api.store = FakeStore(scan={**_scan_row(), "session": PREV})
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_loader(as_of, universe, extra_symbols=()):
        started.set()
        await release.wait()
        return PANEL, KINDS, as_of

    monkeypatch.setattr(swing_context, "loader", slow_loader)
    warm = asyncio.create_task(swing_context.warm_context())
    await started.wait()
    swing_context.set_context(CTX)  # the job, done with session S
    release.set()
    await warm

    assert swing_context._CACHE.ctx is CTX
    assert await swing_context.get_context() is CTX


def test_a_universe_change_during_a_job_never_leaves_an_older_session_cached(api, monkeypatch):
    """I2 (b), the probe: Settings change the universe while a job runs, a request builds from
    the S-1 scan, then the job's S context (old filters) is dropped. S-1 must not stay."""
    monkeypatch.setattr(swing_routes.job, "is_running", lambda: True)
    api.store = FakeStore(scan={**_scan_row(), "session": PREV})
    body = {**SwingSettings().model_dump(), "min_price": 15.0}
    with TestClient(api.app, headers=WEB) as client:  # one event loop, as the real API has
        assert client.put("/v1/settings", json=body).status_code == 200  # starts the rebuild
        assert client.get(BUILDS).status_code == 200  # waits for it: from the S-1 scan
        cached = swing_context._CACHE.ctx
        assert cached is not None and cached.session.date() == PREV

        swing_context.set_context(CTX)  # the job's S context, built with the old filters: dropped

        assert swing_context._CACHE.ctx is None  # S-1 cleared too
        api.store.scan = _scan_row()  # the job saved S before it handed its context over
        assert client.get(BUILDS).status_code == 200
    assert swing_context._CACHE.ctx.session.date() == SESSION
    assert [load[0] for load in api.loads] == [PREV, SESSION]


async def test_a_build_older_than_a_dropped_job_context_is_not_cached(api, monkeypatch):
    """I2 (b): the lazy build was still running when the job's newer context was dropped."""
    monkeypatch.setattr(swing_routes.job, "is_running", lambda: True)
    api.store = FakeStore(scan={**_scan_row(), "session": PREV})
    swing_context.invalidate_context(during_job=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_loader(as_of, universe, extra_symbols=()):
        started.set()
        await release.wait()
        return PANEL, KINDS, as_of

    monkeypatch.setattr(swing_context, "loader", slow_loader)
    build = asyncio.create_task(swing_context.get_context())
    await started.wait()
    swing_context.set_context(CTX)  # dropped: built with the old filters
    release.set()

    got = await build
    cached = swing_context._CACHE.ctx
    # S-1 is refused. The rebuild Settings started (invalidate_context) held the lock first and
    # was the one refused; this read then builds the newest session offered (S), or finds none.
    assert got is None or got.session.date() == SESSION
    assert cached is None or cached.session.date() == SESSION


def test_a_stale_cached_context_never_overwrites_newer_advice(api):
    """I2 (c): the job (run from the CLI) advised the position for S; the API's cache is still at
    S-1. Editing the stop must not write S-1 advice over it, which the next nightly would walk
    from with the stop checked (a false stop hit)."""
    swing_context.set_context(CTX_PREV, UniverseFilter())
    pid = _add_position(
        api, entry=100.0, stop=90.0, opened=PANEL.dates()[440].date(), last_action="hold",
        last_action_text="hold, stop 90.00", last_action_on=SESSION,
    )  # fmt: skip

    out = api.client.patch(f"/v1/positions/{pid}", json={"current_stop": 91.0}).json()

    assert out["last_action_on"] == SESSION.isoformat()
    assert out["last_action_text"] == "hold, stop 90.00" and out["current_stop"] == 91.0
    assert api.store.writes == [(pid, {"current_stop": 91.0})]


def test_run_scan_now_reports_a_job_that_could_not_record_its_run(api, monkeypatch):
    """M1: never 202 without a job id. A database down at ``start_job`` is a readable 503."""
    log: list[str] = []

    class DownStore:
        async def start_job(self, kind: str, session_date: date | None) -> uuid.UUID:
            raise ConnectionRefusedError("[Errno 61] Connect call failed ('127.0.0.1', 5432)")

    async def fake_run(deps: JobDeps, kind: str) -> dict[str, Any]:
        await deps.store.start_job(kind, SESSION)
        return {"status": "ok"}

    monkeypatch.setattr(swing_routes, "make_swing_deps", lambda: _fake_deps(log, DownStore()))
    monkeypatch.setattr(swing_routes, "run_job", fake_run)

    response = api.client.post("/v1/scan/run")

    assert response.status_code == 503, response.text
    assert "Connect call failed" in response.json()["detail"]


async def test_two_concurrent_creates_for_one_symbol_give_one_position(api):
    """M2: the one-open-position-per-symbol check and the insert are one step."""
    import httpx

    api.store.slow = True
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://atlas.test", headers=WEB
    ) as client:
        first, second = await asyncio.gather(
            client.post("/v1/positions", json=_position_body(opened_on=NEXT.isoformat())),
            client.post("/v1/positions", json=_position_body(opened_on=NEXT.isoformat())),
        )

    assert sorted([first.status_code, second.status_code]) == [201, 409]
    assert len(api.store.positions) == 1


def test_a_failed_context_build_says_why_on_the_stock_page(api, monkeypatch):
    """M3: with a scan stored, a failed build is not "no scan data"; the reason is redacted."""

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("connection to postgresql://atlas:hunter2@db:5432 refused")

    monkeypatch.setattr(swing_context, "loader", broken)
    api.store = FakeStore(scan=_scan_row())

    response = api.get("/v1/stocks/S13")

    detail = response.json()["detail"]
    assert response.status_code == 503 and detail.startswith("Couldn't load scan data: ")
    assert "hunter2" not in detail and "refused" in detail


def test_closing_a_position_bought_before_a_split_gives_r_on_one_scale(api):
    """M4: a 4:1 split between the purchase and the close, not yet rescaled by the nightly job.
    The user paid 4x today's adjusted prices and sold at today's price."""
    swing_context.set_context(CTX, UniverseFilter())
    opened, closed = PANEL.dates()[440], PANEL.dates()[448]
    adjusted = float(PANEL.close.at[opened, "S13"])
    entry, stop = 4 * adjusted, 4 * adjusted * 0.9  # the prices before the split
    exit_price = float(PANEL.close.at[closed, "S13"])  # after it
    pid = _add_position(api, entry=entry, stop=stop, opened=opened.date(), shares=10.0)

    out = api.client.post(
        f"/v1/positions/{pid}/close",
        json={"closed_on": closed.date().isoformat(), "exit_price": exit_price},
    ).json()

    assert out["r_multiple"] == pytest.approx((exit_price - adjusted) / (adjusted * 0.1))
    assert out["entry_price"] == pytest.approx(adjusted) and out["shares"] == pytest.approx(40.0)
    assert out["initial_stop"] == pytest.approx(adjusted * 0.9)
    assert out["exit_price"] == pytest.approx(exit_price)


def test_the_stock_pages_dollar_volume_item_is_the_checklists_rule_3():
    """M5: the stock page's single dollar-volume item is the checklist's rule 3 itself."""
    from swing_engine.checklist import dollar_volume_rule, eligibility_checklist

    universe = UniverseFilter()
    assert (
        dollar_volume_rule(CTX, "S13", universe) == eligibility_checklist(CTX, "S13", universe)[2]
    )


def test_open_risk_is_one_shared_rule():
    from swing_engine.positions import open_risk

    assert open_risk({"shares": 10.0, "entry_price": 100.0, "initial_stop": 90.0,
                      "current_stop": 95.0}) == 50.0  # fmt: skip
    assert open_risk({"shares": 4.0, "entry_price": 50.0, "initial_stop": 40.0,
                      "current_stop": 55.0}) == 0.0  # fmt: skip
    assert not hasattr(job, "_open_risk") and not hasattr(swing_routes, "_open_risk")


# ---------------------------------------------------------------------------- fix round 2


def _pre_split(panel: Panel, symbol: str, factor: float) -> Panel:
    """``panel`` as it read before a split restated ``symbol``'s history (prices x ``factor``)."""
    frames = {f: getattr(panel, f).copy() for f in ("open", "high", "low", "close", "volume")}
    for f in ("open", "high", "low", "close"):
        frames[f][symbol] = frames[f][symbol] * factor
    return Panel(**frames, raw_close=frames["close"].copy())


def test_closing_on_a_stale_context_never_reverts_a_split_rescale(api):
    """The nightly job ran outside the API (the CLI) and rescaled the position for a 4:1 split; the
    API's cache still holds the context from before the split. Closing must not rescale it back."""
    stale = build_context(_pre_split(PANEL, "S13", 4.0), KINDS, PREV)
    swing_context.set_context(stale, UniverseFilter())
    opened, closed = PANEL.dates()[440], PANEL.dates()[450]
    entry = float(PANEL.close.at[opened, "S13"])  # already on today's (post-split) scale
    exit_price = entry * 1.1
    pid = _add_position(
        api, entry=entry, stop=entry * 0.9, opened=opened.date(), shares=40.0,
        last_action="hold", last_action_text="hold", last_action_on=closed.date(),
    )  # fmt: skip

    out = api.client.post(
        f"/v1/positions/{pid}/close",
        json={"closed_on": closed.date().isoformat(), "exit_price": exit_price},
    ).json()

    assert out["r_multiple"] == pytest.approx(1.0)
    assert out["entry_price"] == pytest.approx(entry) and out["shares"] == pytest.approx(40.0)
    assert out["exit_price"] == pytest.approx(exit_price)


def test_the_cache_follows_a_newer_stored_scan_without_a_handover(api):
    """The job ran outside the API (the CLI): the store has session S, the cache S-1."""
    swing_context.set_context(CTX_PREV, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())

    assert api.get(BUILDS).status_code == 200

    assert [load[0] for load in api.loads] == [SESSION]
    assert swing_context._CACHE.ctx is not None
    assert swing_context._CACHE.ctx.session.date() == SESSION


def test_a_cache_as_new_as_the_stored_scan_is_not_rebuilt(api):
    swing_context.set_context(CTX, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())
    for _ in range(3):
        assert api.get("/v1/positions").status_code == 200
    assert api.loads == [] and swing_context._CACHE.ctx is CTX


def test_while_a_job_runs_here_the_cache_waits_for_its_handover(api, monkeypatch):
    """The in-process job hands its context over when it is done; rebuilding the same session
    beside it would double the memory and CPU for nothing."""
    monkeypatch.setattr(swing_context.job, "is_running", lambda: True)
    swing_context.set_context(CTX_PREV, UniverseFilter())
    api.store = FakeStore(scan=_scan_row())

    assert api.get("/v1/positions").status_code == 200

    assert api.loads == [] and swing_context._CACHE.ctx is CTX_PREV


def test_a_build_that_would_lose_to_a_newer_session_is_not_retried_on_every_request(api):
    """A dropped job context (universe changed mid-job) is newer than the stored scan: the build
    goes to that session, so it is cached and the next requests do not reload."""
    api.store = FakeStore(scan={**_scan_row(), "session": PREV})
    swing_context.invalidate_context(during_job=True)
    swing_context.set_context(CTX)  # dropped, but S is the newest session now

    for _ in range(3):
        assert api.get(BUILDS).status_code == 200

    assert [load[0] for load in api.loads] == [SESSION]
    assert swing_context._CACHE.ctx.session.date() == SESSION


def test_run_scan_now_answers_starting_when_the_job_is_slow_to_record_its_run(api, monkeypatch):
    """The dashboard's proxy gives up after about 30 s: a slow calendar is "starting", not an
    error, and the job goes on (the dashboard then polls the system status)."""
    log: list[str] = []
    store = _JobStore()
    release = threading.Event()

    async def slow_run(deps: JobDeps, kind: str) -> dict[str, Any]:
        while not release.is_set():
            await asyncio.sleep(0.01)  # the calendar fetch
        await deps.store.start_job(kind, SESSION)
        return {"status": "ok"}

    monkeypatch.setattr(swing_routes, "JOB_ID_WAIT_S", 0.05)
    monkeypatch.setattr(swing_routes, "make_swing_deps", lambda: _fake_deps(log, store))
    monkeypatch.setattr(swing_routes, "run_job", slow_run)

    with TestClient(api.app, headers=WEB) as client:
        response = client.post("/v1/scan/run")
        release.set()
        for _ in range(200):
            if log:
                break
            threading.Event().wait(0.01)

    assert response.status_code == 202
    assert response.json() == {"job_id": None, "starting": True}
    assert log == ["closed"]  # it ran to the end after the answer


def test_run_scan_now_waits_at_most_20_seconds():
    assert swing_routes.JOB_ID_WAIT_S <= 20.0


# ---------------------------------------------------------------------------- bounded context waits
#
# D3, the controller's live run: on a cold start the context build reads a year and a half of
# bars for the whole universe (about a minute), and every positions request waited for it, so the
# Positions page and "I bought this" hung. Each request now has its own bound and the build runs
# in the background. The loader below takes 3 seconds; every bound here is far shorter.

SLOW_S = 3.0


def _slow(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_loader(as_of, universe, extra_symbols=()):
        api.loads.append((as_of, universe, list(extra_symbols)))
        await asyncio.sleep(SLOW_S)
        return PANEL, KINDS, as_of or SESSION

    monkeypatch.setattr(swing_context, "loader", slow_loader)
    monkeypatch.setattr(swing_context, "WRITE_WAIT_S", 0.2)
    monkeypatch.setattr(swing_context, "STOCK_WAIT_S", 0.3)


def _timed(call: Any) -> tuple[Any, float]:
    start = time.monotonic()
    response = call()
    return response, time.monotonic() - start


def test_listing_positions_never_waits_for_a_cold_build(api, monkeypatch):
    _slow(api, monkeypatch)
    api.store = FakeStore(scan=_scan_row())
    _add_position(api, entry=100.0, stop=95.0, opened=SESSION)

    response, took = _timed(lambda: api.get("/v1/positions"))

    assert response.status_code == 200 and took < 1.0
    (row,) = response.json()
    assert row["last_close"] is None and row["r_now"] is None
    assert api.loads, "the miss started a background build"


def test_a_position_write_waits_at_most_its_bound_then_saves_without_advice(api, monkeypatch):
    _slow(api, monkeypatch)
    api.store = FakeStore(scan=_scan_row())

    created, took = _timed(lambda: api.client.post("/v1/positions", json=_position_body()))

    assert created.status_code == 201, created.text
    assert took < swing_context.WRITE_WAIT_S + 1.0 < SLOW_S
    out = created.json()
    assert out["last_action"] is None and out["current_stop"] == 95.0  # the nightly advises it

    patched, took = _timed(
        lambda: api.client.patch(f"/v1/positions/{out['id']}", json={"current_stop": 96.0})
    )
    assert patched.status_code == 200 and took < swing_context.WRITE_WAIT_S + 1.0
    assert patched.json()["current_stop"] == 96.0 and patched.json()["last_action"] is None

    body = {"closed_on": NEXT.isoformat(), "exit_price": 110.0}
    closed, took = _timed(
        lambda: api.client.post(f"/v1/positions/{out['id']}/close", json=body)
    )
    assert closed.status_code == 200 and took < swing_context.WRITE_WAIT_S + 1.0
    assert closed.json()["exit_price"] == 110.0  # as given: no split check without a context
    assert closed.json()["r_multiple"] == pytest.approx((110.0 - 100.0) / 5.0)


def test_a_stock_page_waits_at_most_its_bound_then_says_it_is_loading(api, monkeypatch):
    _slow(api, monkeypatch)
    api.store = FakeStore(scan=_scan_row())

    response, took = _timed(lambda: api.get("/v1/stocks/S05"))

    assert response.status_code == 503 and took < swing_context.STOCK_WAIT_S + 1.0 < SLOW_S
    assert response.json() == {
        "detail": "Loading market data; this takes about a minute after ATLAS starts."
    }


def test_a_stock_page_gets_the_context_when_it_is_ready_within_its_bound(api):
    api.store = FakeStore(scan=_scan_row())  # the default loader is quick
    response, took = _timed(lambda: api.get("/v1/stocks/S05"))
    assert response.status_code == 200 and took < swing_context.STOCK_WAIT_S


def test_a_universe_change_starts_the_rebuild_without_waiting_for_a_request(api):
    api.store = FakeStore(scan=_scan_row())
    swing_context.set_context(CTX, UniverseFilter())
    body = {**SwingSettings().model_dump(), "min_price": 15.0}

    with TestClient(api.app, headers=WEB) as client:
        assert client.put("/v1/settings", json=body).status_code == 200
        for _ in range(500):
            if swing_context._CACHE.ctx is not None:
                break
            threading.Event().wait(0.01)

    assert [load[1].min_price for load in api.loads] == [15.0]
    assert swing_context._CACHE.universe == api.store.settings.universe()


def test_the_lifespan_warms_the_cache_even_with_the_scheduler_off(monkeypatch):
    warmed: list[bool] = []

    async def warm() -> None:
        warmed.append(True)

    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.setattr(service, "load_env", lambda: None)
    monkeypatch.setattr(service, "warm_swing_context", warm)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ATLAS_DISABLE_SCHEDULER", "1")
    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
        for _ in range(100):
            if warmed:
                break
            threading.Event().wait(0.01)
    assert warmed == [True]
