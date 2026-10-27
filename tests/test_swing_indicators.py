"""Swing engine: config defaults, the daily panel, and strict per-segment indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest
from swing_engine.config import (
    REGIME_FACTOR,
    BreakoutParams,
    PullbackParams,
    RiskSettings,
    UniverseFilter,
)
from swing_engine.indicators import atr, compute_indicators, rsi, sma, true_range
from swing_engine.panel import build_panel, concat_panels, segments, select, truncate
from swing_engine.testing import make_bars, make_panel, trend_series


def test_defaults_match_spec():
    assert UniverseFilter().min_median_dollar_volume == 20_000_000.0
    assert BreakoutParams().stop_atr == 2.0 and BreakoutParams().trail_atr == 3.0
    assert PullbackParams().rsi2_max == 10.0 and PullbackParams().max_hold == 30
    assert RiskSettings().risk_pct == 0.01 and RiskSettings().max_positions == 8
    assert REGIME_FACTOR == {"uptrend": 1.0, "mixed": 0.5, "downtrend": 0.0}


def test_sma_is_strict_about_warmup():
    p = make_panel({"AAA": list(range(1, 61))})
    s = sma(p.close, 50)["AAA"]
    assert s.iloc[:49].isna().all() and s.iloc[49] == pytest.approx(25.5)


def test_atr_and_rsi_match_pandas_ta_after_warmup():
    p = make_panel({"AAA": trend_series(400, seed=3)})
    ours = atr(p, 14)["AAA"].iloc[-100:]
    ref = ta.atr(p.high["AAA"], p.low["AAA"], p.close["AAA"], length=14).iloc[-100:]
    assert np.allclose(ours, ref, rtol=1e-4)
    assert np.allclose(
        rsi(p.close, 14)["AAA"].iloc[-100:], ta.rsi(p.close["AAA"], 14).iloc[-100:], rtol=1e-4
    )


def test_gap_over_ten_sessions_restarts_indicators():
    a = make_bars("AAA", [100.0] * 60)  # old company
    b = make_bars("AAA", [10.0] * 60, start="2020-06-01")  # reused ticker, > 10-session gap
    spy = make_bars("SPY", trend_series(200))
    p = build_panel(pd.concat([a, b, spy]))
    s = sma(p.close, 50)["AAA"]
    first_new = pd.Timestamp(b["ts"].iloc[0]).tz_convert(None)
    assert s.loc[first_new:].dropna().iloc[0] == pytest.approx(10.0)  # no blend of 100s and 10s
    assert s.loc[first_new:].iloc[:49].isna().all()


def test_true_range_does_not_use_prior_company_close_across_gap():
    a = make_bars("AAA", [100.0] * 30)
    b = make_bars("AAA", [10.0] * 30, start="2020-06-01")
    spy = make_bars("SPY", trend_series(200))
    p = build_panel(pd.concat([a, b, spy]))
    first_new = pd.Timestamp(b["ts"].iloc[0]).tz_convert(None)
    tr = true_range(p)["AAA"]
    assert tr.loc[first_new] == pytest.approx(
        p.high["AAA"].loc[first_new] - p.low["AAA"].loc[first_new]
    )
    assert tr.loc[first_new:].dropna().max() < 1.0  # a 100 -> 10 "crash" never enters TR


def test_truncate_drops_future_rows():
    p = make_panel({"AAA": trend_series(30)})
    t = truncate(p, p.dates()[9])
    assert len(t.close) == 10 and t.close.index[-1] == p.dates()[9]


def test_make_bars_ts_is_utc_midnight_and_open_is_prior_close():
    b = make_bars("AAA", [10.0, 11.0, 12.0], start="2020-01-02")
    assert str(b["ts"].dt.tz) == "UTC"
    assert (b["ts"].dt.hour == 0).all()
    assert b["open"].tolist() == [10.0, 10.0, 11.0]
    assert (b["high"] >= b[["open", "close"]].max(axis=1)).all()
    assert (b["low"] <= b[["open", "close"]].min(axis=1)).all()


def test_build_panel_index_is_tz_naive_session_dates():
    p = make_panel({"AAA": [1.0, 2.0, 3.0], "BBB": [4.0, 5.0, 6.0]})
    assert p.close.index.tz is None
    assert p.dates().equals(p.close.index.normalize())
    assert p.symbols() == ["AAA", "BBB"]
    assert p.close.index.equals(p.volume.index)


def _off_calendar_bars() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """AAA on Thu 2 to Tue 7 Jan 2020 plus a stray Saturday bar; SPY on Thu 2 to Mon 6 only."""
    aaa = make_bars("AAA", [10.0, 11.0, 12.0, 13.0], start="2020-01-02")
    stray = aaa.iloc[[0]].assign(ts=pd.Timestamp("2020-01-04", tz="UTC"))
    spy = make_bars("SPY", [300.0, 301.0, 302.0], start="2020-01-02")
    return aaa, stray, spy


def test_build_panel_keeps_only_the_sessions_spy_traded():
    """The incident: a bar dated off SPY's calendar (a stray holiday or weekend print, or a stock
    updated before SPY) added a panel row on which every other symbol was missing. That reads as
    a missed session for all of them and shifts every rolling window by a row."""
    aaa, stray, spy = _off_calendar_bars()
    p = build_panel(pd.concat([aaa, stray, spy]), raw_bars=pd.concat([aaa, stray]))
    assert list(p.dates()) == list(pd.bdate_range("2020-01-02", periods=3))
    assert p.close["AAA"].tolist() == [10.0, 11.0, 12.0]
    assert p.raw_close is not None and p.raw_close.index.equals(p.close.index)
    assert len(build_panel(pd.concat([aaa, stray])).dates()) == 5  # no SPY: every date is kept


def test_concat_panels_joins_symbol_batches_on_spy_sessions():
    aaa, stray, spy = _off_calendar_bars()
    bbb = make_bars("BBB", [50.0, 51.0], start="2020-01-03")
    whole = build_panel(pd.concat([aaa, stray, bbb, spy]), raw_bars=pd.concat([aaa, spy]))
    batches = [
        build_panel(pd.concat([aaa, stray]), raw_bars=aaa),
        build_panel(pd.concat([bbb, spy]), raw_bars=spy),
    ]
    joined = concat_panels(batches)
    for field in ("open", "high", "low", "close", "volume", "raw_close"):
        pd.testing.assert_frame_equal(getattr(joined, field), getattr(whole, field))
    with pytest.raises(ValueError, match="AAA"):
        concat_panels([batches[0], batches[0]])


def test_select_keeps_only_requested_symbols():
    p = make_panel({"AAA": [1.0, 2.0, 3.0], "BBB": [4.0, 5.0, 6.0]})
    s = select(p, ["BBB"])
    assert s.symbols() == ["BBB"]
    assert list(s.high.columns) == ["BBB"]


def test_segments_split_on_session_gap_not_calendar_days():
    idx = pd.bdate_range("2020-01-02", periods=40)
    vals = pd.Series(1.0, index=idx)
    vals.iloc[10:25] = np.nan  # 15 missing sessions between the values at rows 9 and 25
    parts = segments(vals, max_gap=10)
    assert [len(s) for s in parts] == [10, 15]
    # Exactly max_gap missing sessions is tolerated; one more starts a new segment.
    tolerated = pd.Series(1.0, index=idx)
    tolerated.iloc[10:20] = np.nan
    assert len(segments(tolerated, max_gap=10)) == 1
    split = pd.Series(1.0, index=idx)
    split.iloc[10:21] = np.nan
    assert len(segments(split, max_gap=10)) == 2


def test_segments_empty_series_has_no_segments():
    idx = pd.bdate_range("2020-01-02", periods=5)
    assert segments(pd.Series(np.nan, index=idx), max_gap=10) == []


def test_compute_indicators_shapes_and_warmup():
    p = make_panel({"AAA": trend_series(300, seed=1), "BBB": trend_series(120, seed=2)})
    ind = compute_indicators(p)
    for frame in (ind.sma50, ind.sma200, ind.ema20, ind.atr10, ind.atr14, ind.atr50, ind.rsi2):
        assert frame.index.equals(p.close.index) and list(frame.columns) == p.symbols()
    assert ind.sma200["AAA"].iloc[:199].isna().all() and pd.notna(ind.sma200["AAA"].iloc[199])
    assert ind.sma200["BBB"].isna().all()  # only 120 sessions
    assert ind.close_max252["AAA"].iloc[:251].isna().all()
    assert pd.notna(ind.close_max252["AAA"].iloc[251])
    assert ind.low_min5["AAA"].iloc[4] == pytest.approx(p.low["AAA"].iloc[:5].min())
    assert ind.high_max50["AAA"].iloc[-1] == pytest.approx(p.high["AAA"].iloc[-50:].max())
    assert ind.close_max50["AAA"].iloc[-1] == pytest.approx(p.close["AAA"].iloc[-50:].max())
