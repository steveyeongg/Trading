"""Swing engine: plain-English idea narration and the eligibility checklist.

Two views of the same engine decisions for the Today and stock pages:

* ``narrate.why`` / ``narrate.cancels`` turn an :class:`Idea` into the sentences a person reads.
  Golden strings are pinned on a small hand-built panel (``AAA`` makes a breakout candidate on
  row 296 and a pullback candidate on row 297), and the copy must never promise a result.
* ``checklist.eligibility_checklist`` lists, for any symbol, each universe, leader and setup rule
  with its actual value. A tick in the checklist must agree with the engine's own decision, so a
  loop over every symbol of a synthetic universe (with deliberately broken stocks mixed in)
  compares the checklist to ``ctx.eligible``, ``ctx.leaders`` and the setup candidates.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest
from swing_engine.checklist import CheckItem, eligibility_checklist
from swing_engine.config import (
    BreakoutParams,
    PullbackParams,
    RiskSettings,
    UniverseFilter,
)
from swing_engine.data.assets import AssetKind
from swing_engine.narrate import SETUP_LABEL, cancels, why
from swing_engine.panel import Panel, build_panel
from swing_engine.scan import Idea, ScanContext, SetupSpec, build_context, run_scan
from swing_engine.setups import candidates_for
from swing_engine.testing import make_bars, synthetic_kinds, synthetic_universe_panel

UNIVERSE = UniverseFilter()

# The 14 rules in display order: universe (1-6), leader gate (7-11), setups (12-14).
LABELS = [
    "Has a bar for Fri 19 Feb",
    "Price at least $10",
    "50-day median dollar volume at least $20M",
    "Daily range at least 0.5% of price (not pinned)",
    "At least 252 sessions of history",
    "Common stock (not an ETF or fund)",
    "Close above 50-day average",
    "50-day average above 200-day average",
    "200-day average rising (vs 20 sessions ago)",
    "Stronger than 70% of US stocks (relative strength)",
    "Within 25% of 52-week high",
    "Breakout: close within 2% of 50-day closing high",
    "Breakout: quiet stretch (ATR10 / ATR50 at most 0.85)",
    "Pullback: 2-day RSI at most 10 or close below 20-day average",
]
N_UNIVERSE, N_LEADER = 6, 5  # rules 1-6 are the universe, rules 7-11 the leader gate


# ------------------------------------------------------------------------------- small panel
#
# SPY is a steady uptrend (so the market regime is not what is being tested). AAA climbs 0.3% a
# day with wide ranges that go quiet for the last 30 sessions, then on row 296 closes 1% under its
# high (a breakout candidate: quiet, within 2% of the 50-day closing high), and on rows 297 to 299
# falls 2% a day (a pullback candidate on 297 and 298, and on 299 it drops under its 50-day
# average, so it is eligible but no longer a leader). CCC is a steady decline (eligible, never a
# leader). DDD stops printing on row 289.

N = 300
BREAKOUT_ROW, PULLBACK_ROW, BELOW_SMA50_ROW = 296, 297, 299


def _aaa() -> pd.DataFrame:
    closes = list(50.0 * 1.003 ** np.arange(N))
    closes[296] = closes[295] * 0.99
    for k in (297, 298, 299):
        closes[k] = closes[k - 1] * 0.98
    bars = make_bars("AAA", closes, range_pct=0.03)
    quiet = np.where(np.arange(N) < 270, 0.03, 0.004)
    bars["high"] = np.maximum(bars["open"], bars["close"]) * (1.0 + quiet)
    bars["low"] = np.minimum(bars["open"], bars["close"]) * (1.0 - quiet)
    return bars


def _small() -> tuple[Panel, dict[str, AssetKind]]:
    frames = [
        make_bars("SPY", list(300.0 * 1.0015 ** np.arange(N))),
        _aaa(),
        make_bars("CCC", list(50.0 * 0.998 ** np.arange(N)), range_pct=0.02),
        make_bars("DDD", list(60.0 * 1.002 ** np.arange(290)), range_pct=0.02),
    ]
    p = build_panel(pd.concat(frames))
    panel = dataclasses.replace(p, raw_close=p.close.copy())
    kinds = {
        "SPY": AssetKind.ETF,
        "AAA": AssetKind.COMMON,
        "CCC": AssetKind.COMMON,
        "DDD": AssetKind.COMMON,
    }
    return panel, kinds


SMALL, SMALL_KINDS = _small()


def _ctx(row: int, panel: Panel = SMALL, kinds=SMALL_KINDS) -> ScanContext:
    return build_context(panel, kinds, panel.dates()[row].date())


def _idea(row: int, setup: str, panel: Panel = SMALL, kinds=SMALL_KINDS) -> Idea:
    """The scan's own idea for AAA on ``row`` (default parameters, unvalidated)."""
    specs = [SetupSpec(BreakoutParams(), live=False), SetupSpec(PullbackParams(), live=False)]
    result = run_scan(panel, kinds, panel.dates()[row].date(), RiskSettings(), specs)
    found = [i for i in (*result.ideas, *result.watch_only) if i.setup == setup]
    assert [i.symbol for i in found] == ["AAA"]
    return found[0]


def _by_label(items: list[CheckItem]) -> dict[str, CheckItem]:
    return {item.rule: item for item in items}


# ------------------------------------------------------------------------------- narration


def test_setup_labels():
    assert SETUP_LABEL == {"breakout": "Breakout", "pullback": "Pullback"}


def test_why_breakout_golden():
    idea = _idea(BREAKOUT_ROW, "breakout")
    assert why(idea, _ctx(BREAKOUT_ROW)) == (
        "AAA is in a long-term uptrend (above its 50- and 200-day averages) and stronger than "
        "100% of US stocks over the past year. It closed within 1.0% of its 50-day high after a "
        "quiet stretch (recent daily range 42% of normal). A move above 121.68 next session "
        "would be a breakout."
    )


def test_why_pullback_golden():
    idea = _idea(PULLBACK_ROW, "pullback")
    assert why(idea, _ctx(PULLBACK_ROW)) == (
        "AAA is in a long-term uptrend and stronger than 100% of US stocks, but has dipped over "
        "the last few days (2-day RSI 6). Buying only above 120.37, today's high, waits for "
        "buyers to step back in."
    )


def test_why_pullback_without_oversold_rsi_names_the_20_day_average():
    # Row 298: 2-day RSI is 2.3, so under a stricter RSI limit the idea stands only because the
    # close is under its 20-day average.
    ctx = _ctx(298)
    idea = _idea(298, "pullback")
    strict = PullbackParams(rsi2_max=1.0)
    assert "(closed below its 20-day average)" in why(idea, ctx, params=strict)
    assert "(2-day RSI 2)" in why(idea, ctx)


def test_why_rounds_relative_strength_to_a_whole_percent():
    ctx = _ctx(BREAKOUT_ROW)
    idea = dataclasses.replace(_idea(BREAKOUT_ROW, "breakout"), rs_pct=87.6)
    assert "stronger than 88% of US stocks" in why(idea, ctx)


def test_cancels_golden_with_default_parameters():
    idea = _idea(BREAKOUT_ROW, "breakout")
    assert cancels(idea, date(2021, 2, 22), BreakoutParams()) == (
        "Cancelled if it doesn't trade above 121.68 on Mon 22 Feb, or if it opens more than one "
        "ATR above that. After entry, the trade is wrong below 117.48; the stop then trails 3.0 "
        "ATR under the highest close, and the trade ends after 30 sessions."
    )


def test_cancels_reads_trail_and_max_hold_from_the_setup_params():
    idea = _idea(BREAKOUT_ROW, "breakout")
    tuned = BreakoutParams(trail_atr=3.5, max_hold=25)
    text = cancels(idea, date(2021, 2, 22), tuned)
    assert "trails 3.5 ATR under the highest close" in text
    assert "ends after 25 sessions" in text
    pullback = _idea(PULLBACK_ROW, "pullback")
    text = cancels(pullback, date(2021, 2, 23), PullbackParams(trail_atr=2.5, max_hold=20))
    assert text.startswith("Cancelled if it doesn't trade above 120.37 on Tue 23 Feb,")
    assert "trails 2.5 ATR" in text
    assert "ends after 20 sessions" in text


def test_narration_rejects_an_unknown_setup():
    idea = dataclasses.replace(_idea(BREAKOUT_ROW, "breakout"), setup="mystery")
    with pytest.raises(ValueError, match="mystery"):
        why(idea, _ctx(BREAKOUT_ROW))


PROMISES = ("guarantee", "will rise", "sure", "profit")


def _assert_no_promises(text: str) -> None:
    lowered = text.lower()
    for word in PROMISES:
        assert word not in lowered, f"{word!r} in {text!r}"


def test_copy_has_no_promises():
    panel = synthetic_universe_panel(seed=5)
    kinds = synthetic_kinds(panel)
    session = panel.dates()[450]
    ctx = build_context(panel, kinds, session.date())
    specs = [SetupSpec(BreakoutParams(), live=False), SetupSpec(PullbackParams(), live=False)]
    result = run_scan(panel, kinds, session.date(), RiskSettings(), specs)
    ideas = [*result.ideas, *result.watch_only]
    assert {i.setup for i in ideas} == {"breakout", "pullback"}  # the loop is not vacuous
    for idea in ideas:
        params = BreakoutParams() if idea.setup == "breakout" else PullbackParams()
        _assert_no_promises(why(idea, ctx, params=params))
        _assert_no_promises(cancels(idea, date(2021, 1, 1), params))
        for item in eligibility_checklist(ctx, idea.symbol, UNIVERSE):
            _assert_no_promises(" ".join([item.rule, item.value, item.threshold]))
    # the forbidden words are really being looked for
    with pytest.raises(AssertionError):
        _assert_no_promises("This will rise for sure")


# ------------------------------------------------------------------------------- checklist


def test_checklist_has_the_fourteen_rules_in_order():
    items = eligibility_checklist(_ctx(BREAKOUT_ROW), "AAA", UNIVERSE)
    assert [i.rule for i in items] == LABELS
    assert all(isinstance(i, CheckItem) for i in items)


def test_checklist_golden_values_for_a_breakout_candidate():
    items = eligibility_checklist(_ctx(BREAKOUT_ROW), "AAA", UNIVERSE)
    got = [(i.passed, i.value, i.threshold) for i in items]
    assert got == [
        (True, "Yes", "Yes"),
        (True, "$119.78", "$10.00"),
        (True, "$225.5M", "$20.0M"),
        (True, "1.75%", "0.50%"),
        (True, "297", "252"),
        (True, "common", "common"),
        (True, "$119.78", "$112.84"),  # close vs the 50-day average
        (True, "$112.84", "$91.42"),  # 50-day vs 200-day average
        (True, "$91.42", "$86.11"),  # 200-day average now vs 20 sessions ago
        (True, "100.0%", "70.0%"),
        (True, "1.0%", "25.0%"),  # how far under the 52-week high
        (True, "1.0%", "2.0%"),  # how far under the 50-day closing high
        (True, "0.42", "0.85"),
        (
            False,
            "RSI 23.0, close $119.78 vs avg $117.85",
            "RSI 10 or lower, or close at or below avg",
        ),
    ]


def test_checklist_marks_failed_rules_with_values():
    # An ETF: fails only "Common stock", and it is never ranked so relative strength has no value.
    spy = _by_label(eligibility_checklist(_ctx(BREAKOUT_ROW), "SPY", UNIVERSE))
    kind = spy["Common stock (not an ETF or fund)"]
    assert (kind.passed, kind.value, kind.threshold) == (False, "etf", "common")
    rs = spy["Stronger than 70% of US stocks (relative strength)"]
    assert (rs.passed, rs.value) == (False, "—")
    assert spy["Price at least $10"].passed and spy["Price at least $10"].value == "$467.52"

    # A downtrend stock that is eligible but not a leader: every leader rule fails, each with the
    # numbers that failed it.
    ccc = _by_label(eligibility_checklist(_ctx(BREAKOUT_ROW), "CCC", UNIVERSE))
    assert all(ccc[label].passed for label in LABELS[1:N_UNIVERSE])
    assert (ccc[LABELS[6]].passed, ccc[LABELS[6]].value, ccc[LABELS[6]].threshold) == (
        False,
        "$27.64",
        "$29.05",
    )
    assert (ccc[LABELS[7]].passed, ccc[LABELS[7]].value, ccc[LABELS[7]].threshold) == (
        False,
        "$29.05",
        "$33.96",
    )
    assert not ccc[LABELS[8]].passed
    assert (ccc[LABELS[9]].passed, ccc[LABELS[9]].value, ccc[LABELS[9]].threshold) == (
        False,
        "50.0%",
        "70.0%",
    )
    assert (ccc[LABELS[10]].passed, ccc[LABELS[10]].value, ccc[LABELS[10]].threshold) == (
        False,
        "39.5%",
        "25.0%",
    )


def test_checklist_names_the_single_rule_a_stock_fails():
    # Row 299: AAA is eligible, strong and near its high, but just closed under its 50-day average.
    items = eligibility_checklist(_ctx(BELOW_SMA50_ROW), "AAA", UNIVERSE)
    failed = [i for i in items[: N_UNIVERSE + N_LEADER] if not i.passed]
    assert [(i.rule, i.value, i.threshold) for i in failed] == [
        ("Close above 50-day average", "$112.73", "$113.44")
    ]
    # Its pullback rule passes (RSI is 1), but without the leader gate that is not an idea.
    assert items[13].passed


def test_checklist_for_a_symbol_with_no_bar_on_the_session():
    # DDD last printed on Wed 10 Feb, seven sessions before the scan.
    items = eligibility_checklist(_ctx(BREAKOUT_ROW), "DDD", UNIVERSE)
    assert (items[0].passed, items[0].value) == (False, "No, last bar Wed 10 Feb")
    for item in items[1:5]:  # price, dollar volume, daily range, history: nothing to read
        assert (item.passed, item.value) == (False, "—")
    assert items[5].passed  # it is still common stock
    assert not any(i.passed for i in items[6:])


def test_checklist_for_a_symbol_the_data_does_not_know():
    items = eligibility_checklist(_ctx(BREAKOUT_ROW), "ZZZ", UNIVERSE)
    assert [i.rule for i in items] == LABELS
    assert not any(i.passed for i in items)
    assert all(i.value == "—" for i in items)
    assert items[0].value == "—"


def test_checklist_tracks_the_universe_filter_in_force():
    ctx = _ctx(BREAKOUT_ROW)
    strict = dataclasses.replace(UNIVERSE, min_price=150.0, min_median_dollar_volume=500e6)
    items = _by_label(eligibility_checklist(ctx, "AAA", strict))
    price = items["Price at least $150"]
    assert (price.passed, price.threshold) == (False, "$150.00")
    volume = items["50-day median dollar volume at least $500M"]
    assert (volume.passed, volume.threshold) == (False, "$500.0M")
    # a fractional floor is not rounded in the label
    odd = eligibility_checklist(ctx, "AAA", dataclasses.replace(UNIVERSE, min_price=12.5))
    assert odd[1].rule == "Price at least $12.5"


def test_checklist_daily_range_rule_can_be_turned_off():
    ctx = _ctx(BREAKOUT_ROW)
    off = eligibility_checklist(ctx, "AAA", dataclasses.replace(UNIVERSE, min_atr_pct=0.0))
    assert off[3].passed and off[3].threshold == "not checked"


def test_checklist_follows_the_setup_parameters_it_is_given():
    ctx = _ctx(BREAKOUT_ROW)
    items = eligibility_checklist(
        ctx,
        "AAA",
        UNIVERSE,
        breakout=BreakoutParams(near_high_pct=0.005, contraction_max=0.30),
        pullback=PullbackParams(rsi2_max=30.0),
    )
    assert items[11].rule == "Breakout: close within 0.5% of 50-day closing high"
    assert not items[11].passed  # 1.0% under the high is outside 0.5%
    assert items[12].rule == "Breakout: quiet stretch (ATR10 / ATR50 at most 0.3)"
    assert not items[12].passed  # 0.42 is above 0.30
    assert items[13].rule == "Pullback: 2-day RSI at most 30 or close below 20-day average"
    assert items[13].passed  # RSI 23 is within 30


# ------------------------------------------------------------- the checklist agrees with the engine


def _variety() -> tuple[Panel, dict[str, AssetKind]]:
    """The seed-5 synthetic universe with stocks broken one rule at a time.

    ``S01`` trades under $10 (volume scaled up to keep its dollar volume), ``S02`` only in raw
    prices (its adjusted prices read normal, as after a later split), ``S03`` has no raw close on
    the scan day (the floor falls back to the adjusted close), ``S04`` is illiquid, ``S05`` has
    under 252 sessions of history, ``S06`` is halted on the scan day, ``S07`` is pinned to a
    price, ``S08`` reappears after a long gap (a reused ticker: a new segment of one bar),
    ``S09`` is a preferred share and ``S10`` missed one session ten rows before the scan day
    (it stays eligible, but a 20-row look-back no longer equals a 20-bar one).
    """
    base = synthetic_universe_panel(seed=5)
    names = ("open", "high", "low", "close", "volume", "raw_close")
    f = {name: getattr(base, name).copy() for name in names}
    scan_row = 450

    for name in ("open", "high", "low", "close", "raw_close"):
        f[name]["S01"] = f[name]["S01"] * 0.005
    f["volume"]["S01"] = f["volume"]["S01"] / 0.005
    f["raw_close"]["S02"] = f["raw_close"]["S02"] * 0.005
    f["raw_close"].loc[f["raw_close"].index[scan_row], "S03"] = np.nan
    f["volume"]["S04"] = f["volume"]["S04"] * 0.001
    for name in names:
        f[name].loc[f[name].index[:300], "S05"] = np.nan
        f[name].loc[f[name].index[scan_row], "S06"] = np.nan
        f[name].loc[f[name].index[200:scan_row], "S08"] = np.nan
        f[name].loc[f[name].index[scan_row - 10], "S10"] = np.nan
    wobble = 25.0 * (1.0 + 1e-5 * np.sin(np.arange(len(f["close"]))))
    pinned_open = np.concatenate([wobble[:1], wobble[:-1]])
    f["close"]["S07"] = wobble
    f["raw_close"]["S07"] = wobble
    f["open"]["S07"] = pinned_open
    f["high"]["S07"] = np.maximum(pinned_open, wobble) * 1.0002
    f["low"]["S07"] = np.minimum(pinned_open, wobble) * 0.9998
    f["volume"]["S07"] = 2e8 / 25.0

    panel = Panel(**f)
    kinds = synthetic_kinds(panel)
    kinds["S09"] = AssetKind.PREFERRED
    return panel, kinds


VARIETY, VARIETY_KINDS = _variety()


def _all_pass(items: list[CheckItem]) -> bool:
    return all(i.passed for i in items)


@pytest.mark.parametrize("row", [330, 450, 540])
def test_checklist_agrees_with_the_engine(row):
    """Every symbol, every rule: the checklist's ticks reproduce the engine's decisions."""
    panel, kinds = VARIETY, VARIETY_KINDS
    ctx = build_context(panel, kinds, panel.dates()[row].date())
    session = ctx.session
    breakout, pullback = BreakoutParams(), PullbackParams()
    found = {
        p.version: candidates_for(ctx.panel, ctx.ind, ctx.leaders, ctx.rs_pct, p)
        for p in (breakout, pullback)
    }
    on_session = {v: set(c.loc[c["date"] == session, "symbol"]) for v, c in found.items()}
    specs = [SetupSpec(breakout, live=False), SetupSpec(pullback, live=False)]
    result = run_scan(panel, kinds, session.date(), RiskSettings(), specs)
    ideas = [*result.ideas, *result.watch_only]

    failed_rules: set[int] = set()
    for symbol in panel.symbols():
        items = eligibility_checklist(ctx, symbol, UNIVERSE)
        assert [i.rule for i in items][1:] == LABELS[1:]
        assert items[0].rule.startswith("Has a bar for ")
        universe_ok = _all_pass(items[:N_UNIVERSE])
        leader_ok = universe_ok and _all_pass(items[N_UNIVERSE : N_UNIVERSE + N_LEADER])
        # eligible exactly when every universe rule passes (SPY is an ETF, so it fails one)
        assert universe_ok == bool(ctx.eligible.at[session, symbol]), symbol
        if not ctx.eligible.at[session, symbol]:
            failed_rules.update(i for i, item in enumerate(items[:N_UNIVERSE]) if not item.passed)
        # a leader exactly when the leader rules pass as well
        assert leader_ok == bool(ctx.leaders.at[session, symbol]), symbol
        if ctx.leaders.at[session, symbol]:
            assert _all_pass(items[N_UNIVERSE : N_UNIVERSE + N_LEADER]), symbol
        # a candidate exactly when a leader meets the setup's rules
        is_breakout = leader_ok and items[11].passed and items[12].passed
        assert is_breakout == (symbol in on_session[breakout.version]), symbol
        is_pullback = leader_ok and items[13].passed
        assert is_pullback == (symbol in on_session[pullback.version]), symbol

    for idea in ideas:
        items = eligibility_checklist(ctx, idea.symbol, UNIVERSE)
        assert _all_pass(items[: N_UNIVERSE + N_LEADER]), idea.symbol
        if idea.setup == "breakout":
            assert items[11].passed and items[12].passed, idea.symbol
        else:
            assert items[13].passed, idea.symbol

    if row == 450:
        # Not vacuous: both setups produced ideas, and each universe rule failed for somebody.
        assert {i.setup for i in ideas} == {"breakout", "pullback"}
        assert failed_rules == set(range(N_UNIVERSE))


def test_price_floor_reads_raw_close_where_present():
    """``S02`` reads normal in adjusted prices but under $10 as printed: the engine drops it."""
    panel, kinds = VARIETY, VARIETY_KINDS
    ctx = build_context(panel, kinds, panel.dates()[450].date())
    s02 = eligibility_checklist(ctx, "S02", UNIVERSE)
    assert not ctx.eligible.at[ctx.session, "S02"]
    assert not s02[1].passed
    assert float(s02[1].value.lstrip("$")) < 10.0
    # no raw close that day: the adjusted close stands in, as in the engine
    assert ctx.panel.raw_close is not None
    assert np.isnan(ctx.panel.raw_close.at[ctx.session, "S03"])
    s03 = eligibility_checklist(ctx, "S03", UNIVERSE)
    assert s03[1].passed
    assert s03[1].value == f"${ctx.panel.close.at[ctx.session, 'S03']:,.2f}"


def test_history_restarts_after_a_long_gap():
    """A reused ticker (``S08``) has one session of history, not the old company's 200."""
    panel, kinds = VARIETY, VARIETY_KINDS
    ctx = build_context(panel, kinds, panel.dates()[450].date())
    items = eligibility_checklist(ctx, "S08", UNIVERSE)
    assert (items[4].passed, items[4].value) == (False, "1")


def test_sma200_lookback_counts_the_symbols_own_bars():
    """``S10`` missed a session inside the look-back: "20 sessions ago" is its 20th earlier bar."""
    panel, kinds = VARIETY, VARIETY_KINDS
    ctx = build_context(panel, kinds, panel.dates()[450].date())
    sma200 = ctx.ind.sma200["S10"].dropna()
    assert sma200.index[-1] == ctx.session
    items = eligibility_checklist(ctx, "S10", UNIVERSE)
    assert items[8].threshold == f"${sma200.iloc[-21]:,.2f}"
    assert items[8].threshold != f"${ctx.ind.sma200['S10'].iloc[-21]:,.2f}"  # not 20 panel rows
