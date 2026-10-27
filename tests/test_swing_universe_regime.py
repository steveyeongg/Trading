"""Swing engine: point-in-time universe eligibility, SPY market regime and breadth."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest
import swing_engine.scan as scan_module
from swing_engine.config import UniverseFilter
from swing_engine.data.assets import TRADEABLE_KINDS, AssetKind
from swing_engine.indicators import MAX_GAP, atr, compute_indicators
from swing_engine.panel import Panel, truncate
from swing_engine.regime import Regime, breadth, market_regime
from swing_engine.scan import build_context
from swing_engine.testing import (
    make_panel,
    synthetic_kinds,
    synthetic_universe_panel,
    trend_series,
)
from swing_engine.universe import eligibility

COMMON = AssetKind.COMMON
_FIELDS = ("open", "high", "low", "close", "volume")


def _drop_bars(p: Panel, symbol: str, rows: slice) -> None:
    """Remove a symbol's bars on ``rows`` (every field), as a halt or late listing would."""
    for name in _FIELDS:
        frame = getattr(p, name)
        frame.iloc[rows, frame.columns.get_loc(symbol)] = np.nan


def test_missing_scan_day_is_not_eligible():
    p = make_panel({"AAA": [50.0] * 300, "SPY": trend_series(300)}, volume=1_000_000)
    p.close.loc[p.dates()[-1], "AAA"] = np.nan
    e = eligibility(p, {"AAA": AssetKind.COMMON}, UniverseFilter())
    assert not e["AAA"].iloc[-1] and e["AAA"].iloc[-2]


def test_missing_day_leaves_no_stale_price_but_eligibility_resumes_next_day():
    p = make_panel({"AAA": [50.0] * 300}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(280, 281))  # one halted session, well inside the gap limit
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"]
    assert e.iloc[279] and not e.iloc[280] and e.iloc[281]


def test_result_is_a_bool_frame_shaped_like_the_panel():
    p = make_panel({"AAA": [50.0] * 300, "BBB": [50.0] * 300}, volume=1_000_000)
    e = eligibility(p, {"AAA": COMMON, "BBB": COMMON}, UniverseFilter())
    assert e.index.equals(p.close.index) and list(e.columns) == p.symbols()
    assert all(dt == np.dtype("bool") for dt in e.dtypes)


def test_dollar_volume_and_price_floor():
    p = make_panel({"CHEAP": [9.99] * 300, "THIN": [50.0] * 300}, volume=300_000)  # THIN: $15M/day
    e = eligibility(p, {"CHEAP": COMMON, "THIN": COMMON}, UniverseFilter())
    assert not e.iloc[-1].any()


def test_price_floor_binds_on_its_own_and_is_inclusive():
    # Both clear the $20M dollar-volume bar; only the price differs.
    p = make_panel({"CHEAP": [9.99] * 300, "EXACT": [10.0] * 300}, volume=10_000_000)
    e = eligibility(p, {"CHEAP": COMMON, "EXACT": COMMON}, UniverseFilter())
    assert not e["CHEAP"].iloc[-1] and e["EXACT"].iloc[-1]


def test_dollar_volume_floor_binds_on_its_own_and_is_inclusive():
    # Price 50 is fine for both; $50 x 400,000 = exactly $20M, $50 x 399,999 is just under.
    p = make_panel({"EXACT": [50.0] * 300}, volume=400_000)
    q = make_panel({"UNDER": [50.0] * 300}, volume=399_999)
    assert eligibility(p, {"EXACT": COMMON}, UniverseFilter())["EXACT"].iloc[-1]
    assert not eligibility(q, {"UNDER": COMMON}, UniverseFilter())["UNDER"].iloc[-1]


def test_dollar_volume_is_a_median_so_a_few_spikes_do_not_qualify_a_thin_stock():
    p = make_panel({"AAA": [50.0] * 300}, volume=300_000)  # $15M/day, below the bar
    p.volume.iloc[-10:, 0] = 1_000_000_000.0  # ten absurd days; the 50-day mean clears $20M
    assert p.close["AAA"].mul(p.volume["AAA"]).iloc[-50:].mean() > 20_000_000.0
    assert not eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"].iloc[-1]


def test_dollar_volume_window_is_exactly_fifty_bars():
    p = make_panel({"AAA": [50.0] * 300}, volume=300_000)  # $15M/day
    p.volume.iloc[250:, 0] = 1_000_000  # $50M/day from row 250 on
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"]
    # Row 273 sees 26 thin and 24 thick days (median thin); row 275 sees 24 thin and 26 thick.
    assert not e.iloc[273] and e.iloc[275]


def test_etf_and_reference_never_eligible():
    p = make_panel(
        {"SPY": trend_series(300), "QQQ": trend_series(300), "WRNT": [50.0] * 300},
        volume=10_000_000,
    )
    kinds = {"SPY": AssetKind.ETF, "QQQ": AssetKind.ETF, "WRNT": AssetKind.WARRANT}
    assert not eligibility(p, kinds, UniverseFilter()).any().any()


def test_symbol_without_a_kind_is_not_eligible():
    p = make_panel({"AAA": [50.0] * 300, "SPY": [400.0] * 300}, volume=10_000_000)
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())  # SPY has no entry at all
    assert e["AAA"].iloc[-1] and not e["SPY"].any()


def test_only_tradeable_kinds_pass():
    kinds = {k.value: k for k in AssetKind}
    p = make_panel({name: [50.0] * 300 for name in kinds}, volume=10_000_000)
    e = eligibility(p, kinds, UniverseFilter()).iloc[-1]
    assert {kinds[s] for s in e.index[e]} == set(TRADEABLE_KINDS)


def test_history_is_counted_in_bars_within_the_current_segment():
    p = make_panel({"AAA": [50.0] * 400}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(0, 40))  # listed at row 40: its 252nd bar is row 291
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"]
    assert not e.iloc[:291].any() and e.iloc[291:].all()


def test_short_gap_keeps_the_segment_and_does_not_count_missing_days():
    p = make_panel({"AAA": [50.0] * 400}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(100, 105))  # 5 missing sessions: same segment
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"]
    # 100 bars before the gap; bar count at row r >= 105 is r - 4, so 252 bars at row 256.
    assert not e.iloc[255] and e.iloc[256]


def test_gap_over_max_restarts_history_for_a_reused_ticker():
    p = make_panel({"AAA": [50.0] * 700}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(300, 315))  # 15 missing sessions: a new company
    e = eligibility(p, {"AAA": COMMON}, UniverseFilter())["AAA"]
    assert e.iloc[299]  # the old company is long established
    assert not e.iloc[315:566].any()  # the new one restarts at row 315 ...
    assert e.iloc[566:].all()  # ... and has its 252nd bar at row 566


def test_gap_over_max_restarts_the_dollar_volume_median_too():
    f = UniverseFilter(min_history_sessions=1)  # isolate the strict 50-bar median
    p = make_panel({"AAA": [50.0] * 200}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(60, 75))  # new segment starts at row 75
    e = eligibility(p, {"AAA": COMMON}, f)["AAA"]
    assert not e.iloc[:49].any() and e.iloc[49:60].all()  # old segment: 50th bar is row 49
    assert not e.iloc[75:124].any() and e.iloc[124:].all()  # new segment: 50th bar is row 124


def test_history_gap_comes_from_the_filter_not_a_hardcoded_constant():
    p = make_panel({"AAA": [50.0] * 400}, volume=1_000_000)
    _drop_bars(p, "AAA", slice(100, 105))  # 5 missing sessions
    tight = UniverseFilter(max_gap_sessions=3, min_history_sessions=150)
    e = eligibility(p, {"AAA": COMMON}, tight)["AAA"]
    # A 3-session tolerance splits at the gap, so the 150th bar after it is at row 254.
    assert not e.iloc[253] and e.iloc[254]
    assert not e.iloc[:100].any()  # the 100 bars before the gap never reached 150


def test_universe_gap_threshold_matches_the_indicator_gap_threshold():
    # Both define "same company" the same way; they must not drift apart.
    assert UniverseFilter().max_gap_sessions == MAX_GAP


def test_eligibility_has_no_look_ahead():
    rng_a = trend_series(700, start=50.0, drift=0.0, vol=0.01, seed=1)
    rng_b = trend_series(700, start=60.0, drift=0.0, vol=0.01, seed=2)
    p = make_panel({"AAA": rng_a, "BBB": rng_b, "CCC": [50.0] * 700}, volume=1_000_000)
    _drop_bars(p, "BBB", slice(300, 315))  # reused ticker
    _drop_bars(p, "CCC", slice(0, 100))  # late listing
    _drop_bars(p, "AAA", slice(500, 502))  # brief halt
    kinds = {"AAA": COMMON, "BBB": COMMON, "CCC": COMMON}
    f = UniverseFilter()
    full = eligibility(p, kinds, f)
    assert full.to_numpy().any() and not full.to_numpy().all()
    for row in (150, 299, 314, 400, 566, 650):
        cut = eligibility(truncate(p, p.dates()[row]), kinds, f)
        pd.testing.assert_frame_equal(full.iloc[: row + 1], cut)


# --- minimum volatility: a price-pinned stock (a pending cash buyout) is not tradable --------
#
# Incident: the first real scan listed HZO and UTZ as breakouts with stops 0.6-0.7% below the
# trigger. Both had sat in a ~1% band for two months (ATR14 about 0.25% of price) because they
# were pending cash buyouts, so the price was pinned to the deal price. Upside is capped and a
# broken deal gaps through a tiny stop, so they are out of the universe: ATR14 / close must reach
# ``UniverseFilter.min_atr_pct``.


def _pinned(n: int = 300, price: float = 50.0, move: float = 0.0005) -> list[float]:
    """Closes that alternate +/- ``move`` around ``price`` (a stock pinned to a deal price)."""
    return [price * (1.0 + move * (-1) ** i) for i in range(n)]


def _swinging(n: int = 300, price: float = 50.0, move: float = 0.015) -> list[float]:
    """Closes that alternate +/- ``move`` around ``price`` (an ordinary, moving stock)."""
    return _pinned(n, price, move)


def _pinned_and_swinging() -> Panel:
    # Same price, same $50M a day, same 300 bars; only the daily range differs.
    return make_panel(
        {"PINNED": _pinned(), "SWING": _swinging()}, volume=1_000_000, range_pct=0.0002
    )


def test_pinned_stock_excluded_by_min_atr_pct():
    p = _pinned_and_swinging()
    e = eligibility(p, {"PINNED": COMMON, "SWING": COMMON}, UniverseFilter())
    atr_pct = (atr(p, 14) / p.close).iloc[-1]
    assert atr_pct["PINNED"] < 0.002 and atr_pct["SWING"] > 0.015  # the premise
    assert not e["PINNED"].iloc[-1] and e["SWING"].iloc[-1]
    assert not e["PINNED"].any()  # pinned throughout, so never eligible on any day


def test_min_atr_pct_zero_disables_rule():
    p = _pinned_and_swinging()
    kinds = {"PINNED": COMMON, "SWING": COMMON}
    e = eligibility(p, kinds, UniverseFilter(min_atr_pct=0.0))
    assert e["PINNED"].iloc[-1] and e["SWING"].iloc[-1]
    # Disabled means no effect at all, warm-up days included (their ATR is still NaN).
    warm = UniverseFilter(min_atr_pct=0.0, dollar_volume_window=1, min_history_sessions=1)
    assert eligibility(p, kinds, warm)["PINNED"].iloc[:13].all()


def test_min_atr_pct_default_and_boundary():
    assert UniverseFilter().min_atr_pct == 0.005
    p = _pinned_and_swinging()
    kinds = {"PINNED": COMMON, "SWING": COMMON}
    ratio = float((atr(p, 14) / p.close).iloc[-1]["PINNED"])
    on = eligibility(p, kinds, UniverseFilter(min_atr_pct=ratio))["PINNED"].iloc[-1]
    over = eligibility(p, kinds, UniverseFilter(min_atr_pct=ratio * 1.0001))["PINNED"].iloc[-1]
    assert on and not over  # the comparison is >=


def test_nan_atr_during_warmup_is_not_eligible():
    # Only the ATR rule can bind here: no history floor, a one-bar dollar-volume window.
    f = UniverseFilter(dollar_volume_window=1, min_history_sessions=1)
    p = make_panel({"SWING": _swinging(40)}, volume=1_000_000, range_pct=0.0002)
    assert atr(p, 14)["SWING"].iloc[:13].isna().all()
    e = eligibility(p, {"SWING": COMMON}, f)["SWING"]
    assert not e.iloc[:13].any() and e.iloc[13:].all()  # the 14th bar is the first with an ATR
    # Without the rule the same days are eligible, so the ATR rule alone excluded them.
    off = dataclasses.replace(f, min_atr_pct=0.0)
    assert eligibility(p, {"SWING": COMMON}, off)["SWING"].all()


def test_atr14_argument_matches_computing_it_inside():
    p = _pinned_and_swinging()
    kinds = {"PINNED": COMMON, "SWING": COMMON}
    f = UniverseFilter()
    pd.testing.assert_frame_equal(
        eligibility(p, kinds, f), eligibility(p, kinds, f, atr14=atr(p, 14))
    )
    # The argument is what is read: an ATR of zero everywhere excludes the moving stock too.
    zero = pd.DataFrame(0.0, index=p.close.index, columns=p.close.columns)
    assert not eligibility(p, kinds, f, atr14=zero).any().any()
    assert eligibility(p, kinds, f, atr14=p.close * 0.5).iloc[-1].all()  # ... and 50% admits both


def test_atr_ratio_uses_the_adjusted_close_not_the_raw_close():
    # Adjusted $500 with a 0.1% ATR is pinned; raw $50 would read the ATR as 1% and let it in.
    p = make_panel({"AAA": _pinned(300, price=500.0)}, volume=100_000, range_pct=0.0002)
    raw = pd.DataFrame(p.close.to_numpy() / 10.0, index=p.close.index, columns=p.close.columns)
    with_raw = Panel(p.open, p.high, p.low, p.close, p.volume, raw_close=raw)
    assert (raw >= 10.0).all().all()  # the premise: raw clears the $10 floor
    assert (atr(p, 14) / raw).iloc[-1]["AAA"] > 0.005  # ... and a raw ratio would pass
    assert not eligibility(with_raw, {"AAA": COMMON}, UniverseFilter())["AAA"].any()


def test_atr_ratio_is_split_invariant():
    # A 10:1 split rescales every adjusted price in history; ATR and close scale together.
    p = _pinned_and_swinging()
    scaled = Panel(*(getattr(p, f) / 10.0 for f in ("open", "high", "low", "close")), p.volume * 10)
    f = UniverseFilter(min_price=0.0)  # the price floor is about the price, not the ratio
    kinds = {"PINNED": COMMON, "SWING": COMMON}
    pd.testing.assert_frame_equal(eligibility(p, kinds, f), eligibility(scaled, kinds, f))


def test_atr_rule_is_point_in_time():
    # PINNED trades normally, is deal-pinned from row 350, then breaks loose at row 500: the
    # rule's answer flips twice, and every row's answer must not depend on later rows.
    moves = np.r_[np.full(350, 0.015), np.full(150, 0.0005), np.full(200, 0.015)]
    price = 50.0 * np.cumprod(1.0 + moves * (-1.0) ** np.arange(700))
    p = make_panel(
        {"PINNED": price.tolist(), "SWING": _swinging(700)}, volume=1_000_000, range_pct=0.0002
    )
    kinds = {"PINNED": COMMON, "SWING": COMMON}
    f = UniverseFilter()
    full = eligibility(p, kinds, f)
    flips = full["PINNED"].astype(int).diff().abs().sum()
    assert flips >= 2 and full["SWING"].iloc[252:].all()  # premise: the answer really changes
    for row in (260, 349, 360, 380, 450, 499, 520, 600, 699):
        cut = eligibility(truncate(p, p.dates()[row]), kinds, f)
        pd.testing.assert_frame_equal(full.iloc[: row + 1], cut)


def test_build_context_uses_ind_atr14(monkeypatch):
    panel = synthetic_universe_panel(seed=5)
    kinds = synthetic_kinds(panel)
    as_of = panel.dates()[450].date()
    seen: list[dict] = []
    real = scan_module.eligibility

    def spy(*args, **kwargs):
        seen.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(scan_module, "eligibility", spy)
    ctx = build_context(panel, kinds, as_of)

    assert len(seen) == 1
    # It reuses the indicators it just computed rather than recomputing the ATR.
    assert seen[0]["kwargs"]["atr14"] is ctx.ind.atr14
    expected = real(ctx.panel, kinds, UniverseFilter(), atr14=ctx.ind.atr14)
    expected[scan_module.BENCHMARK] = False
    pd.testing.assert_frame_equal(ctx.eligible, expected)


def test_build_context_applies_min_atr_pct():
    panel = synthetic_universe_panel(seed=5)
    kinds = synthetic_kinds(panel)
    as_of = panel.dates()[450].date()
    default = build_context(panel, kinds, as_of)
    off = build_context(panel, kinds, as_of, universe=UniverseFilter(min_atr_pct=0.0))
    strict = build_context(panel, kinds, as_of, universe=UniverseFilter(min_atr_pct=0.5))
    assert not strict.eligible.to_numpy().any()  # 50% of price a day: nothing qualifies
    assert off.eligible.to_numpy().sum() >= default.eligible.to_numpy().sum()


def test_regime_rules():
    up = pd.Series(np.linspace(100, 200, 300))
    down = up[::-1].reset_index(drop=True)
    assert market_regime(up).iloc[-1] == Regime.UPTREND
    assert market_regime(down).iloc[-1] == Regime.DOWNTREND
    assert market_regime(up).iloc[10] == Regime.MIXED


def test_regime_members_and_values():
    assert [r.value for r in Regime] == ["uptrend", "mixed", "downtrend"]
    assert Regime.UPTREND == "uptrend"


def test_regime_is_mixed_until_sma200_exists():
    up = pd.Series(np.linspace(100, 200, 300))
    r = market_regime(up)
    assert (r.iloc[:199] == Regime.MIXED).all()
    assert r.iloc[199] == Regime.UPTREND  # first bar with a full 200-bar SMA


def test_regime_keeps_the_input_index_and_holds_regime_members():
    spy = make_panel({"SPY": list(np.linspace(100, 200, 300))}).close["SPY"]
    r = market_regime(spy)
    assert r.index.equals(spy.index) and len(r) == len(spy)
    assert all(isinstance(v, Regime) for v in r)


def test_regime_needs_both_conditions_for_a_trend():
    # Long decline, then one spike: close > SMA200 but SMA50 < SMA200 -> not an uptrend.
    spiked = pd.Series([*np.linspace(200, 100, 299), 500.0])
    sma50, sma200 = spiked.rolling(50).mean(), spiked.rolling(200).mean()
    assert spiked.iloc[-1] > sma200.iloc[-1] and sma50.iloc[-1] < sma200.iloc[-1]
    assert market_regime(spiked).iloc[-1] == Regime.MIXED
    # Long advance, then one crash: close < SMA200 but SMA50 > SMA200 -> not a downtrend.
    crashed = pd.Series([*np.linspace(100, 200, 299), 10.0])
    sma50, sma200 = crashed.rolling(50).mean(), crashed.rolling(200).mean()
    assert crashed.iloc[-1] < sma200.iloc[-1] and sma50.iloc[-1] > sma200.iloc[-1]
    assert market_regime(crashed).iloc[-1] == Regime.MIXED


def test_regime_exact_equality_is_mixed():
    flat = pd.Series([100.0] * 300)  # close == SMA50 == SMA200
    assert market_regime(flat).iloc[-1] == Regime.MIXED
    # SMA50 exactly equal to SMA200 (both 100.5), with the close on either side of them.
    prefix = [100.0] * 100
    older = [100.0] * 75 + [101.0] * 75
    for recent, side in (([100.0] * 25 + [101.0] * 25, 1), ([101.0] * 25 + [100.0] * 25, -1)):
        s = pd.Series(prefix + older + recent)
        assert s.rolling(50).mean().iloc[-1] == s.rolling(200).mean().iloc[-1] == 100.5
        assert (s.iloc[-1] - 100.5) * side > 0  # above SMA200, then below it
        assert market_regime(s).iloc[-1] == Regime.MIXED


def test_regime_close_tied_with_sma200_is_mixed_even_when_sma50_agrees():
    # The last 200 bars average exactly 100.0 and the last close is 100.0 (a tie with SMA200).
    for older, recent, sma50_vs_sma200 in (
        ([99.0] * 147 + [100.0] * 3, [103.0] * 49, 1),  # SMA50 = 102.94 > SMA200
        ([101.0] * 147 + [100.0] * 3, [97.0] * 49, -1),  # SMA50 = 97.06 < SMA200
    ):
        s = pd.Series([100.0] * 100 + older + recent + [100.0])
        sma50, sma200 = s.rolling(50).mean().iloc[-1], s.rolling(200).mean().iloc[-1]
        assert s.iloc[-1] == sma200 == 100.0 and (sma50 - sma200) * sma50_vs_sma200 > 0
        assert market_regime(s).iloc[-1] == Regime.MIXED


def test_regime_survives_a_missing_spy_bar_without_a_long_blackout():
    spy = make_panel({"SPY": list(np.linspace(100, 200, 300))}).close["SPY"]
    spy.iloc[250] = np.nan
    r = market_regime(spy)
    assert r.iloc[250] == Regime.MIXED  # no bar, no call
    assert r.iloc[251] == Regime.UPTREND and r.iloc[-1] == Regime.UPTREND


def test_regime_has_no_look_ahead():
    wave = 150 + 40 * np.sin(np.linspace(0, 6 * np.pi, 600)) + np.linspace(0, 60, 600)
    spy = make_panel({"SPY": list(wave)}).close["SPY"]
    full = market_regime(spy)
    assert {Regime.UPTREND, Regime.DOWNTREND, Regime.MIXED} <= set(full)
    for n in (100, 199, 200, 321, 450, 599):
        pd.testing.assert_series_equal(full.iloc[:n], market_regime(spy.iloc[:n]))


def test_breadth_is_percent_of_eligible_symbols_above_sma50():
    idx = pd.bdate_range("2024-01-02", periods=3)
    close = pd.DataFrame(
        {"A": [11.0, 9.0, 11.0], "B": [11.0, 9.0, 9.0], "C": [11.0, 9.0, 11.0], "D": [11.0] * 3},
        index=idx,
    )
    sma50 = pd.DataFrame(10.0, index=idx, columns=close.columns)
    eligible = pd.DataFrame(
        {"A": [True] * 3, "B": [True] * 3, "C": [True, True, False], "D": [False] * 3}, index=idx
    )
    b = breadth(close, sma50, eligible)
    assert b.index.equals(idx)
    # Day 1: A, B, C eligible and above -> 100. Day 2: all below -> 0. Day 3: A above, B below.
    assert b.tolist() == pytest.approx([100.0, 0.0, 50.0])  # D is above SMA50 but never counted


def test_breadth_ignores_ineligible_symbols_and_equality_is_not_above():
    idx = pd.bdate_range("2024-01-02", periods=1)
    close = pd.DataFrame({"A": [10.0], "B": [12.0], "C": [50.0]}, index=idx)
    sma50 = pd.DataFrame({"A": [10.0], "B": [10.0], "C": [10.0]}, index=idx)
    eligible = pd.DataFrame({"A": [True], "B": [True], "C": [False]}, index=idx)
    assert breadth(close, sma50, eligible).iloc[0] == pytest.approx(50.0)  # A equal, B above


def test_breadth_is_nan_not_zero_when_nothing_is_eligible():
    idx = pd.bdate_range("2024-01-02", periods=2)
    close = pd.DataFrame({"A": [11.0, 11.0]}, index=idx)
    sma50 = pd.DataFrame({"A": [10.0, 10.0]}, index=idx)
    eligible = pd.DataFrame({"A": [True, False]}, index=idx)
    b = breadth(close, sma50, eligible)
    assert b.iloc[0] == pytest.approx(100.0) and np.isnan(b.iloc[1])


def test_breadth_with_real_eligibility_and_indicators_has_no_look_ahead():
    p = make_panel(
        {
            "UP": list(np.linspace(50, 150, 400)),
            "DOWN": list(np.linspace(150, 50, 400)),
            "NEW": list(np.linspace(50, 150, 400)),
            "SPY": list(np.linspace(100, 200, 400)),
        },
        volume=1_000_000,
    )
    _drop_bars(p, "NEW", slice(0, 300))  # fewer than 252 bars at the end: ineligible throughout
    kinds = {"UP": COMMON, "DOWN": COMMON, "NEW": COMMON, "SPY": AssetKind.ETF}
    f = UniverseFilter()

    def run(panel: Panel) -> pd.Series:
        return breadth(panel.close, compute_indicators(panel).sma50, eligibility(panel, kinds, f))

    full = run(p)
    assert full.iloc[-1] == pytest.approx(50.0)  # UP above its SMA50, DOWN below; NEW and SPY out
    assert np.isnan(full.iloc[100])  # nothing has 252 bars of history yet
    for row in (260, 300, 399):
        pd.testing.assert_series_equal(full.iloc[: row + 1], run(truncate(p, p.dates()[row])))
