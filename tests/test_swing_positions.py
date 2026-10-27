"""Swing engine: the nightly action for a position the user actually took.

``position_action`` turns one row of ``swing_positions`` plus the scan context into a single
instruction: hold, raise the stop, the stop was hit, the time limit is up, or there is no price.
The rules run in a fixed order and every test pins the exact ``kind``, ``stop`` and ``text`` for one
of them:

1. no bar for the symbol on the scan day -> ``NO_DATA`` (a delisted, halted or typo'd symbol must
   not crash the scan, and must not look like a quiet hold with a stale stop);
2. opened on the scan day -> ``HOLD`` (the day's low may predate the fill);
3. the day's low reached the stop -> ``STOP_HIT``;
4. ``max_hold`` sessions with a bar since the open -> ``MAX_HOLD``;
5. the chandelier trail (highest close since the open minus ``trail_atr`` x ATR14 on the scan day,
   never lowered) at least a cent above the stop -> ``RAISE_STOP``;
6. otherwise ``HOLD``.

The contexts are hand-built: a few symbols over consecutive business days, an ATR of 2.0 on every
cell (so every trail is worked out by hand), and a NaN row wherever a symbol has no bar. One test
runs the same trade through ``simulate_trade``, the engine's one exit model, night by night, to pin
that a position's trail and the backtest's trail are the same line.
"""

from __future__ import annotations

import dataclasses
import json
import math
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from swing_engine.config import BreakoutParams, ExecutionParams
from swing_engine.execution import ExitReason, TrackStatus, simulate_trade
from swing_engine.indicators import Indicators, compute_indicators
from swing_engine.panel import Panel
from swing_engine.positions import (
    Action,
    ActionKind,
    exit_params,
    nightly_update,
    position_action,
    raise_stop_text,
    rescaled,
    resolve_exit_rules,
    split_ratio,
    walk_action,
    with_exit_rules,
)
from swing_engine.scan import ScanContext
from swing_engine.testing import make_bars, trend_series

REPO = Path(__file__).resolve().parents[1]
START = "2024-01-02"
ATR = 2.0

Bar = tuple[float, float, float, float]  # open, high, low, close


def _bar(close: float, low: float | None = None) -> Bar:
    """A bar that closes at ``close`` with a low one dollar under it unless told otherwise."""
    return (close, close + 1.0, close - 1.0 if low is None else low, close)


def _rows(closes: list[float]) -> list[Bar | None]:
    return [_bar(c) for c in closes]


def _context(
    spec: dict[str, list[Bar | None]],
    atr: float | None = ATR,
    real_indicators: bool = False,
) -> ScanContext:
    """Consecutive business days from ``START``; the scan day is the last row.

    ``spec`` maps a symbol to one entry per day: a bar, or None for a day with no bar. Every
    symbol lists the same number of days. ``atr`` fills every cell of ``atr14`` (None leaves them
    NaN) unless ``real_indicators`` asks for the true indicators.
    """
    n = len(next(iter(spec.values())))
    index = pd.bdate_range(START, periods=n)
    columns = list(spec)
    frames = {
        f: pd.DataFrame(np.nan, index=index, columns=columns)
        for f in ("open", "high", "low", "close", "volume")
    }
    for symbol, rows in spec.items():
        for k, row in enumerate(rows):
            if row is None:
                continue
            for field, value in zip(("open", "high", "low", "close"), row, strict=True):
                frames[field].at[index[k], symbol] = value
            frames["volume"].at[index[k], symbol] = 1_000_000.0
    panel = Panel(**frames)
    blank = pd.DataFrame(np.nan, index=index, columns=columns)
    if real_indicators:
        ind = compute_indicators(panel)
    else:
        fields = {f.name: blank for f in dataclasses.fields(Indicators)}
        fields["atr14"] = pd.DataFrame(np.nan if atr is None else atr, index=index, columns=columns)
        ind = Indicators(**fields)
    flags = pd.DataFrame(False, index=index, columns=columns)
    return ScanContext(
        session=pd.Timestamp(index[-1]),
        panel=panel,
        ind=ind,
        eligible=flags,
        rs_pct=blank,
        leaders=flags,
        regime=pd.Series(dtype=object),
        kinds={},
    )


def _day(k: int) -> date:
    """The k-th business day from ``START`` as a ``date`` (what ``opened_on`` holds)."""
    return date.fromisoformat(pd.bdate_range(START, periods=k + 1)[k].date().isoformat())


def _pos(opened_k: int | date, stop: float = 95.0, **over: Any) -> dict[str, Any]:
    """A position row as ``product_store.list_positions`` returns it, opened on day ``opened_k``."""
    pos: dict[str, Any] = {
        "id": "pos-1",
        "symbol": "ABC",
        "opened_on": _day(opened_k) if isinstance(opened_k, int) else opened_k,
        "entry_price": 100.0,
        "shares": 10.0,
        "initial_stop": stop,
        "current_stop": stop,
        "trail_atr": 3.0,
    }
    pos.update(over)
    return pos


def _ctx_closes(closes: list[float], **kw: Any) -> ScanContext:
    return _context({"ABC": _rows(closes)}, **kw)


# ---------------------------------------------------------------- rule 1: no bar on the scan day


def test_no_bar_on_scan_day_is_no_data_not_crash():
    # ABC traded for five sessions and then has no bar on the scan day; XYZ keeps trading, so the
    # session itself is a real one.
    spec: dict[str, list[Bar | None]] = {
        "ABC": [*_rows([100, 101, 102, 103, 104]), None],
        "XYZ": _rows([50, 51, 52, 53, 54, 55]),
    }
    ctx = _context(spec)
    act = position_action(_pos(0), ctx)
    assert act == Action(
        position_id="pos-1",
        symbol="ABC",
        kind=ActionKind.NO_DATA,
        stop=None,
        text="no price data for ABC since 2024-01-08",
    )


def test_no_data_names_the_last_day_with_a_close_not_the_last_row():
    spec: dict[str, list[Bar | None]] = {
        "ABC": [*_rows([100, 101, 102]), None, None, None],
        "XYZ": _rows([50, 51, 52, 53, 54, 55]),
    }
    act = position_action(_pos(0), _context(spec))
    assert act.kind is ActionKind.NO_DATA
    assert act.text == "no price data for ABC since 2024-01-04"


def test_symbol_missing_from_panel_is_no_data_since_opened_on():
    ctx = _context({"XYZ": _rows([50, 51, 52, 53])})
    act = position_action(_pos(1, symbol="TYPO"), ctx)
    assert act.kind is ActionKind.NO_DATA
    assert act.stop is None
    assert act.symbol == "TYPO"
    assert act.text == "no price data for TYPO since 2024-01-03"


def test_symbol_with_no_close_anywhere_is_no_data_since_opened_on():
    spec: dict[str, list[Bar | None]] = {"ABC": [None] * 4, "XYZ": _rows([50, 51, 52, 53])}
    act = position_action(_pos(2), _context(spec))
    assert act.kind is ActionKind.NO_DATA
    assert act.text == "no price data for ABC since 2024-01-04"


def test_no_bar_beats_every_other_rule():
    # Opened on the scan day (rule 2), and a session count past max_hold (rule 4) would both
    # apply if the symbol had a bar. It has none, so the answer is NO_DATA.
    spec: dict[str, list[Bar | None]] = {
        "ABC": [*_rows([100.0] * 40), None],
        "XYZ": _rows([50.0] * 41),
    }
    ctx = _context(spec)
    assert position_action(_pos(40), ctx).kind is ActionKind.NO_DATA
    assert position_action(_pos(0), ctx).kind is ActionKind.NO_DATA


def test_a_stale_lower_stop_is_never_reported_as_a_hold_when_there_is_no_bar():
    spec: dict[str, list[Bar | None]] = {"ABC": [*_rows([100, 101]), None], "XYZ": _rows([5, 6, 7])}
    act = position_action(_pos(0, stop=90.0), _context(spec))
    assert act.kind is ActionKind.NO_DATA and act.stop is None


# ---------------------------------------------------------------- rule 2: opened on the scan day


def test_opened_on_scan_day_holds_at_the_current_stop():
    ctx = _ctx_closes([100, 101, 102, 103])
    act = position_action(_pos(3, stop=95.0), ctx)
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 95.0, "hold, stop 95.00")


def test_opened_on_scan_day_ignores_a_low_through_the_stop():
    # The day's low (90) is under the stop (95), but the low may have printed before the fill.
    rows = [*_rows([100, 101, 102]), _bar(100.0, low=90.0)]
    act = position_action(_pos(3, stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.HOLD
    assert act.stop == 95.0
    assert act.text == "hold, stop 95.00"


def test_opened_on_scan_day_does_not_raise_the_stop():
    # Closes high enough that the trail (130 - 6 = 124) is far above the stop; opening day holds.
    act = position_action(_pos(0, stop=95.0), _ctx_closes([130.0]))
    assert act.kind is ActionKind.HOLD
    assert act.stop == 95.0


def test_opened_on_is_matched_by_date_whatever_type_the_store_returns():
    ctx = _ctx_closes([100, 101, 102, 103])
    as_timestamp = _pos(3, opened_on=pd.Timestamp(_day(3)))
    as_datetime = _pos(3, opened_on=pd.Timestamp(_day(3)).to_pydatetime())
    assert position_action(as_timestamp, ctx).text == "hold, stop 95.00"
    assert position_action(as_datetime, ctx).text == "hold, stop 95.00"


def test_a_position_dated_after_the_scan_day_is_held_not_stopped():
    # Scanning an earlier session than the position's open (a back-dated catch-up): the trade did
    # not exist yet, so a low through the stop on that day is not a stop-out.
    rows = [*_rows([100, 101]), _bar(100.0, low=80.0)]
    act = position_action(_pos(5, stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.HOLD
    assert act.text == "hold, stop 95.00"


# ---------------------------------------------------------------------- rule 3: the stop was hit


def test_low_under_the_stop_is_stop_hit():
    rows = [*_rows([100, 102, 104]), _bar(98.0, low=94.5)]
    act = position_action(_pos(0, stop=95.0), _context({"ABC": rows}))
    assert act == Action("pos-1", "ABC", ActionKind.STOP_HIT, 95.0, "stop hit, exit (stop 95.00)")


def test_low_exactly_at_the_stop_is_stop_hit():
    rows = [*_rows([100, 102, 104]), _bar(98.0, low=95.0)]
    act = position_action(_pos(0, stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.STOP_HIT
    assert act.text == "stop hit, exit (stop 95.00)"


def test_low_a_cent_above_the_stop_is_not_stop_hit():
    rows = [*_rows([100, 102, 104]), _bar(98.0, low=95.01)]
    act = position_action(_pos(0, stop=95.0), _context({"ABC": rows}))
    assert act.kind is not ActionKind.STOP_HIT


def test_stop_hit_reports_the_stop_in_force_not_a_new_trail():
    # The close of 130 would pull the trail to 124, but the low already breached the stop of 95.
    rows = [*_rows([100, 102, 104]), _bar(130.0, low=94.0)]
    act = position_action(_pos(0, stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.STOP_HIT
    assert act.stop == 95.0


def test_stop_hit_uses_a_raised_current_stop():
    rows = [*_rows([100, 110, 112]), _bar(108.0, low=104.5)]
    act = position_action(_pos(0, stop=104.5, initial_stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.STOP_HIT
    assert act.stop == 104.5
    assert act.text == "stop hit, exit (stop 104.50)"


def test_stop_hit_beats_max_hold():
    closes = [100.0] * 29 + [96.0]
    rows = [*_rows(closes[:-1]), _bar(96.0, low=94.0)]
    act = position_action(_pos(0, stop=95.0), _context({"ABC": rows}))
    assert act.kind is ActionKind.STOP_HIT


# ----------------------------------------------------------------------- rule 4: the time limit


def test_thirty_sessions_since_the_open_is_max_hold():
    # Days 0 to 29 inclusive are 30 sessions with a bar. Flat prices, so nothing else fires.
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100.0] * 30))
    assert act == Action(
        "pos-1",
        "ABC",
        ActionKind.MAX_HOLD,
        95.0,
        "max hold reached (30 sessions), consider exiting",
    )


def test_twenty_nine_sessions_is_not_yet_max_hold():
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100.0] * 29))
    assert act.kind is ActionKind.HOLD
    assert act.text == "hold, stop 95.00"


def test_max_hold_is_a_parameter_and_the_text_follows_it():
    ctx = _ctx_closes([100.0] * 5)
    act = position_action(_pos(0, stop=95.0), ctx, max_hold=5)
    assert act.kind is ActionKind.MAX_HOLD
    assert act.stop == 95.0
    assert act.text == "max hold reached (5 sessions), consider exiting"
    assert position_action(_pos(0, stop=95.0), ctx, max_hold=6).kind is ActionKind.HOLD


def test_max_hold_beats_a_trail_raise():
    # Rising closes would raise the stop (129 - 6 = 123), but the time limit is up.
    closes = [float(100 + i) for i in range(30)]
    act = position_action(_pos(0, stop=95.0), _ctx_closes(closes))
    assert act.kind is ActionKind.MAX_HOLD
    assert act.stop == 95.0


def test_max_hold_counts_only_sessions_with_a_bar():
    # 30 rows since the open but two have no bar: 28 held sessions, so not yet.
    rows: list[Bar | None] = _rows([100.0] * 30)
    rows[10] = None
    rows[11] = None
    spec = {"ABC": rows}
    assert position_action(_pos(0, stop=95.0), _context(spec)).kind is ActionKind.HOLD
    # Two more sessions with bars bring it to 30.
    longer = [*rows, *_rows([100.0, 100.0])]
    assert (
        position_action(_pos(0, stop=95.0), _context({"ABC": longer})).kind is ActionKind.MAX_HOLD
    )


def test_max_hold_counts_from_the_first_session_after_a_non_session_open():
    # Saturday 2024-01-06 is not a session: the count starts on Monday 2024-01-08 (row 4).
    # Rows 4 to 33 are 30 sessions; rows 0 to 3 must not be counted.
    ctx = _ctx_closes([100.0] * 34)
    saturday = _pos(date(2024, 1, 6), stop=95.0)
    assert position_action(saturday, ctx).kind is ActionKind.MAX_HOLD
    assert position_action(saturday, _ctx_closes([100.0] * 33)).kind is ActionKind.HOLD


def test_sessions_before_the_open_are_not_counted_toward_max_hold():
    ctx = _ctx_closes([100.0] * 40)
    assert position_action(_pos(15, stop=95.0), ctx).kind is ActionKind.HOLD  # rows 15 to 39: 25
    assert position_action(_pos(10, stop=95.0), ctx).kind is ActionKind.MAX_HOLD  # 10 to 39: 30


# ----------------------------------------------------------------------- rule 5: the trail


def test_trail_above_the_stop_raises_it():
    # Highest close 110, ATR 2.0, trail_atr 3.0: 110 - 6 = 104, above the stop of 95.
    closes = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
    act = position_action(_pos(0, stop=95.0), _ctx_closes(closes))
    assert act == Action(
        "pos-1", "ABC", ActionKind.RAISE_STOP, 104.0, "raise stop to 104.00 (trail)"
    )


def test_trail_uses_the_highest_close_since_the_open_not_todays_close():
    # The peak close was 120; today closes at 112. Trail = 120 - 6 = 114, not 112 - 6 = 106.
    closes = [100.0, 110.0, 120.0, 116.0, 112.0]
    act = position_action(_pos(0, stop=95.0), _ctx_closes(closes))
    assert act.kind is ActionKind.RAISE_STOP
    assert act.stop == 114.0
    assert act.text == "raise stop to 114.00 (trail)"


def test_trail_includes_the_open_day_close():
    # The highest close is the opening day's (130), then a lower stretch. Trail = 130 - 6 = 124.
    closes = [130.0, 128.0, 126.0, 124.0]
    act = position_action(_pos(0, stop=95.0), _ctx_closes(closes))
    assert act.kind is ActionKind.RAISE_STOP
    assert act.stop == 124.0


def test_trail_ignores_closes_before_the_open():
    # A 150 close the week before the user bought must not drag the stop up to 144.
    closes = [150.0, 100.0, 101.0, 102.0, 103.0]
    act = position_action(_pos(1, stop=95.0), _ctx_closes(closes))
    assert act.kind is ActionKind.RAISE_STOP
    assert act.stop == 97.0  # 103 - 6
    assert act.text == "raise stop to 97.00 (trail)"


def test_trail_is_never_lowered():
    # Stop already raised to 110. Highest close 112 gives only 106, below it.
    closes = [100.0, 108.0, 112.0, 111.0, 112.0]
    act = position_action(_pos(0, stop=110.0, initial_stop=95.0), _ctx_closes(closes))
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 110.0, "hold, stop 110.00")


def test_trail_that_has_not_cleared_the_stop_holds():
    # 101 - 6 = 95 equals the stop: no raise.
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100.0, 101.0, 100.0]))
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 95.0, "hold, stop 95.00")


def test_trail_uses_the_atr_on_the_scan_day():
    ctx = _ctx_closes([100.0, 110.0, 110.0])
    index = ctx.ind.atr14.index
    atr = ctx.ind.atr14.copy()
    atr.loc[index[:-1]] = 99.0  # earlier days must not matter
    atr.loc[index[-1]] = 4.0
    ctx = dataclasses.replace(ctx, ind=dataclasses.replace(ctx.ind, atr14=atr))
    act = position_action(_pos(0, stop=95.0), ctx)
    assert act.kind is ActionKind.RAISE_STOP
    assert act.stop == 98.0  # 110 - 3 * 4
    assert act.text == "raise stop to 98.00 (trail)"


def test_trail_atr_comes_from_the_position():
    closes = [100.0, 104.0, 110.0]
    wide = position_action(_pos(0, stop=95.0, trail_atr=4.0), _ctx_closes(closes))
    tight = position_action(_pos(0, stop=95.0, trail_atr=2.0), _ctx_closes(closes))
    assert wide.stop == 102.0 and wide.kind is ActionKind.RAISE_STOP  # 110 - 4 * 2
    assert tight.stop == 106.0 and tight.kind is ActionKind.RAISE_STOP  # 110 - 2 * 2


def test_a_position_without_a_trail_is_refused_not_given_the_code_default():
    """The incident: positions trailed at the code default of 3.0 ATR while the sealed
    pre-registrations, the idea's narration and forward tracking all said 3.5. A row with no
    ``trail_atr`` is resolved through the pre-registered lookup before it gets here; a
    ``BreakoutParams()`` fallback here would silently bring the 3.0 back."""
    with pytest.raises(ValueError, match="no trail_atr"):
        position_action(_pos(0, stop=95.0, trail_atr=None), _ctx_closes([100.0, 104.0, 110.0]))


def test_exit_rules_come_from_the_committed_preregistrations():
    """A linked idea takes its own setup and version's sealed parameters; an unlinked trade takes
    the breakout family's as tonight's scan runs it. Both preregs seal ``trail_atr`` 3.5."""
    breakout = json.loads((REPO / "ml/validation/breakout/breakout-v1.prereg.json").read_text())
    pullback = json.loads((REPO / "ml/validation/pullback/pullback-v1.prereg.json").read_text())
    linked = exit_params({"setup": "pullback", "version": "pullback-v1"}, REPO)
    assert linked.trail_atr == pullback["params"]["trail_atr"] == 3.5
    assert linked.max_hold == pullback["params"]["max_hold"]
    unlinked = exit_params(None, REPO)
    assert isinstance(unlinked, BreakoutParams) and unlinked.version == "breakout-v1"
    assert unlinked.trail_atr == breakout["params"]["trail_atr"] == 3.5
    assert unlinked.trail_atr != BreakoutParams().trail_atr  # premise: the default differs


def test_with_exit_rules_keeps_a_stored_trail_and_fills_a_missing_one():
    params = dataclasses.replace(BreakoutParams(), trail_atr=3.5)
    assert with_exit_rules(_pos(0, trail_atr=None), params)["trail_atr"] == 3.5
    assert with_exit_rules(_pos(0, trail_atr=2.0), params)["trail_atr"] == 2.0


async def test_resolve_exit_rules_reads_the_linked_idea_or_falls_back_to_breakout():
    ideas = {"idea-1": {"setup": "pullback", "version": "pullback-v1"}}

    async def get_idea(idea_id: Any) -> dict[str, Any] | None:
        return ideas.get(str(idea_id))

    linked, hold = await resolve_exit_rules(_pos(0, trail_atr=None, idea_id="idea-1"), get_idea, REPO)
    assert linked["trail_atr"] == 3.5 and hold == 30
    gone, _ = await resolve_exit_rules(_pos(0, trail_atr=None, idea_id="gone"), get_idea, REPO)
    assert gone["trail_atr"] == exit_params(None, REPO).trail_atr


def test_a_raise_of_less_than_a_cent_is_a_hold():
    # Trail = 101.005 - 6 = 95.005: half a cent over the stop.
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100.0, 101.005]))
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 95.0, "hold, stop 95.00")


def test_a_raise_of_exactly_a_cent_is_a_raise():
    # 101.02 - 6 is 95.02 and the stop is 95.01, but in floating point the gap is
    # 0.00999999999999: it is still one cent, and must not be lost to rounding.
    assert 101.02 - 6.0 - 95.01 < 0.01
    act = position_action(_pos(0, stop=95.01), _ctx_closes([100.0, 101.02]))
    assert act.kind is ActionKind.RAISE_STOP
    assert act.text == "raise stop to 95.02 (trail)"
    assert act.stop is not None and math.isclose(act.stop, 95.02)


def test_nan_atr_on_the_scan_day_holds_at_the_current_stop():
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100.0, 110.0, 120.0], atr=None))
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 95.0, "hold, stop 95.00")


def test_a_raise_applies_only_after_the_stop_and_time_checks_pass():
    # The rule order in one place: the same rising prices give RAISE_STOP while nothing else
    # fires, and the other kinds the moment their own condition is met.
    closes = [100.0, 104.0, 108.0, 112.0]
    assert position_action(_pos(0, stop=95.0), _ctx_closes(closes)).kind is ActionKind.RAISE_STOP
    assert position_action(_pos(3, stop=95.0), _ctx_closes(closes)).kind is ActionKind.HOLD
    assert position_action(_pos(0, stop=95.0), _ctx_closes(closes), max_hold=4).kind is (
        ActionKind.MAX_HOLD
    )
    low_through = [*_rows(closes[:-1]), _bar(112.0, low=90.0)]
    assert position_action(_pos(0, stop=95.0), _context({"ABC": low_through})).kind is (
        ActionKind.STOP_HIT
    )


# ----------------------------------------------------------------------------- the Action itself


def test_action_carries_a_string_id_and_the_symbol():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    act = position_action(_pos(3, id=uid), _ctx_closes([100, 101, 102, 103]))
    assert act.position_id == "12345678-1234-5678-1234-567812345678"
    assert isinstance(act.position_id, str)
    assert act.symbol == "ABC"


def test_action_is_frozen():
    act = position_action(_pos(3), _ctx_closes([100, 101, 102, 103]))
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.kind = ActionKind.NO_DATA


def test_action_kind_values():
    assert {k.name: k.value for k in ActionKind} == {
        "HOLD": "hold",
        "RAISE_STOP": "raise_stop",
        "STOP_HIT": "stop_hit",
        "MAX_HOLD": "max_hold",
        "NO_DATA": "no_data",
    }


def test_the_inputs_are_not_modified():
    ctx = _ctx_closes([100.0, 104.0, 110.0])
    pos = _pos(0, stop=95.0)
    before_pos = dict(pos)
    before_close = ctx.panel.close.copy()
    position_action(pos, ctx)
    assert pos == before_pos
    pd.testing.assert_frame_equal(ctx.panel.close, before_close)


# ------------------------------------------------------------- the same line as simulate_trade


def _follow_the_trade(seed: int, real_indicators: bool) -> list[tuple[Action, Any]]:
    """Run one trade on a drifting random walk through ``simulate_trade`` and ``position_action``.

    The trade enters on the session after the signal. From the night after entry, each night asks
    ``position_action`` what to do with the stop it was told the night before (a user who follows
    every ``RAISE_STOP``) and runs ``simulate_trade`` on the bars up to that night. Returns each
    night's ``(action, outcome)``, ending with the night the model closes the trade.
    """
    bars = make_bars("ABC", trend_series(90, start=100.0, drift=0.004, vol=0.012, seed=seed))
    rows: list[Bar | None] = list(
        zip(bars["open"], bars["high"], bars["low"], bars["close"], strict=True)
    )
    full = _context({"ABC": rows}, real_indicators=real_indicators)
    frame = pd.DataFrame(
        {
            "open": full.panel.open["ABC"],
            "high": full.panel.high["ABC"],
            "low": full.panel.low["ABC"],
            "close": full.panel.close["ABC"],
            "atr14": full.ind.atr14["ABC"],
        }
    )
    sig = 30
    signal_date = frame.index[sig].date()
    trigger = float(frame["close"].iloc[sig]) * 0.999
    stop = float(frame["close"].iloc[sig]) - 3.0 * float(frame["atr14"].iloc[sig])
    opened_on = frame.index[sig + 1].date()
    ex = ExecutionParams()

    nights: list[tuple[Action, Any]] = []
    tracked = stop
    for k in range(sig + 2, len(frame)):
        out = simulate_trade(frame.iloc[: k + 1], signal_date, trigger, stop, 3.0, 30, ex)
        assert out.status in (TrackStatus.OPEN, TrackStatus.CLOSED)
        truncated = _context({"ABC": rows[: k + 1]}, real_indicators=real_indicators)
        act = position_action(_pos(opened_on, stop=stop, current_stop=tracked), truncated)
        nights.append((act, out))
        if out.status is TrackStatus.CLOSED:
            break
        assert act.stop is not None
        tracked = act.stop
    return nights


def test_with_a_steady_atr_the_nightly_trail_is_the_one_simulate_trade_holds():
    """Same formula, same stop: to the cent, below which a raise is not announced.

    With ATR 2.0 on every day the backtest's day-by-day ratchet and tonight's one-shot formula
    (highest close since the open minus ``trail_atr`` x ATR) are the same line. The user's stop
    can sit a cent under the model's, because a raise smaller than a cent is not announced, and
    the stop and time-limit exits fire on the very night the model closes the trade.
    """
    raised = 0
    for seed in range(8):
        nights = _follow_the_trade(seed, real_indicators=False)
        *open_nights, (last_act, last_out) = nights
        assert last_out.status is TrackStatus.CLOSED, f"seed {seed} never closed"
        expected = (
            ActionKind.MAX_HOLD
            if last_out.exit_reason is ExitReason.MAX_HOLD
            else ActionKind.STOP_HIT
        )
        assert last_act.kind is expected, (seed, last_act, last_out)
        for act, out in open_nights:
            assert act.kind in (ActionKind.HOLD, ActionKind.RAISE_STOP), (seed, act, out)
            assert act.stop is not None and out.current_stop is not None
            assert act.stop <= out.current_stop + 1e-9, (seed, act, out)
            assert act.stop == pytest.approx(out.current_stop, abs=0.01), (seed, act, out)
            raised += act.kind is ActionKind.RAISE_STOP
    assert raised >= 20, "the walks should have raised the trail many times"


def test_with_a_real_atr_the_nightly_stop_is_never_above_the_model_stop():
    """With a moving ATR the two can differ, always on the side of the lower stop.

    The model keeps every earlier night's candidate (``highest close - trail_atr x ATR`` as of
    that night); tonight's formula uses tonight's ATR, and opening day only holds. So when the ATR
    has risen the position's trail can sit under the model's, never over it.
    """
    for seed in range(8):
        for act, out in _follow_the_trade(seed, real_indicators=True):
            if out.status is TrackStatus.OPEN:
                assert act.stop is not None and out.current_stop is not None
                assert act.stop <= out.current_stop + 1e-9, (seed, act, out)


# ------------------------------------------------------------- the nightly update (the job's step)
#
# ``nightly_update`` is what the job stores for each open position after a close: the position is
# rescaled first if a split happened since it was bought, then the rules above are walked over every
# session since it was last advised (``last_action_on``; a position never advised is judged on the
# scan day alone), so a stop hit on a day the Mac slept through is reported instead of tonight's
# hold. A position already advised for this session is left as it is: a second run of the same
# session must say the same thing, not test tonight's low against the stop the first run raised.


def test_the_raise_text_is_the_one_position_action_uses():
    act = position_action(_pos(0, stop=95.0), _ctx_closes([100, 110, 111]))
    assert act.kind is ActionKind.RAISE_STOP and act.stop is not None
    assert act.text == raise_stop_text(act.stop) == "raise stop to 105.00 (trail)"


@pytest.mark.parametrize(
    ("ratio", "snapped"),
    [
        pytest.param(1.0, 1.0, id="no-split"),
        pytest.param(0.75, 1.0, id="band-low-edge"),
        pytest.param(1.33, 1.0, id="band-high-edge"),
        pytest.param(0.25, 0.25, id="4-for-1"),
        pytest.param(0.30, 1 / 3, id="near-3-for-1"),
        pytest.param(0.60, 2 / 3, id="3-for-2"),
        pytest.param(1.34, 3 / 2, id="just-over-the-band"),
        pytest.param(7.0, 8.0, id="nearest-by-ratio-not-by-difference"),
        pytest.param(0.07, 0.1, id="1-for-10-reverse"),
    ],
)
def test_split_ratio_snaps_outside_the_band(ratio, snapped):
    ctx = _ctx_closes([100.0, 100.0, 100.0])
    assert split_ratio(_pos(1, entry_price=100.0 / ratio), ctx) == pytest.approx(snapped)


def test_split_ratio_needs_a_bar_on_the_open_day_and_a_positive_entry():
    spec: dict[str, list[Bar | None]] = {
        "ABC": [_bar(25.0), None, _bar(25.0)],
        "XYZ": _rows([5] * 3),
    }
    ctx = _context(spec)
    assert split_ratio(_pos(0), ctx) == 0.25  # bought at 100, now 25 on the open day
    assert split_ratio(_pos(1), ctx) == 1.0  # no bar on the open day
    assert split_ratio(_pos(0, symbol="TYPO"), ctx) == 1.0
    assert split_ratio(_pos(0, entry_price=0.0), ctx) == 1.0
    assert split_ratio(_pos(5), ctx) == 1.0  # opened after the scan day


def test_rescaled_keeps_the_money_at_risk():
    pos = _pos(0, stop=90.0, current_stop=96.0, shares=12.0)
    out = rescaled(pos, 0.25)
    assert out == {"entry_price": 25.0, "initial_stop": 22.5, "current_stop": 24.0, "shares": 48.0}
    risk = lambda p: p["shares"] * (p["entry_price"] - p["current_stop"])  # noqa: E731
    assert risk({**pos, **out}) == pytest.approx(risk(pos))
    assert "current_stop" not in rescaled({**pos, "current_stop": None}, 2.0)


def _walked(closes: list[Bar | None], **pos: Any) -> tuple[Action, float]:
    spec: dict[str, list[Bar | None]] = {"ABC": closes, "XYZ": _rows([50.0] * len(closes))}
    return walk_action(_pos(0, **pos), _context(spec), max_hold=pos.pop("max_hold", 30))


def test_a_stop_hit_on_a_day_since_the_last_advice_is_the_action():
    # Advised on Thu 4 Jan (day 2); Fri 5 Jan dipped through the stop; Mon 8 Jan looks fine.
    bars = [_bar(100), _bar(100), _bar(100), _bar(100, low=94.0), _bar(100)]
    act, stop = _walked(bars, last_action_on=_day(2))
    assert act.kind is ActionKind.STOP_HIT and stop == 95.0
    assert act.text == "Fri 5 Jan: stop hit, exit (stop 95.00)"


def test_the_walk_starts_after_the_last_advice_not_on_it():
    bars = [_bar(100), _bar(100), _bar(100, low=94.0), _bar(100), _bar(100)]
    act, _ = _walked(bars, last_action_on=_day(2))
    assert act.kind is ActionKind.HOLD  # day 2 was already advised


def test_a_position_never_advised_is_judged_on_the_scan_day_only():
    """Ruling (Task 8): no walk back to ``opened_on``. The user records the stop as it is today,
    so testing it against the lows since the open would report a stop hit (Wed 3 Jan here) from
    before ATLAS knew of the position. The walk starts once it has been advised."""
    bars = [_bar(100), _bar(100, low=94.0), _bar(100), _bar(100)]
    act, stop = _walked(bars)
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 95.0, "hold, stop 95.00")
    assert stop == 95.0


def test_a_never_advised_position_still_meets_the_stop_on_the_scan_day():
    act, _ = _walked([_bar(100), _bar(100), _bar(100, low=94.0)])
    assert act == Action("pos-1", "ABC", ActionKind.STOP_HIT, 95.0, "stop hit, exit (stop 95.00)")


def test_a_stop_raised_on_an_earlier_day_carries_and_is_told_even_if_tonight_holds():
    bars = _rows([100, 110, 108, 108])  # day 1 raises the trail to 104; nothing moves after
    act, stop = _walked(bars, last_action_on=_day(0))
    assert stop == 104.0
    assert act == Action(
        "pos-1", "ABC", ActionKind.RAISE_STOP, 104.0, "raise stop to 104.00 (trail)"
    )


def test_a_carried_raise_is_named_beside_a_max_hold():
    act, stop = _walked(_rows([100, 110, 108, 108]), last_action_on=_day(0), max_hold=3)
    assert act.kind is ActionKind.MAX_HOLD and stop == 104.0
    assert act.text == (
        "max hold reached (3 sessions), consider exiting; raise stop to 104.00 (trail)"
    )


def test_a_carried_raise_is_named_beside_no_data():
    act, stop = _walked([*_rows([100, 110, 108]), None], last_action_on=_day(0))
    assert act.kind is ActionKind.NO_DATA and stop == 104.0
    assert act.text == "no price data for ABC since 2024-01-04; raise stop to 104.00 (trail)"


def test_a_walk_over_tonight_only_is_position_action():
    ctx = _ctx_closes([100, 110, 111])
    pos = _pos(0, stop=95.0, last_action_on=_day(1))
    act, stop = walk_action(pos, ctx)
    assert act == position_action(pos, ctx) and stop == act.stop


def test_raised_stops_are_kept_to_the_cent():
    act, stop = _walked(_rows([100, 101.017, 101.0]), last_action_on=_day(1))  # trail 95.017
    assert act.kind is ActionKind.RAISE_STOP and stop == 95.02 == act.stop
    assert act.text == "raise stop to 95.02 (trail)"


def test_nightly_update_stores_the_action_for_the_session():
    ctx = _ctx_closes([100, 110, 111])
    update = nightly_update(_pos(0, stop=95.0, last_action_on=_day(1)), ctx)
    assert update.action.kind is ActionKind.RAISE_STOP and not update.reused
    assert update.fields == {
        "last_action": "raise_stop",
        "last_action_text": "raise stop to 105.00 (trail)",
        "last_action_on": _day(2),
        "current_stop": 105.0,
    }


def test_the_same_session_twice_gives_the_same_action_and_stop():
    """The incident: a re-run tested tonight's low against the stop the first run had just raised,
    so "raise stop to 109.00 (trail)" turned into "stop hit, exit (stop 109.00)"."""
    bars = [_bar(100), _bar(110), _bar(110, low=108.5)]
    ctx = _context({"ABC": bars}, atr=2.0)
    pos = _pos(0, stop=108.4, trail_atr=0.5, last_action_on=_day(1))
    first = nightly_update(pos, ctx)
    assert first.action.kind is ActionKind.RAISE_STOP and first.action.stop == 109.0  # premise
    advised = {**pos, **first.fields}
    assert position_action(advised, ctx).kind is ActionKind.STOP_HIT  # premise: re-testing flips

    again = nightly_update(advised, ctx)
    assert again.reused and again.fields == {}
    assert again.action == first.action


def test_a_stored_no_data_action_is_rebuilt_without_a_stop():
    ctx = _ctx_closes([100, 101])
    pos = _pos(
        0,
        last_action="no_data",
        last_action_text="no price data for ABC since 2024-01-02",
        last_action_on=_day(1),
    )
    update = nightly_update(pos, ctx)
    assert update.reused and update.action == Action(
        "pos-1", "ABC", ActionKind.NO_DATA, None, "no price data for ABC since 2024-01-02"
    )


def test_a_split_is_applied_before_the_rules_and_named_first():
    ctx = _ctx_closes([25.0, 25.5, 26.0])  # bought at 100 before a 4-for-1 split
    update = nightly_update(_pos(0, stop=88.0, shares=10.0, last_action_on=_day(1)), ctx)
    assert update.ratio == 0.25
    assert update.fields["entry_price"] == 25.0 and update.fields["shares"] == 40.0
    assert update.fields["initial_stop"] == 22.0
    assert update.action.text.startswith("adjusted for a split (\u00d70.25); ")
    assert update.action.kind is not ActionKind.STOP_HIT  # 4:1 is not a crash through the stop


# ------------------------------------------------- a stop edited after the close (Task 8 PATCH)


def test_a_stop_set_after_the_close_is_not_tested_against_that_sessions_low():
    """``PATCH /v1/positions/{id}`` re-advises a position at once when the user edits its stop.
    That stop was not working during tonight's session, so tonight's low says nothing about it:
    ``check_stop=False`` skips rule 3 and nothing else."""
    ctx = _ctx_closes([100, 110, 111])  # tonight's low is 110; the trail is 111 - 3 x 2 = 105
    pos = _pos(0, stop=110.5)
    assert position_action(pos, ctx).kind is ActionKind.STOP_HIT  # premise: rule 3 would fire

    act = position_action(pos, ctx, check_stop=False)
    assert act == Action("pos-1", "ABC", ActionKind.HOLD, 110.5, "hold, stop 110.50")
    update = nightly_update(pos, ctx, check_stop=False)
    assert update.action == act and update.fields["last_action"] == "hold"


def test_an_edited_stop_under_the_trail_is_still_raised():
    act = position_action(_pos(0, stop=100.0), _ctx_closes([100, 110, 111]), check_stop=False)
    assert act == Action("pos-1", "ABC", ActionKind.RAISE_STOP, 105.0, raise_stop_text(105.0))
