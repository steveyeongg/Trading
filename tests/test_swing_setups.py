"""Swing engine: relative strength, the leader gate, and breakout / pullback candidates.

Every rule is checked on small hand-built cases: a real panel and real ``Indicators`` are
computed, then specific cells on the last session are overwritten so exactly one condition at
a time is satisfied or broken. A last test runs the whole pipeline on a truncated panel to
prove a candidate on date d never depends on later rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from swing_engine.config import BreakoutParams, LeaderParams, PullbackParams, UniverseFilter
from swing_engine.data.assets import AssetKind
from swing_engine.indicators import Indicators, compute_indicators
from swing_engine.leaders import leader_mask, rs_percentile, rs_score
from swing_engine.panel import Panel, select, truncate
from swing_engine.setups import (
    CANDIDATE_COLUMNS,
    breakout_candidates,
    candidates_for,
    pullback_candidates,
)
from swing_engine.testing import make_panel, trend_series
from swing_engine.universe import eligibility

N = 300  # long enough for every indicator (SMA200 and the 20-bar lag on it) to be warm


def _set(frame: pd.DataFrame, value: float, symbol: str = "AAA", row: int = -1) -> None:
    """Overwrite one cell: ``row`` counts from the panel's own rows (-1 is the last session)."""
    frame.iloc[row, frame.columns.get_loc(symbol)] = value


def _flags(p: Panel, last: bool) -> pd.DataFrame:
    """A bool frame that is ``last`` on the final session and False everywhere else."""
    out = pd.DataFrame(False, index=p.close.index, columns=p.close.columns)
    out.iloc[-1] = last
    return out


def _rs(p: Panel, last: float) -> pd.DataFrame:
    """A percentile frame that is ``last`` on the final session and NaN everywhere else."""
    out = pd.DataFrame(np.nan, index=p.close.index, columns=p.close.columns)
    out.iloc[-1] = last
    return out


# --------------------------------------------------------------------------- relative strength


def test_rs_score_weights_and_horizons():
    closes = trend_series(N, seed=5)
    score = rs_score(make_panel({"AAA": closes}).close)["AAA"]
    c = np.asarray(closes)

    def r(n: int) -> float:
        return c[-1] / c[-1 - n] - 1.0

    assert score.iloc[-1] == pytest.approx(0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252))
    # Equal weights would give a different number, so the 0.4 on the latest quarter is pinned.
    assert score.iloc[-1] != pytest.approx(0.25 * (r(63) + r(126) + r(189) + r(252)))


def test_rs_score_is_nan_until_252_prior_bars_exist():
    score = rs_score(make_panel({"AAA": trend_series(N, seed=5)}).close)["AAA"]
    assert score.iloc[:252].isna().all()
    assert score.iloc[252:].notna().all()


def test_rs_score_counts_the_symbols_own_bars_not_panel_rows():
    closes = trend_series(320, seed=2)
    p = make_panel({"AAA": closes})
    _set(p.close, np.nan, row=150)  # one halted session inside the 252-bar lookback
    own = np.delete(np.asarray(closes), 150)  # the symbol's bars: 319 of them

    def r(n: int) -> float:
        return own[-1] / own[-1 - n] - 1.0

    expected = 0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)
    by_rows = 0.4 * (closes[-1] / closes[-64] - 1) + 0.2 * (closes[-1] / closes[-127] - 1)
    by_rows += 0.2 * (closes[-1] / closes[-190] - 1) + 0.2 * (closes[-1] / closes[-253] - 1)
    got = rs_score(p.close)["AAA"]
    assert got.iloc[-1] == pytest.approx(expected)
    assert got.iloc[-1] != pytest.approx(by_rows)
    assert np.isnan(got.iloc[150])  # no bar, no score: nothing is filled


def test_rs_score_restarts_after_a_long_gap():
    p = make_panel({"AAA": trend_series(400, seed=1)})
    p.close.iloc[200:216, 0] = np.nan  # 16 missing sessions: more than the 10-session limit
    score = rs_score(p.close)["AAA"]
    assert score.iloc[:200].isna().all()  # 200 bars is still short of 253
    assert score.iloc[216:].isna().all()  # the new segment has 184 bars and the old one is gone


def test_rs_percentile_ignores_ineligible():
    idx = pd.bdate_range("2024-01-02", periods=2)
    score = pd.DataFrame(
        {
            "A": [0.10, 0.50],
            "B": [0.20, 0.40],
            "C": [0.90, 0.10],  # the highest on day 1 but not eligible on it
            "D": [0.30, 0.30],
            "E": [0.40, 0.20],
        },
        index=idx,
    )
    eligible = pd.DataFrame(True, index=idx, columns=score.columns)
    eligible.loc[idx[0], "C"] = False
    pct = rs_percentile(score, eligible)
    # Day 1: C is out, so A<B<D<E rank 25/50/75/100 and the 0.90 does not push anyone down.
    assert pct.loc[idx[0], ["A", "B", "D", "E"]].tolist() == [25.0, 50.0, 75.0, 100.0]
    assert np.isnan(pct.loc[idx[0], "C"])
    # Day 2: everyone is eligible, five ranks.
    assert pct.loc[idx[1]].tolist() == [100.0, 80.0, 20.0, 60.0, 40.0]


def test_rs_percentile_nan_score_is_nan_and_leaves_the_ranking_alone():
    idx = pd.bdate_range("2024-01-02", periods=1)
    score = pd.DataFrame({"A": [0.1], "B": [np.nan], "C": [0.3]}, index=idx)
    pct = rs_percentile(score, pd.DataFrame(True, index=idx, columns=score.columns))
    assert pct.loc[idx[0], "A"] == 50.0 and pct.loc[idx[0], "C"] == 100.0
    assert np.isnan(pct.loc[idx[0], "B"])


def test_rs_percentile_ties_share_the_average_rank():
    idx = pd.bdate_range("2024-01-02", periods=1)
    score = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [2.0], "D": [3.0]}, index=idx)
    pct = rs_percentile(score, pd.DataFrame(True, index=idx, columns=score.columns))
    assert pct.iloc[0].tolist() == [25.0, 62.5, 62.5, 100.0]


def test_rs_percentile_date_with_no_eligible_symbol_is_all_nan():
    idx = pd.bdate_range("2024-01-02", periods=1)
    score = pd.DataFrame({"A": [0.1], "B": [0.2]}, index=idx)
    pct = rs_percentile(score, pd.DataFrame(False, index=idx, columns=score.columns))
    assert pct.isna().all().all() and pct.shape == score.shape


# ------------------------------------------------------------------------------- leader gate


def _leader_case() -> tuple[pd.DataFrame, Indicators, pd.DataFrame, pd.DataFrame]:
    """A symbol that clears every leader test on the last session, with room to spare.

    close 150 > sma50 140 > sma200 120; sma200 was 110 twenty bars ago; rs 90; and the close is
    within 25% of the 252-session high (160).
    """
    p = make_panel({"AAA": trend_series(N, seed=1)})
    ind = compute_indicators(p)
    close = p.close.copy()
    _set(close, 150.0)
    _set(ind.sma50, 140.0)
    _set(ind.sma200, 120.0)
    _set(ind.sma200, 110.0, row=-21)
    _set(ind.close_max252, 160.0)
    eligible = pd.DataFrame(True, index=p.close.index, columns=p.close.columns)
    rs = pd.DataFrame(90.0, index=p.close.index, columns=p.close.columns)
    return close, ind, rs, eligible


def _last(close, ind, rs, eligible, p: LeaderParams | None = None) -> bool:
    mask = leader_mask(close, ind, rs, eligible, p or LeaderParams())
    return bool(mask["AAA"].iloc[-1])


def test_leader_when_every_condition_holds():
    close, ind, rs, eligible = _leader_case()
    mask = leader_mask(close, ind, rs, eligible, LeaderParams())
    assert mask["AAA"].iloc[-1]
    assert all(dt == np.dtype("bool") for dt in mask.dtypes)
    assert mask.index.equals(close.index) and list(mask.columns) == list(close.columns)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("not eligible", lambda c, i, r, e: _set(e, False)),
        ("close equals sma50 (strict)", lambda c, i, r, e: _set(i.sma50, 150.0)),
        ("close below sma50", lambda c, i, r, e: _set(i.sma50, 150.01)),
        ("sma50 equals sma200 (strict)", lambda c, i, r, e: _set(i.sma50, 120.0)),
        ("sma50 below sma200", lambda c, i, r, e: _set(i.sma50, 119.0)),
        ("sma200 flat over 20 bars (strict)", lambda c, i, r, e: _set(i.sma200, 120.0, row=-21)),
        ("sma200 falling over 20 bars", lambda c, i, r, e: _set(i.sma200, 130.0, row=-21)),
        ("rs just under 70", lambda c, i, r, e: _set(r, 69.99)),
        ("close under 75% of the 252-high", lambda c, i, r, e: _set(i.close_max252, 200.01)),
    ],
)
def test_each_condition_alone_breaks_the_leader(name, mutate):
    case = _leader_case()
    assert _last(*case)
    mutate(*case)
    assert not _last(*case), name


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("rs exactly 70", lambda c, i, r, e: _set(r, 70.0)),
        ("close exactly 75% of the 252-high", lambda c, i, r, e: _set(i.close_max252, 200.0)),
        (
            "sma200 a hair above its 20-bars-ago value",
            lambda c, i, r, e: _set(i.sma200, 119.99, row=-21),
        ),
    ],
)
def test_inclusive_boundaries_still_pass(name, mutate):
    case = _leader_case()
    mutate(*case)
    assert _last(*case), name


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c, i, r, e: _set(c, np.nan),
        lambda c, i, r, e: _set(i.sma50, np.nan),
        lambda c, i, r, e: _set(i.sma200, np.nan),
        lambda c, i, r, e: _set(i.close_max252, np.nan),
        lambda c, i, r, e: _set(r, np.nan),
    ],
    ids=["close", "sma50", "sma200", "close_max252", "rs_pct"],
)
def test_nan_in_any_input_is_never_a_leader(mutate):
    case = _leader_case()
    mutate(*case)
    assert not _last(*case)


def test_sma200_with_too_few_bars_to_look_back_is_never_a_leader():
    close, ind, rs, eligible = _leader_case()
    ind.sma200.iloc[:-20, 0] = np.nan  # SMA200 warm for only the last 20 bars: no value 20 ago
    assert not _last(close, ind, rs, eligible)
    ind.sma200.iloc[-21, 0] = 110.0  # one more warm bar and the look-back exists again
    assert _last(close, ind, rs, eligible)


def test_leader_params_are_honoured():
    case = _leader_case()
    assert not _last(*case, LeaderParams(rs_min_pct=95.0))  # rs is 90
    assert not _last(*case, LeaderParams(within_high_pct=0.05))  # 150 < 0.95 * 160
    assert _last(*case, LeaderParams(within_high_pct=0.07))  # 150 >= 0.93 * 160 = 148.8
    close, ind, rs, eligible = case
    _set(ind.sma200, 130.0, row=-6)  # falling over 5 bars, 110 -> 120 over the default 20
    assert not _last(close, ind, rs, eligible, LeaderParams(sma200_rising_lookback=5))
    assert _last(close, ind, rs, eligible, LeaderParams(sma200_rising_lookback=20))


def test_sma200_lookback_counts_the_symbols_bars_not_panel_rows():
    p = make_panel({"AAA": trend_series(N, seed=1)})
    _set(p.close, np.nan, row=290)  # a halted session inside the last 20 bars
    ind = compute_indicators(p)
    close = p.close.copy()
    _set(close, 150.0)
    _set(ind.sma50, 140.0)
    _set(ind.sma200, 120.0)
    _set(ind.close_max252, 160.0)
    # 20 bars before row 299 is row 278 (row 290 has no bar). 279 is 20 *rows* back.
    _set(ind.sma200, 110.0, row=278)
    _set(ind.sma200, 1e9, row=279)
    eligible = pd.DataFrame(True, index=p.close.index, columns=p.close.columns)
    rs = pd.DataFrame(90.0, index=p.close.index, columns=p.close.columns)
    assert _last(close, ind, rs, eligible)


def test_sma200_lookback_never_reaches_across_a_segment_break():
    p = make_panel({"AAA": trend_series(520, seed=1)})
    p.close.iloc[260:280, 0] = np.nan  # 20 missing sessions: the old company ends at row 259
    ind = compute_indicators(p)
    # The new segment's SMA200 is first warm at row 479, with no value 20 bars earlier until
    # row 499. Make everything else pass and the old company's SMA200 tiny, so a lag that
    # reached back into it would read "rising" on rows 479 to 498.
    close = p.close.copy()
    close.iloc[479:] = 1e6
    ind.sma50.iloc[479:] = 5e5
    ind.close_max252.iloc[479:] = 1e6
    ind.sma200.iloc[199:260] = 1.0
    assert ind.sma200.iloc[479:499].notna().all().all()
    eligible = pd.DataFrame(True, index=p.close.index, columns=p.close.columns)
    rs = pd.DataFrame(90.0, index=p.close.index, columns=p.close.columns)
    mask = leader_mask(close, ind, rs, eligible, LeaderParams())["AAA"]
    assert not mask.iloc[479:499].any()


# ------------------------------------------------------------------------------- breakouts


def _case(symbols: tuple[str, ...] = ("AAA",)):
    """A panel, its indicators, leaders True on the last session only, and rs 85 there."""
    p = make_panel({s: trend_series(N, drift=0.002, seed=i) for i, s in enumerate(symbols)})
    return p, compute_indicators(p), _flags(p, True), _rs(p, 85.0)


def _breakout_case(close: float = 99.0, **cells: float):
    """Last session of AAA: close 99 within 2% of the 50-close high (100); atr10/atr50 = 0.8."""
    p, ind, leaders, rs = _case()
    values = {
        "close_max50": 100.0,
        "high_max50": 101.0,
        "atr10": 0.8,
        "atr50": 1.0,
        "atr14": 1.5,
    } | cells
    _set(p.close, close)
    for name, value in values.items():
        _set(getattr(ind, name), value)
    return p, ind, leaders, rs


def test_breakout_trigger_and_stop_values():
    p, ind, leaders, rs = _breakout_case()
    c = breakout_candidates(p, ind, leaders, rs, BreakoutParams())
    assert list(c.columns) == CANDIDATE_COLUMNS and len(c) == 1
    row = c.iloc[0]
    assert row.date == p.dates()[-1] and row.symbol == "AAA"
    assert row.setup == "breakout" and row.version == "breakout-v1"
    assert row.trigger == pytest.approx(101.0 + 0.1 * 1.5)
    assert row.trigger - row.stop == pytest.approx(2.0 * 1.5)
    assert row.stop == pytest.approx(98.15)
    assert row.atr14 == 1.5 and row.rs_pct == 85.0


def test_breakout_uses_the_params_it_is_given():
    p, ind, leaders, rs = _breakout_case()
    params = BreakoutParams(version="breakout-v2", trigger_atr=0.5, stop_atr=1.0)
    row = breakout_candidates(p, ind, leaders, rs, params).iloc[0]
    assert row.version == "breakout-v2"
    assert row.trigger == pytest.approx(101.0 + 0.5 * 1.5)
    assert row.trigger - row.stop == pytest.approx(1.5)


def test_breakout_requires_contraction():
    p, ind, leaders, rs = _breakout_case(atr10=0.9)  # 0.9 / 1.0
    assert breakout_candidates(p, ind, leaders, rs, BreakoutParams()).empty


def test_breakout_contraction_boundary_is_inclusive():
    exact = breakout_candidates(*_breakout_case(atr10=0.85), BreakoutParams())
    over = breakout_candidates(*_breakout_case(atr10=0.8501), BreakoutParams())
    assert len(exact) == 1 and over.empty


def test_breakout_near_high_boundary_is_inclusive():
    exact = breakout_candidates(*_breakout_case(close=98.0), BreakoutParams())  # 0.98 * 100
    under = breakout_candidates(*_breakout_case(close=97.99), BreakoutParams())
    assert len(exact) == 1 and under.empty


def test_breakout_needs_a_leader():
    p, ind, leaders, rs = _breakout_case()
    assert breakout_candidates(p, ind, leaders & False, rs, BreakoutParams()).empty


@pytest.mark.parametrize("field", ["close_max50", "high_max50", "atr10", "atr50", "atr14"])
def test_breakout_nan_input_means_no_candidate(field):
    assert breakout_candidates(*_breakout_case(**{field: np.nan}), BreakoutParams()).empty


def test_breakout_nan_close_or_rs_means_no_candidate():
    p, ind, leaders, rs = _breakout_case(close=np.nan)
    assert breakout_candidates(p, ind, leaders, rs, BreakoutParams()).empty
    p, ind, leaders, rs = _breakout_case()
    _set(rs, np.nan)
    assert breakout_candidates(p, ind, leaders, rs, BreakoutParams()).empty


def test_breakout_zero_long_atr_never_makes_a_candidate():
    # atr10 / atr50 with atr50 == 0 is inf (or NaN for 0/0): not a contraction.
    assert breakout_candidates(*_breakout_case(atr50=0.0), BreakoutParams()).empty
    assert breakout_candidates(*_breakout_case(atr10=0.0, atr50=0.0), BreakoutParams()).empty


def test_breakout_rejects_a_window_the_indicators_do_not_carry():
    with pytest.raises(ValueError, match="high_window"):
        breakout_candidates(*_breakout_case(), BreakoutParams(high_window=20))


# ------------------------------------------------------------------------------- pullbacks


def _pullback_case(**cells: float):
    """Last session of AAA: rsi2 5, close 99 above ema20 98, high 101, atr14 2, low_min5 98."""
    p, ind, leaders, rs = _case()
    values = {"rsi2": 5.0, "ema20": 98.0, "atr14": 2.0, "low_min5": 98.0} | cells
    _set(p.close, 99.0)
    _set(p.high, 101.0)
    for name, value in values.items():
        _set(getattr(ind, name), value)
    return p, ind, leaders, rs


def test_pullback_trigger_and_stop_values():
    p, ind, leaders, rs = _pullback_case()
    c = pullback_candidates(p, ind, leaders, rs, PullbackParams())
    assert list(c.columns) == CANDIDATE_COLUMNS and len(c) == 1
    row = c.iloc[0]
    assert row.date == p.dates()[-1] and row.symbol == "AAA"
    assert row.setup == "pullback" and row.version == "pullback-v1"
    assert row.trigger == pytest.approx(101.0 + 0.05 * 2.0)  # the day's high, not high_max50
    assert row.stop == pytest.approx(98.0 - 0.25 * 2.0)  # low_min5 - 0.25 ATR, inside [1, 3] ATR
    assert row.atr14 == 2.0 and row.rs_pct == 85.0


@pytest.mark.parametrize(
    ("low_min5", "stop_distance_atr"),
    [
        (80.0, 3.0),  # far below: the stop is capped at 3 ATR under the trigger
        (100.5, 1.0),  # just under the trigger: floored at 1 ATR
        (95.6, 3.0),  # raw distance is exactly 3 ATR
        (99.6, 1.0),  # raw distance is exactly 1 ATR
        (98.0, 1.8),  # raw distance 3.6 = 1.8 ATR, left alone
    ],
)
def test_pullback_stop_clamped_between_1_and_3_atr(low_min5, stop_distance_atr):
    p, ind, leaders, rs = _pullback_case(low_min5=low_min5)
    row = pullback_candidates(p, ind, leaders, rs, PullbackParams()).iloc[0]
    assert row.trigger == pytest.approx(101.1)
    assert row.trigger - row.stop == pytest.approx(stop_distance_atr * 2.0)


def test_pullback_uses_the_params_it_is_given():
    p, ind, leaders, rs = _pullback_case(low_min5=80.0)
    params = PullbackParams(version="pullback-v2", trigger_atr=0.5, max_stop_atr=2.0)
    row = pullback_candidates(p, ind, leaders, rs, params).iloc[0]
    assert row.version == "pullback-v2"
    assert row.trigger == pytest.approx(101.0 + 0.5 * 2.0)
    assert row.trigger - row.stop == pytest.approx(2.0 * 2.0)
    params = PullbackParams(stop_buffer_atr=1.0, min_stop_atr=0.5)
    p, ind, leaders, rs = _pullback_case(low_min5=98.0)
    row = pullback_candidates(p, ind, leaders, rs, params).iloc[0]
    assert row.stop == pytest.approx(98.0 - 1.0 * 2.0)  # raw 96, distance 5.1 within [1, 6]


def test_pullback_oversold_by_rsi2_alone():
    # close 99 > ema20 98, so only the RSI(2) leg is live; the boundary is inclusive.
    assert len(pullback_candidates(*_pullback_case(rsi2=10.0), PullbackParams())) == 1
    assert pullback_candidates(*_pullback_case(rsi2=10.01), PullbackParams()).empty


def test_pullback_below_ema20_alone():
    # rsi2 50 is not oversold, so only the EMA leg is live; "close <= ema20" is inclusive.
    assert len(pullback_candidates(*_pullback_case(rsi2=50.0, ema20=99.0), PullbackParams())) == 1
    assert len(pullback_candidates(*_pullback_case(rsi2=50.0, ema20=99.5), PullbackParams())) == 1
    assert pullback_candidates(*_pullback_case(rsi2=50.0, ema20=98.99), PullbackParams()).empty


def test_pullback_needs_a_leader():
    p, ind, leaders, rs = _pullback_case()
    assert pullback_candidates(p, ind, leaders & False, rs, PullbackParams()).empty


@pytest.mark.parametrize("field", ["rsi2", "ema20", "atr14", "low_min5"])
def test_pullback_nan_input_means_no_candidate(field):
    # Even when the other leg of the OR is true (rsi2 5 / close below ema20 99.5).
    case = _pullback_case(**{"ema20": 99.5} | {field: np.nan})
    assert pullback_candidates(*case, PullbackParams()).empty


def test_pullback_nan_high_close_or_rs_means_no_candidate():
    p, ind, leaders, rs = _pullback_case()
    _set(p.high, np.nan)
    assert pullback_candidates(p, ind, leaders, rs, PullbackParams()).empty
    p, ind, leaders, rs = _pullback_case()
    _set(rs, np.nan)
    assert pullback_candidates(p, ind, leaders, rs, PullbackParams()).empty


def test_pullback_rejects_windows_the_indicators_do_not_carry():
    with pytest.raises(ValueError, match="ema_window"):
        pullback_candidates(*_pullback_case(), PullbackParams(ema_window=10))
    with pytest.raises(ValueError, match="low_window"):
        pullback_candidates(*_pullback_case(), PullbackParams(low_window=3))


# ------------------------------------------------------------- non-leaders and the frame shape


def _both_setups_live_case(rs_last: float):
    """Last session where AAA is a leader (given rs) and satisfies the breakout and pullback."""
    p, ind, _, _ = _case()
    _set(p.close, 99.0)
    _set(p.high, 101.0)
    for name, value in {
        "sma50": 90.0,
        "sma200": 80.0,
        "close_max252": 100.0,
        "close_max50": 100.0,
        "high_max50": 101.0,
        "atr10": 0.8,
        "atr50": 1.0,
        "atr14": 1.5,
        "rsi2": 5.0,
        "low_min5": 98.0,
    }.items():
        _set(getattr(ind, name), value)
    _set(ind.sma200, 70.0, row=-21)
    eligible = _flags(p, True)
    rs = _rs(p, rs_last)
    leaders = leader_mask(p.close, ind, rs, eligible, LeaderParams())
    return p, ind, leaders, rs


def test_leader_gets_both_setups_and_non_leader_gets_neither():
    p, ind, leaders, rs = _both_setups_live_case(85.0)
    assert len(breakout_candidates(p, ind, leaders, rs, BreakoutParams())) == 1
    assert len(pullback_candidates(p, ind, leaders, rs, PullbackParams())) == 1
    p, ind, leaders, rs = _both_setups_live_case(60.0)
    assert not leaders.any().any()
    assert breakout_candidates(p, ind, leaders, rs, BreakoutParams()).empty
    assert pullback_candidates(p, ind, leaders, rs, PullbackParams()).empty


def test_candidates_for_dispatches_on_the_params_type():
    p, ind, leaders, rs = _both_setups_live_case(85.0)
    b, pb = BreakoutParams(), PullbackParams()
    pd.testing.assert_frame_equal(
        candidates_for(p, ind, leaders, rs, b), breakout_candidates(p, ind, leaders, rs, b)
    )
    pd.testing.assert_frame_equal(
        candidates_for(p, ind, leaders, rs, pb), pullback_candidates(p, ind, leaders, rs, pb)
    )
    assert candidates_for(p, ind, leaders, rs, b).setup.tolist() == ["breakout"]
    assert candidates_for(p, ind, leaders, rs, pb).setup.tolist() == ["pullback"]
    with pytest.raises(TypeError):
        candidates_for(p, ind, leaders, rs, LeaderParams())  # type: ignore[arg-type]


def test_candidate_columns_are_pinned():
    assert CANDIDATE_COLUMNS == [
        "date",
        "symbol",
        "setup",
        "version",
        "trigger",
        "stop",
        "atr14",
        "rs_pct",
    ]


def test_empty_result_is_an_empty_frame_with_the_candidate_columns():
    p, ind, leaders, rs = _breakout_case()
    for params in (BreakoutParams(), PullbackParams()):
        out = candidates_for(p, ind, leaders & False, rs, params)
        assert out.empty and list(out.columns) == CANDIDATE_COLUMNS
        assert isinstance(out.index, pd.RangeIndex)
        assert out["date"].dtype == np.dtype("datetime64[ns]")
        assert out["trigger"].dtype == np.dtype("float64")


def _three_symbol_breakouts() -> tuple[Panel, Indicators, pd.DataFrame, pd.DataFrame]:
    """Columns deliberately not alphabetical; every symbol breaks out on the last two sessions."""
    order = ["ZZZ", "AAA", "MMM"]
    p = select(make_panel({s: trend_series(N, seed=i) for i, s in enumerate(order)}), order)
    ind = compute_indicators(p)
    p.close.iloc[-2:] = 99.0
    for name, value in {
        "close_max50": 100.0,
        "high_max50": 101.0,
        "atr10": 0.8,
        "atr50": 1.0,
        "atr14": 1.5,
    }.items():
        getattr(ind, name).iloc[-2:] = value
    leaders = pd.DataFrame(False, index=p.close.index, columns=p.close.columns)
    leaders.iloc[-2:] = True
    rs = pd.DataFrame(80.0, index=p.close.index, columns=p.close.columns)
    return p, ind, leaders, rs


def test_candidates_are_sorted_by_date_then_symbol_with_a_range_index():
    p, ind, leaders, rs = _three_symbol_breakouts()
    assert p.symbols() == ["ZZZ", "AAA", "MMM"]
    out = breakout_candidates(p, ind, leaders, rs, BreakoutParams())
    dates = p.dates()[-2:]
    assert out["symbol"].tolist() == ["AAA", "MMM", "ZZZ", "AAA", "MMM", "ZZZ"]
    assert out["date"].tolist() == [dates[0]] * 3 + [dates[1]] * 3
    assert isinstance(out.index, pd.RangeIndex) and out.index.tolist() == list(range(6))
    assert out["date"].dt.tz is None


def test_leader_and_rs_frames_are_matched_by_label_not_position():
    p, ind, leaders, rs = _three_symbol_breakouts()
    leaders.loc[:, "MMM"] = False  # only ZZZ and AAA are leaders
    want = breakout_candidates(p, ind, leaders, rs, BreakoutParams())
    assert sorted(set(want["symbol"])) == ["AAA", "ZZZ"]
    flipped = breakout_candidates(
        p,
        ind,
        leaders[["MMM", "AAA", "ZZZ"]].iloc[::-1],
        rs[["AAA", "ZZZ", "MMM"]].iloc[::-1],
        BreakoutParams(),
    )
    pd.testing.assert_frame_equal(flipped, want)


# ----------------------------------------------------------------------------- no look-ahead


def _pipeline(p: Panel, kinds: dict[str, AssetKind]):
    ind = compute_indicators(p)
    eligible = eligibility(p, kinds, UniverseFilter())
    rs = rs_percentile(rs_score(p.close), eligible)
    leaders = leader_mask(p.close, ind, rs, eligible, LeaderParams())
    return ind, rs, leaders


def _world(n: int = 460) -> Panel:
    """Fourteen steady climbers (strong to flat) whose daily ranges swing from wide to narrow.

    A smooth climb keeps the close near its high, and the wide-then-narrow ranges make
    atr10 / atr50 dip below 0.85, so both setups fire on many dates for the strongest names.
    """
    series = {}
    for i in range(14):
        noise = 0.004 if i % 2 == 0 else 0.012  # odd names are choppier, so they pull back
        rets = 0.004 - 0.0003 * i + noise * np.random.default_rng(i).standard_normal(n)
        series[f"S{i:02d}"] = (40.0 * np.cumprod(1.0 + rets)).tolist()
    p = make_panel(series)
    phase = (np.arange(n)[:, None] + 13 * np.arange(14)[None, :]) % 90
    spread = np.where(phase < 55, 0.03, 0.004)
    p.high[:] = np.maximum(p.open, p.close).to_numpy() * (1.0 + spread)
    p.low[:] = np.minimum(p.open, p.close).to_numpy() * (1.0 - spread)
    return p


def test_candidates_on_a_date_do_not_depend_on_later_rows():
    p = _world()
    kinds = dict.fromkeys(p.symbols(), AssetKind.COMMON)
    ind, rs, leaders = _pipeline(p, kinds)
    for params in (BreakoutParams(), PullbackParams()):
        full = candidates_for(p, ind, leaders, rs, params)
        assert len(full) > 20, "the world must produce candidates or this test proves nothing"
        # Cut the panel on dates that have candidates, so the cut-day row itself is compared.
        days = full["date"].drop_duplicates().to_list()
        cuts = [days[i] for i in np.linspace(0, len(days) - 1, 8).astype(int)]
        for as_of in cuts:
            short = truncate(p, as_of)
            ind_s, rs_s, leaders_s = _pipeline(short, kinds)
            got = candidates_for(short, ind_s, leaders_s, rs_s, params)
            want = full[full["date"] <= as_of].reset_index(drop=True)
            assert (want["date"] == as_of).any()
            pd.testing.assert_frame_equal(got, want)


def test_rs_percentile_on_a_date_does_not_depend_on_later_rows():
    p = _world()
    kinds = dict.fromkeys(p.symbols(), AssetKind.COMMON)
    _, rs, _ = _pipeline(p, kinds)
    short = truncate(p, p.dates()[380])
    _, rs_s, _ = _pipeline(short, kinds)
    pd.testing.assert_frame_equal(rs_s, rs.loc[: p.dates()[380]])
    assert rs_s.iloc[-1].notna().any()
