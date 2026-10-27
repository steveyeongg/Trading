"""Swing engine: forward tracking of published ideas and the live / reconstructed rule.

``track_ideas`` re-simulates every published idea from its signal session through the context's
session, with the one fill and exit model (``simulate_trade``), so the live track record builds
itself and can never disagree with the backtest. Four layers, each on exact numbers:

* ``is_live``: an idea counts as live only if it was published before 09:30 New York time on its
  target session, DST included. A reconstructed idea (published after the fact by a catch-up) is
  kept out of the live record.
* ``track_ideas`` on a hand-built panel (10 bps slippage, ATR 2.0 on every cell, so every fill,
  exit, R and ``known_date`` is worked out by hand): a stop-out, a time stop, an open trade with
  its raised trail, a buy-stop that never triggered, a gap skip, a not-yet-traded idea, and the two
  DELISTED paths (a segment break in the bars, and the caller's inactive-symbol flag, whose
  ``known_date`` is the panel session ``MAX_GAP + 1`` rows later, capped at the last session).
* ``track_ideas`` on a synthetic universe against ``simulate_trade`` called directly and against
  ``simulate_candidates`` (the backtest), plus the properties that make nightly re-simulation safe:
  idempotent with no stored state, and a trade that closed by an earlier session reads the same
  from a later one.
* ``live_stats``: the live and reconstructed records never mix, watch-only ideas are not part of
  either, an open trade still blocks a later entry on its symbol (so ``n_distinct`` means what it
  means in the backtest), and no closed trade gives ``n=0`` and NaN rather than an exception.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from swing_engine import tracking
from swing_engine.backtest.trades import (
    context_candidates,
    known_date_for,
    simulate_candidates,
)
from swing_engine.config import BreakoutParams, ExecutionParams, PullbackParams
from swing_engine.execution import ExitReason, TrackStatus, simulate_trade
from swing_engine.indicators import MAX_GAP, Indicators
from swing_engine.panel import Panel
from swing_engine.scan import ScanContext, build_context
from swing_engine.testing import synthetic_kinds, synthetic_universe_panel
from swing_engine.tracking import is_live, live_stats, track_ideas

START = "2024-01-02"
N = 40
ATR = 2.0
SLIP = 0.001  # ExecutionParams() default: 10 bps
INDEX = pd.bdate_range(START, periods=N)

Bar = tuple[float, float, float, float]  # open, high, low, close

TRACK_KEYS = {
    "id",
    "track_status",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
    "exit_reason",
    "known_date",
    "r_multiple",
    "current_stop",
    "price_scale",
}
NO_TRADE = TRACK_KEYS - {"id", "track_status", "price_scale"}  # None until there is a fill


# ------------------------------------------------------------------------------- is_live


def test_is_live_cutoff_is_0930_et_of_target_session():
    assert is_live(datetime(2026, 10, 16, 21, 0, tzinfo=UTC), date(2026, 10, 19))
    assert not is_live(datetime(2026, 10, 19, 13, 31, tzinfo=UTC), date(2026, 10, 19))  # 09:31 EDT


def test_is_live_is_strict_at_the_open_and_follows_dst():
    # EDT (UTC-4): the open is 13:30 UTC. 2026-10-19.
    assert is_live(datetime(2026, 10, 19, 13, 29, 59, tzinfo=UTC), date(2026, 10, 19))
    assert not is_live(datetime(2026, 10, 19, 13, 30, tzinfo=UTC), date(2026, 10, 19))
    # EST (UTC-5) after the clocks go back on 2026-11-01: the open is 14:30 UTC. 2026-11-02.
    assert is_live(datetime(2026, 11, 2, 14, 29, 59, tzinfo=UTC), date(2026, 11, 2))
    assert not is_live(datetime(2026, 11, 2, 14, 30, tzinfo=UTC), date(2026, 11, 2))
    # The same 13:45 UTC is after the open in summer and before it in winter.
    assert not is_live(datetime(2026, 10, 19, 13, 45, tzinfo=UTC), date(2026, 10, 19))
    assert is_live(datetime(2026, 11, 2, 13, 45, tzinfo=UTC), date(2026, 11, 2))


def test_is_live_reads_any_timezone_and_a_midnight_after_is_late():
    et = ZoneInfo("America/New_York")
    assert is_live(datetime(2026, 10, 19, 9, 29, tzinfo=et), date(2026, 10, 19))
    assert not is_live(datetime(2026, 10, 19, 9, 30, tzinfo=et), date(2026, 10, 19))
    tokyo = timezone(timedelta(hours=9))
    assert is_live(datetime(2026, 10, 19, 22, 0, tzinfo=tokyo), date(2026, 10, 19))  # 09:00 EDT
    assert not is_live(datetime(2026, 10, 19, 23, 0, tzinfo=tokyo), date(2026, 10, 19))  # 10:00 EDT
    # Published the day after the target session: not live, however early in that day.
    assert not is_live(datetime(2026, 10, 20, 0, 5, tzinfo=et), date(2026, 10, 19))


def test_is_live_refuses_a_naive_time():
    with pytest.raises(ValueError, match="timezone"):
        is_live(datetime(2026, 10, 16, 21, 0), date(2026, 10, 19))


# ------------------------------------------------------------------ the hand-built context


def _panel(spec: dict[str, dict[int, Bar]]) -> Panel:
    """A panel of ``N`` business days; a symbol's unlisted rows are unpriced (NaN)."""
    frames = {
        f: pd.DataFrame(np.nan, index=INDEX, columns=list(spec))
        for f in ("open", "high", "low", "close", "volume")
    }
    for symbol, rows in spec.items():
        for k, (o, h, low, c) in rows.items():
            frames["open"].at[INDEX[k], symbol] = o
            frames["high"].at[INDEX[k], symbol] = h
            frames["low"].at[INDEX[k], symbol] = low
            frames["close"].at[INDEX[k], symbol] = c
            frames["volume"].at[INDEX[k], symbol] = 1_000_000.0
    return Panel(**frames)


def _context(panel: Panel, session: pd.Timestamp | None = None) -> ScanContext:
    """A context whose only meaningful parts are the panel and an ATR of 2.0 on every cell."""
    index, columns = panel.close.index, panel.close.columns
    blank = pd.DataFrame(np.nan, index=index, columns=columns)
    fields = {f.name: blank for f in dataclasses.fields(Indicators)}
    fields["atr14"] = pd.DataFrame(ATR, index=index, columns=columns)
    flags = pd.DataFrame(False, index=index, columns=columns)
    return ScanContext(
        session=pd.Timestamp(index[-1] if session is None else session),
        panel=panel,
        ind=Indicators(**fields),
        eligible=flags,
        rs_pct=blank,
        leaders=flags,
        regime=pd.Series("uptrend", index=index),
        kinds={},
    )


def _rows(first: int, last: int, bar: Bar) -> dict[int, Bar]:
    """The same bar on every row from ``first`` to ``last`` inclusive."""
    return dict.fromkeys(range(first, last + 1), bar)


def _idea(
    idea_id: str,
    symbol: str,
    signal_row: int,
    *,
    trigger: float = 100.0,
    stop: float = 96.0,
    atr14: float = ATR,
    setup: str = "breakout",
    version: str = "breakout-v1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": idea_id,
        "session": INDEX[signal_row].date(),
        "symbol": symbol,
        "setup": setup,
        "version": version,
        "trigger": trigger,
        "stop": stop,
        "atr14": atr14,
        "watch_only": False,
        "reconstructed": False,
        **extra,
    }


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in rows}


FLAT: Bar = (100.0, 101.0, 99.0, 100.0)
FILL: Bar = (100.0, 103.0, 99.0, 102.0)  # fills the 100 trigger at 100; close 102 -> trail 96
HELD: Bar = (102.0, 104.0, 100.0, 103.0)  # a quiet session well above the stop


def _book() -> tuple[ScanContext, list[dict[str, Any]]]:
    """One symbol per outcome, all with signal row 1, trigger 100, stop 96, ATR 2.0.

    * STP: filled row 2 at 100.1; row 3 opens 102 and trades down to 95, through the 96 stop.
    * MXH: filled row 2; a flat 100.5 close; the 30th held session is row 31 (a time stop).
    * OPN: signal row 33, filled row 34, rising closes 101..106, so the trail has risen to 100.
    * NTR: row 2 only reaches 99.5, under the 100 trigger.
    * GAP: row 2 opens at 103, more than 1 ATR (2.0) over the trigger.
    * PND: signal on the last row, so there is no later session yet.
    * DLS: priced rows 0..5, then 11 unpriced rows (a segment break), then a reused ticker.
    * TRM: priced rows 0..29, then 10 unpriced rows (not a break): open unless flagged inactive.
    """
    spec: dict[str, dict[int, Bar]] = {
        "SPY": _rows(0, N - 1, FLAT),
        "STP": {**_rows(0, 1, FLAT), 2: FILL, 3: (102.0, 103.0, 95.0, 97.0), **_rows(4, 39, FLAT)},
        "MXH": {**_rows(0, 1, FLAT), **_rows(2, 31, (100.0, 101.0, 99.5, 100.5))},
        "OPN": {
            **_rows(0, 33, FLAT),
            34: (100.0, 102.0, 99.5, 101.0),
            35: (101.0, 103.0, 100.5, 102.0),
            36: (102.0, 104.0, 101.5, 103.0),
            37: (103.0, 105.0, 102.5, 104.0),
            38: (104.0, 106.0, 103.5, 105.0),
            39: (105.0, 107.0, 104.5, 106.0),
        },
        "NTR": {**_rows(0, 1, FLAT), 2: (98.0, 99.5, 97.0, 99.0), **_rows(3, 39, FLAT)},
        "GAP": {**_rows(0, 1, FLAT), 2: (103.0, 104.0, 102.0, 103.0), **_rows(3, 39, FLAT)},
        "PND": _rows(0, 39, FLAT),
        "DLS": {
            **_rows(0, 1, FLAT),
            2: FILL,
            **_rows(3, 5, HELD),
            **_rows(20, 39, (50, 51, 49, 50)),
        },
        "TRM": {**_rows(0, 1, FLAT), 2: FILL, **_rows(3, 29, HELD)},
    }
    ideas = [
        _idea("stp", "STP", 1),
        _idea("mxh", "MXH", 1),
        _idea("opn", "OPN", 33),
        _idea("ntr", "NTR", 1),
        _idea("gap", "GAP", 1),
        _idea("pnd", "PND", N - 1),
        _idea("dls", "DLS", 1),
        _idea("trm", "TRM", 1),
    ]
    return _context(_panel(spec)), ideas


def _track(
    ctx: ScanContext,
    ideas: list[dict[str, Any]],
    repo: Path,
    delisted: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    return _by_id(track_ideas(ideas, ctx, delisted or set(), repo))


ENTRY = 100.0 * (1 + SLIP)  # 100.1: the trigger, plus slippage
RISK = ENTRY - 96.0


def test_a_stopped_out_idea_exits_at_the_stop_and_is_known_that_day(tmp_path):
    ctx, ideas = _book()
    row = _track(ctx, ideas, tmp_path)["stp"]
    assert set(row) == TRACK_KEYS
    assert row["track_status"] == "closed"
    assert row["entry_date"] == INDEX[2].date()
    assert row["entry_price"] == pytest.approx(ENTRY)
    assert row["exit_date"] == INDEX[3].date()
    assert row["exit_price"] == pytest.approx(96.0 * (1 - SLIP))
    assert row["exit_reason"] == "stop"
    assert row["r_multiple"] == pytest.approx((96.0 * (1 - SLIP) - ENTRY) / RISK)
    assert row["current_stop"] == pytest.approx(96.0)
    assert row["known_date"] == INDEX[3].date()


def test_the_time_stop_comes_from_the_setups_parameters(tmp_path):
    ctx, ideas = _book()
    row = _track(ctx, ideas, tmp_path)["mxh"]  # breakout-v1 holds 30 sessions
    assert row["track_status"] == "closed"
    assert row["exit_reason"] == "max_hold"
    assert row["exit_date"] == INDEX[31].date()
    assert row["known_date"] == INDEX[31].date()
    assert row["exit_price"] == pytest.approx(100.5 * (1 - SLIP))
    assert row["r_multiple"] == pytest.approx((100.5 * (1 - SLIP) - ENTRY) / RISK)


def test_params_are_looked_up_by_setup_and_version(tmp_path, monkeypatch):
    seen: list[tuple[str, str | None, Path]] = []

    def fake_params_for(family: str, version: str | None, repo: Path) -> BreakoutParams:
        seen.append((family, version, repo))
        return BreakoutParams(max_hold=3)

    monkeypatch.setattr(tracking, "params_for", fake_params_for)
    ctx, ideas = _book()
    row = _track(ctx, [i for i in ideas if i["id"] == "mxh"], tmp_path)["mxh"]
    assert seen == [("breakout", "breakout-v1", tmp_path)]
    assert row["exit_reason"] == "max_hold"
    assert row["exit_date"] == INDEX[4].date()  # held rows 2, 3 and 4


def test_an_open_idea_reports_the_stop_in_force(tmp_path):
    ctx, ideas = _book()
    row = _track(ctx, ideas, tmp_path)["opn"]
    assert row["track_status"] == "open"
    assert row["entry_date"] == INDEX[34].date()
    assert row["entry_price"] == pytest.approx(ENTRY)
    assert row["current_stop"] == pytest.approx(106.0 - 3.0 * ATR)  # highest close - 3 ATR
    for key in ("exit_date", "exit_price", "exit_reason", "r_multiple", "known_date"):
        assert row[key] is None


def test_ideas_that_never_filled_carry_no_trade(tmp_path):
    ctx, ideas = _book()
    rows = _track(ctx, ideas, tmp_path)
    for idea_id, status in (("ntr", "not_triggered"), ("gap", "skipped_gap"), ("pnd", "pending")):
        row = rows[idea_id]
        assert row["track_status"] == status
        assert row["price_scale"] == 1.0
        assert {k: row[k] for k in NO_TRADE} == dict.fromkeys(NO_TRADE)


def test_a_segment_break_closes_the_idea_delisted_and_is_known_11_sessions_later(tmp_path):
    ctx, ideas = _book()
    rows = _track(ctx, ideas, tmp_path)  # DLS is not flagged: the 11 unpriced rows are the break
    row = rows["dls"]
    assert row["track_status"] == "closed"
    assert row["exit_reason"] == "delisted"
    assert row["exit_date"] == INDEX[5].date()  # the last priced bar
    assert row["exit_price"] == pytest.approx(103.0 * (1 - SLIP))
    assert row["known_date"] == INDEX[5 + MAX_GAP + 1].date()  # 11 panel rows after the exit
    assert row["known_date"] == INDEX[16].date()
    assert row["r_multiple"] == pytest.approx((103.0 * (1 - SLIP) - ENTRY) / RISK)


def test_an_inactive_symbol_flag_closes_an_open_idea_known_at_most_on_the_last_session(tmp_path):
    ctx, ideas = _book()
    still_trading = _track(ctx, ideas, tmp_path, delisted=set())["trm"]
    assert still_trading["track_status"] == "open"
    assert still_trading["known_date"] is None

    row = _track(ctx, ideas, tmp_path, delisted={"TRM"})["trm"]
    assert row["track_status"] == "closed"
    assert row["exit_reason"] == "delisted"
    assert row["exit_date"] == INDEX[29].date()
    assert row["exit_price"] == pytest.approx(103.0 * (1 - SLIP))
    assert row["known_date"] == INDEX[N - 1].date()  # 29 + 11 = 40 is past the end: capped
    # Only the flagged symbol changed.
    others = {k: v for k, v in _track(ctx, ideas, tmp_path, delisted={"TRM"}).items() if k != "trm"}
    assert others == {k: v for k, v in _track(ctx, ideas, tmp_path).items() if k != "trm"}


def test_known_date_for_matches_the_backtests_rule():
    index = pd.DatetimeIndex(INDEX)
    assert known_date_for(index, None, None) is None
    assert known_date_for(index, INDEX[7].date(), ExitReason.STOP) == INDEX[7].date()
    assert known_date_for(index, INDEX[7].date(), ExitReason.TRAIL) == INDEX[7].date()
    assert known_date_for(index, INDEX[7].date(), ExitReason.MAX_HOLD) == INDEX[7].date()
    assert known_date_for(index, INDEX[7].date(), ExitReason.DELISTED) == INDEX[18].date()
    assert known_date_for(index, INDEX[35].date(), ExitReason.DELISTED) == INDEX[N - 1].date()


def test_an_idea_with_nothing_to_track_gets_no_row_and_never_raises(tmp_path):
    """No row, not a row of Nones: a row would overwrite what tracking stored for the idea on an
    earlier night (an open trade's entry) whenever tonight's panel lacks it."""
    ctx, ideas = _book()
    stp = ideas[0]
    bad = [
        {**stp, "id": "no-symbol", "symbol": "ZZZ"},  # not in the panel at all
        {**stp, "id": "no-row", "session": date(2024, 1, 6)},  # a Saturday: not a panel session
        {**stp, "id": "future", "session": date(2030, 1, 2)},  # after the context's session
        {**stp, "id": "unpriced", "symbol": "DLS", "session": INDEX[8].date()},  # DLS has no bar
        {**stp, "id": "bad-stop", "stop": 101.0},  # above the entry fill: simulate_trade refuses
        {**stp, "id": "bad-setup", "setup": "swing-ish"},  # not a known family
    ]
    rows = _track(ctx, [*bad, stp], tmp_path)
    assert set(rows) == {"stp"}  # none of the bad ones has a row
    assert rows["stp"]["track_status"] == "closed"  # the good idea beside them is unaffected
    assert set(rows["stp"]) == TRACK_KEYS


def test_each_symbol_frame_is_built_once_however_many_ideas_it_has(tmp_path, monkeypatch):
    built: list[int] = []
    real = tracking.PanelBars.frame

    def counting(self: tracking.PanelBars, col: int) -> pd.DataFrame:
        built.append(col)
        return real(self, col)

    monkeypatch.setattr(tracking.PanelBars, "frame", counting)
    ctx, ideas = _book()
    stp = ideas[0]
    several = [
        stp,
        {**stp, "id": "stp2", "trigger": 100.5},
        {**stp, "id": "stp3", "session": INDEX[2].date()},
    ]
    rows = _track(ctx, several, tmp_path)
    assert len(built) == 1
    assert set(rows) == {"stp", "stp2", "stp3"}


def test_watch_only_ideas_are_tracked_like_any_other(tmp_path):
    ctx, ideas = _book()
    watch = {**ideas[0], "id": "watch", "watch_only": True}
    rows = _track(ctx, [ideas[0], watch], tmp_path)
    assert rows["watch"] == {**rows["stp"], "id": "watch"}
    assert rows["watch"]["track_status"] == "closed"


def test_tracking_does_not_change_its_inputs_and_is_idempotent(tmp_path):
    ctx, ideas = _book()
    before = copy.deepcopy(ideas)
    first = track_ideas(ideas, ctx, {"TRM"}, tmp_path)
    second = track_ideas(ideas, ctx, {"TRM"}, tmp_path)
    assert ideas == before
    assert first == second
    assert [row["id"] for row in first] == [idea["id"] for idea in ideas]  # input order kept
    # No hidden state: the order the ideas arrive in does not matter either.
    reversed_rows = track_ideas(ideas[::-1], ctx, {"TRM"}, tmp_path)
    assert _by_id(reversed_rows) == _by_id(first)


# ----------------------------------------------------- splits and the published price scale


def _rescaled(ctx: ScanContext, factor: float) -> ScanContext:
    """The context after the whole price history was re-adjusted by ``factor`` (bars and ATR).

    A 4:1 split after the signal date makes the data provider restate every earlier price at a
    quarter, so the adjusted panel the nightly job reads is the old one times 0.25.
    """
    p = ctx.panel
    panel = dataclasses.replace(
        p, open=p.open * factor, high=p.high * factor, low=p.low * factor, close=p.close * factor
    )
    ind = dataclasses.replace(ctx.ind, atr14=ctx.ind.atr14 * factor)
    return dataclasses.replace(ctx, panel=panel, ind=ind)


def test_a_split_after_the_signal_date_does_not_hide_a_real_fill(tmp_path):
    """The incident: ideas are published in the price scale of their day, and a 4:1 split later
    restates all history at 0.25. Re-simulating the stored trigger of 100 against bars now
    trading near 25 reported a trade that really filled as ``not_triggered`` (or left it
    open at a stop that could never be hit). The published levels are rescaled by the ratio of
    the ATR now to the ATR stored with the idea."""
    ctx, ideas = _book()
    plain = _track(ctx, ideas, tmp_path)
    split_ctx = _rescaled(ctx, 0.25)
    split = _track(split_ctx, ideas, tmp_path)  # the ideas still carry their old-scale levels

    # Without the rescale the stop-out would have been missed: the model sees a 100 trigger over
    # bars that never get above 26.
    stp = next(i for i in ideas if i["id"] == "stp")
    assert _direct(split_ctx, stp, BreakoutParams(), set()).status is TrackStatus.NOT_TRIGGERED

    assert split.keys() == plain.keys()
    for idea_id, want in plain.items():
        got = split[idea_id]
        assert got["price_scale"] == pytest.approx(0.25), idea_id
        for key in ("track_status", "entry_date", "exit_date", "exit_reason", "known_date"):
            assert got[key] == want[key], (idea_id, key)
        for key in ("entry_price", "exit_price", "current_stop"):  # on the current scale
            if want[key] is None:
                assert got[key] is None, (idea_id, key)
            else:
                assert got[key] == pytest.approx(0.25 * want[key]), (idea_id, key)
        if want["r_multiple"] is None:
            assert got["r_multiple"] is None
        else:
            assert got["r_multiple"] == pytest.approx(want["r_multiple"]), idea_id  # scale-free

    stp_row = split["stp"]
    assert stp_row["track_status"] == "closed"
    assert stp_row["entry_price"] == pytest.approx(ENTRY / 4)
    assert stp_row["exit_price"] == pytest.approx(96.0 * (1 - SLIP) / 4)
    assert stp_row["r_multiple"] == pytest.approx((96.0 * (1 - SLIP) - ENTRY) / RISK)
    assert split["opn"]["current_stop"] == pytest.approx(100.0 / 4)


def test_a_reverse_split_rescales_upward(tmp_path):
    ctx, ideas = _book()
    plain = _track(ctx, ideas, tmp_path)
    split = _track(_rescaled(ctx, 8.0), ideas, tmp_path)  # a 1:8 reverse split
    for idea_id, want in plain.items():
        got = split[idea_id]
        assert got["price_scale"] == pytest.approx(8.0), idea_id
        assert got["track_status"] == want["track_status"], idea_id
        assert got["r_multiple"] == pytest.approx(want["r_multiple"]), idea_id
    assert split["stp"]["entry_price"] == pytest.approx(8.0 * ENTRY)


def test_a_dividend_sized_adjustment_leaves_the_levels_alone(tmp_path):
    ctx, ideas = _book()
    plain = _track(ctx, ideas, tmp_path)
    shifted = _track(_rescaled(ctx, 0.997), ideas, tmp_path)  # history restated 0.3% lower
    for idea_id, want in plain.items():
        assert shifted[idea_id]["price_scale"] == 1.0, idea_id  # not 0.997: nothing was applied
        assert shifted[idea_id]["track_status"] == want["track_status"], idea_id
    stp = shifted["stp"]  # the published 96 stop is used as published
    assert stp["entry_price"] == pytest.approx(ENTRY)
    assert stp["exit_price"] == pytest.approx(96.0 * (1 - SLIP))
    assert stp["r_multiple"] == pytest.approx(plain["stp"]["r_multiple"])


@pytest.mark.parametrize(
    ("factor", "applied"), [(0.9951, 1.0), (1.0049, 1.0), (0.9949, 0.9949), (1.0051, 1.0051)]
)
def test_the_rescale_threshold_is_half_a_percent(tmp_path, factor, applied):
    ctx, ideas = _book()
    row = _track(_rescaled(ctx, factor), ideas, tmp_path)["stp"]
    assert row["price_scale"] == pytest.approx(applied)
    if applied == 1.0:
        assert row["price_scale"] == 1.0  # exactly 1.0, not the factor that was ignored


def test_an_idea_without_a_usable_scale_gets_no_row(tmp_path):
    ctx, ideas = _book()
    stp = ideas[0]
    unusable = [
        {**stp, "id": "zero-atr", "atr14": 0.0},
        {**stp, "id": "none-atr", "atr14": None},
        {**stp, "id": "nan-atr", "atr14": math.nan},
        {**stp, "id": "inf-atr", "atr14": math.inf},
        {**stp, "id": "negative-atr", "atr14": -2.0},
    ]
    no_key = {k: v for k, v in stp.items() if k != "atr14"}
    no_key["id"] = "no-atr-key"
    atr = ctx.ind.atr14.copy()
    atr.at[INDEX[1], "NTR"] = math.nan  # the ATR now is missing on NTR's signal date
    missing_now = dataclasses.replace(ctx, ind=dataclasses.replace(ctx.ind, atr14=atr))

    rows = _track(missing_now, [*unusable, no_key, stp, ideas[3]], tmp_path)
    for idea_id in [*(i["id"] for i in unusable), "no-atr-key", "ntr"]:
        assert idea_id not in rows, idea_id
    # The same idea with a usable ATR on both sides is still tracked.
    assert rows["stp"]["track_status"] == "closed"


def test_a_rejected_idea_gets_no_row(tmp_path):
    ctx, ideas = _book()
    # The levels are rescaled, but the stop is still above the (rescaled) entry fill: refused.
    bad = {**ideas[0], "id": "bad", "stop": 101.0}
    assert _track(_rescaled(ctx, 0.25), [bad], tmp_path) == {}


# -------------------------------------------------------------------- a synthetic universe


@pytest.fixture(scope="module")
def universe() -> tuple[ScanContext, list[dict[str, Any]], pd.DataFrame]:
    """A context on session 520 of a synthetic universe, with the ideas both setups would have
    published on each of the 100 sessions before it, and the backtest's candidate table."""
    panel = synthetic_universe_panel(7)
    ctx = build_context(panel, synthetic_kinds(panel), panel.dates()[520].date())
    start = panel.dates()[420]
    tables = [
        (params, context_candidates(ctx, params, start))
        for params in (BreakoutParams(), PullbackParams())
    ]
    ideas: list[dict[str, Any]] = []
    for _, table in tables:
        for row in table.itertuples(index=False):
            ideas.append(
                {
                    "id": str(uuid.uuid4()),
                    "session": row.date.date(),
                    "symbol": row.symbol,
                    "setup": row.setup,
                    "version": row.version,
                    "trigger": float(row.trigger),
                    "stop": float(row.stop),
                    "atr14": float(row.atr14),
                    "watch_only": False,
                    "reconstructed": False,
                }
            )
    return ctx, ideas, pd.concat([t for _, t in tables], ignore_index=True)


def _direct(ctx: ScanContext, idea: dict[str, Any], params: Any, delisted: set[str]) -> Any:
    """``simulate_trade`` on a frame built here, apart from tracking's own."""
    sym = idea["symbol"]
    frame = pd.DataFrame(
        {
            "open": ctx.panel.open[sym].to_numpy(),
            "high": ctx.panel.high[sym].to_numpy(),
            "low": ctx.panel.low[sym].to_numpy(),
            "close": ctx.panel.close[sym].to_numpy(),
            "atr14": ctx.ind.atr14[sym].to_numpy(),
        },
        index=ctx.panel.close.index,
    )
    return simulate_trade(
        frame,
        idea["session"],
        idea["trigger"],
        idea["stop"],
        params.trail_atr,
        params.max_hold,
        ExecutionParams(),
        delisted=sym in delisted,
    )


def _still_open(universe, tmp_path) -> set[str]:
    """Two symbols with an idea still open at the end: flagged inactive, they close DELISTED."""
    ctx, ideas, _ = universe
    rows = track_ideas(ideas, ctx, set(), tmp_path)
    held = {i["symbol"] for i, r in zip(ideas, rows, strict=True) if r["track_status"] == "open"}
    assert len(held) >= 2
    return set(sorted(held)[:2])


def test_track_ideas_equals_simulate_trade_run_directly(universe, tmp_path):
    ctx, ideas, _ = universe
    delisted = _still_open(universe, tmp_path)
    rows = track_ideas(ideas, ctx, delisted, tmp_path)
    assert len(rows) == len(ideas) > 200
    statuses: set[str] = set()
    for idea, row in zip(ideas, rows, strict=True):
        params = BreakoutParams() if idea["setup"] == "breakout" else PullbackParams()
        want = _direct(ctx, idea, params, delisted)
        assert row["id"] == idea["id"]
        assert row["track_status"] == want.status.value
        assert row["entry_date"] == want.entry_date
        assert row["entry_price"] == want.entry_price
        assert row["exit_date"] == want.exit_date
        assert row["exit_price"] == want.exit_price
        assert row["exit_reason"] == (want.exit_reason.value if want.exit_reason else None)
        assert row["r_multiple"] == want.r_multiple
        assert row["current_stop"] == want.current_stop
        statuses.add(row["track_status"])
    # The comparison is only meaningful if the ideas land in every state.
    assert statuses == {"pending", "not_triggered", "open", "closed"}


def test_track_ideas_agrees_with_the_backtest_row_for_row(universe, tmp_path):
    ctx, ideas, candidates = universe
    delisted = _still_open(universe, tmp_path)
    rows = track_ideas(ideas, ctx, delisted, tmp_path)
    expected = pd.concat(
        [
            simulate_candidates(
                ctx.panel,
                ctx.ind,
                candidates[candidates["setup"] == name],
                params,
                ExecutionParams(),
                delisted,
            )
            for name, params in (("breakout", BreakoutParams()), ("pullback", PullbackParams()))
        ],
        ignore_index=True,
    )
    assert len(expected) == len(rows)
    for row, want in zip(rows, expected.itertuples(index=False), strict=True):
        assert row["track_status"] == want.status
        assert _same_date(row["entry_date"], want.entry_date)
        assert _same_date(row["exit_date"], want.exit_date)
        assert _same_date(row["known_date"], want.known_date)
        assert _same_float(row["r_multiple"], want.r_multiple)
        assert _same_float(row["current_stop"], want.current_stop)
    assert sum(row["exit_reason"] == "delisted" for row in rows) > 0  # the flags did something


def _same_date(got: date | None, want: pd.Timestamp) -> bool:
    return got is None if pd.isna(want) else got == want.date()


def _same_float(got: float | None, want: float) -> bool:
    return got is None if math.isnan(want) else got == pytest.approx(want)


def test_a_trade_closed_by_an_earlier_session_reads_the_same_from_a_later_one(universe, tmp_path):
    ctx, ideas, _ = universe
    dates = ctx.panel.dates()
    # build_context truncates the panel at session 480: nothing after it can reach this context.
    earlier = build_context(ctx.panel, ctx.kinds, dates[480].date())
    early = _by_id(
        track_ideas(
            [i for i in ideas if i["session"] < dates[470].date()], earlier, set(), tmp_path
        )
    )
    late = _by_id(track_ideas(ideas, ctx, set(), tmp_path))
    closed = [r for r in early.values() if r["track_status"] == "closed"]
    assert len(closed) > 10
    for row in closed:
        assert late[row["id"]] == row
    # Every fill that had happened by then is still the same fill later on.
    for row in early.values():
        if row["entry_date"] is not None:
            assert late[row["id"]]["entry_date"] == row["entry_date"]
            assert late[row["id"]]["entry_price"] == row["entry_price"]


# --------------------------------------------------------------------------- live_stats


def _trade(
    symbol: str,
    r: float,
    entry: str = "2026-10-05",
    known: str | None = "2026-10-09",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "track_status": "closed",
        "entry_date": date.fromisoformat(entry),
        "known_date": date.fromisoformat(known) if known else None,
        "r_multiple": r,
        "watch_only": False,
        "reconstructed": False,
        **extra,
    }


def _open(symbol: str, entry: str = "2026-10-05", **extra: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "track_status": "open",
        "entry_date": date.fromisoformat(entry),
        "known_date": None,
        "r_multiple": None,
        "watch_only": False,
        "reconstructed": False,
        **extra,
    }


def _record() -> list[dict[str, Any]]:
    return [
        _trade("AAA", 2.0),
        _trade("BBB", -1.0),
        _trade("CCC", 1.0),
        _open("DDD"),
        {**_open("EEE"), "track_status": "pending", "entry_date": None},
        {**_open("FFF"), "track_status": "not_triggered", "entry_date": None},
        _trade("RRA", 3.0, reconstructed=True),
        _trade("RRB", -1.0, reconstructed=True),
        _open("RRC", reconstructed=True),
    ]


def test_reconstructed_rows_kept_out_of_live_stats():
    rows = _record()
    live = live_stats(rows, reconstructed=False)
    assert live["n"] == 3
    assert live["n_distinct"] == 3
    assert live["win_rate"] == pytest.approx(2 / 3)
    assert live["avg_win_r"] == pytest.approx(1.5)
    assert live["avg_loss_r"] == pytest.approx(-1.0)
    assert live["expectancy_r"] == pytest.approx(2 / 3)
    assert live["profit_factor"] == pytest.approx(3.0)
    assert live["open"] == 1

    rebuilt = live_stats(rows, reconstructed=True)
    assert rebuilt["n"] == 2
    assert rebuilt["win_rate"] == pytest.approx(0.5)
    assert rebuilt["expectancy_r"] == pytest.approx(1.0)
    assert rebuilt["profit_factor"] == pytest.approx(3.0)
    assert rebuilt["open"] == 1

    # Dropping the reconstructed rows changes nothing about the live record.
    assert live_stats([r for r in rows if not r["reconstructed"]], False) == pytest.approx(
        live, nan_ok=True
    )


def test_watch_only_rows_are_excluded_from_live_stats():
    rows = _record()
    with_watch = [
        *rows,
        _trade("WWA", 10.0, watch_only=True),
        _trade("WWB", 9.0, watch_only=True, reconstructed=True),
        _open("WWC", watch_only=True),
        _open("WWD", watch_only=True, reconstructed=True),
    ]
    for flag in (False, True):
        assert live_stats(with_watch, flag) == pytest.approx(live_stats(rows, flag), nan_ok=True)


def test_an_open_trade_still_blocks_a_later_entry_on_its_symbol():
    # AAA is entered on the 5th and still open; its second idea is entered on the 7th and closes.
    rows = [_open("AAA", "2026-10-05"), _trade("AAA", 2.0, "2026-10-07", "2026-10-09")]
    stats = live_stats(rows, False)
    assert stats["n"] == 1
    assert stats["n_distinct"] == 0
    assert stats["open"] == 1
    # Once the first has exited (known on the 6th), the second is its own trade.
    rows = [_trade("AAA", -1.0, "2026-10-05", "2026-10-06"), rows[1]]
    stats = live_stats(rows, False)
    assert (stats["n"], stats["n_distinct"], stats["open"]) == (2, 2, 0)


def test_live_stats_with_no_closed_trade_is_empty_not_an_error():
    for rows in ([], [_open("AAA")]):
        stats = live_stats(rows, False)
        assert stats["n"] == 0
        assert stats["n_distinct"] == 0
        assert stats["open"] == len([r for r in rows if r["track_status"] == "open"])
        for key in ("win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor"):
            assert math.isnan(stats[key]), key
    # Only the other record has trades.
    only_rebuilt = [_trade("AAA", 1.0, reconstructed=True)]
    assert live_stats(only_rebuilt, False)["n"] == 0
    assert live_stats(only_rebuilt, True)["n"] == 1


def test_live_stats_does_not_change_its_rows():
    rows = _record()
    before = copy.deepcopy(rows)
    live_stats(rows, False)
    assert rows == before


def test_the_status_values_are_the_track_status_enum_values(tmp_path):
    ctx, ideas = _book()
    rows = track_ideas(ideas, ctx, set(), tmp_path)
    allowed = {s.value for s in TrackStatus}
    assert {row["track_status"] for row in rows} <= allowed
    assert all(type(row["track_status"]) is str for row in rows)
    assert all(row["exit_reason"] is None or type(row["exit_reason"]) is str for row in rows)
    assert all(row["entry_date"] is None or type(row["entry_date"]) is date for row in rows)
    assert all(row["known_date"] is None or type(row["known_date"]) is date for row in rows)
    assert all(row["entry_price"] is None or type(row["entry_price"]) is float for row in rows)
    assert all(type(row["price_scale"]) is float and row["price_scale"] == 1.0 for row in rows)
