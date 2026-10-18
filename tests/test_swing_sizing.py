"""Swing engine: fixed-fractional position sizing and open-position overlap flags.

Sizing risks ``equity * risk_pct * regime_factor`` dollars across the distance from trigger to
stop, rounds the share count down, then caps the position value at ``max_position_pct`` of
equity. With the default 25,000 account that is a 250 dollar risk budget (1%) and a 5,000 dollar
position cap (20%). Overlap flags compare an idea's daily returns with each open position over
the last ``window`` sessions; the panel is a hand-built close frame so the correlations are
known by construction: a scaled copy of a series has identical returns, an independent random
walk does not.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest
from swing_engine.config import RiskSettings
from swing_engine.sizing import Capacity, Sizing, overlap_flags, size_idea

# ---------------------------------------------------------------------------- size_idea


def test_shares_from_one_percent_risk():
    s = size_idea(148.20, 140.10, RiskSettings(), 1.0)
    assert s.shares == 30 and s.risk_dollars == pytest.approx(30 * 8.10)  # floor(250 / 8.10)
    assert s.position_value == pytest.approx(30 * 148.20)  # 4,446: under the 5,000 cap
    assert s.capped_by is None


def test_sizing_outputs_have_plain_python_types():
    s = size_idea(148.20, 140.10, RiskSettings(), 1.0)
    assert type(s.shares) is int
    assert type(s.risk_dollars) is float
    assert type(s.position_value) is float
    whole = size_idea(100, 90, RiskSettings(), 1.0)  # integer prices still give floats
    assert whole.shares == 25 and type(whole.shares) is int
    assert whole.risk_dollars == 250.0 and type(whole.risk_dollars) is float
    assert whole.position_value == 2_500.0 and type(whole.position_value) is float


def test_mixed_regime_halves_risk():
    assert size_idea(148.20, 140.10, RiskSettings(), 0.5).shares == 15


def test_position_value_cap_20_percent():
    s = size_idea(100.0, 99.0, RiskSettings(), 1.0)  # 250 shares by risk → capped to 50
    assert s.shares == 50 and s.capped_by == "max_position"
    assert s.risk_dollars == pytest.approx(50.0)
    assert s.position_value == pytest.approx(5_000.0)


def test_cap_that_ties_the_risk_shares_is_not_reported_as_binding():
    # risk allows floor(250 / 5) = 50 shares and the cap allows floor(5000 / 100) = 50
    s = size_idea(100.0, 95.0, RiskSettings(), 1.0)
    assert s.shares == 50 and s.capped_by is None


def test_cap_larger_than_one_share_price_gives_zero_shares_capped():
    s = size_idea(6_000.0, 5_990.0, RiskSettings(), 1.0)  # one share is 6,000 > 5,000 cap
    assert s.shares == 0 and s.capped_by == "max_position"
    assert s.risk_dollars == 0.0 and s.position_value == 0.0


def test_risk_budget_smaller_than_risk_per_share_gives_zero_shares():
    s = size_idea(500.0, 240.0, RiskSettings(), 1.0)  # 260 risk per share vs 250 budget
    assert s.shares == 0 and s.capped_by is None
    assert s.risk_dollars == 0.0 and s.position_value == 0.0


def test_sizing_follows_the_settings_it_is_given():
    settings = RiskSettings(equity=100_000.0, risk_pct=0.005, max_position_pct=0.10)
    s = size_idea(50.0, 48.0, settings, 1.0)  # 500 budget / 2 = 250 shares; cap 10,000 / 50 = 200
    assert s.shares == 200 and s.capped_by == "max_position"
    wide = size_idea(50.0, 40.0, settings, 1.0)  # 500 / 10 = 50 shares, cap not binding
    assert wide.shares == 50 and wide.capped_by is None


def test_downtrend_or_bad_stop_gives_zero_shares():
    down = size_idea(100, 90, RiskSettings(), 0.0)
    assert down.shares == 0 and down.capped_by is None  # no trade, but the stop was fine
    assert down.risk_dollars == 0.0 and down.position_value == 0.0
    assert size_idea(100, 100, RiskSettings(), 1.0).capped_by == "no_risk"


@pytest.mark.parametrize(
    ("trigger", "stop", "factor"),
    [
        (100.0, 100.0, 1.0),  # zero distance
        (100.0, 105.0, 1.0),  # stop above the trigger
        (0.0, -1.0, 1.0),  # a zero price would divide by zero in the cap
        (math.nan, 90.0, 1.0),
        (100.0, math.nan, 1.0),
        (math.inf, 90.0, 1.0),
        (100.0, -math.inf, 1.0),
        (100.0, 90.0, math.nan),
    ],
)
def test_no_risk_inputs_give_zero_shares_without_raising(trigger, stop, factor):
    s = size_idea(trigger, stop, RiskSettings(), factor)
    assert s == Sizing(shares=0, risk_dollars=0.0, position_value=0.0, capped_by="no_risk")


def test_bad_stop_beats_a_zero_regime_factor():
    s = size_idea(100.0, 100.0, RiskSettings(), 0.0)
    assert s.shares == 0 and s.capped_by == "no_risk"


def test_sizing_and_capacity_are_frozen():
    s = size_idea(148.20, 140.10, RiskSettings(), 1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.shares = 99  # type: ignore[misc]
    cap = Capacity(
        open_positions=3, max_positions=8, open_risk_dollars=720.0, max_risk_dollars=1500.0
    )
    assert (cap.open_positions, cap.max_positions) == (3, 8)
    assert (cap.open_risk_dollars, cap.max_risk_dollars) == (720.0, 1500.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cap.open_positions = 4  # type: ignore[misc]


# ------------------------------------------------------------------------- overlap_flags

N = 100
INDEX = pd.bdate_range("2024-01-02", periods=N)
AS_OF = INDEX[-1]


def _walk(seed: int, n: int = N) -> pd.Series:
    """A positive random walk with ~1% daily moves; each seed is an independent series."""
    steps = 0.01 * np.random.default_rng(seed).standard_normal(n)
    return pd.Series(100.0 * np.cumprod(1.0 + steps), index=INDEX[:n])


def _panel(**series: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(series, index=INDEX)


def test_overlap_flag_above_0_8_correlation():
    msft = _walk(1)
    close = _panel(MSFT=msft, IDEA=msft * 2.5, OTHER=_walk(7))  # IDEA's returns equal MSFT's
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": ["MSFT"]}
    assert overlap_flags(close, AS_OF, ["OTHER"], ["MSFT"]) == {"OTHER": []}  # independent walk


def test_one_key_per_idea_in_idea_order_and_open_symbols_in_open_order():
    base = _walk(1)
    close = _panel(AAA=base, BBB=base * 1.5, CCC=base * 0.5, X=base * 2.0, Y=_walk(7), Z=_walk(11))
    flags = overlap_flags(close, AS_OF, ["Z", "X", "Y"], ["CCC", "AAA", "BBB"])
    assert list(flags) == ["Z", "X", "Y"]
    assert flags["X"] == ["CCC", "AAA", "BBB"]  # the open_symbols order, not column order
    assert flags["Z"] == [] and flags["Y"] == []
    assert overlap_flags(close, AS_OF, ["X"], ["BBB", "CCC", "AAA"]) == {"X": ["BBB", "CCC", "AAA"]}


def test_no_idea_symbols_or_no_open_symbols():
    close = _panel(MSFT=_walk(1), IDEA=_walk(1) * 2)
    assert overlap_flags(close, AS_OF, [], ["MSFT"]) == {}
    assert overlap_flags(close, AS_OF, ["IDEA"], []) == {"IDEA": []}


def test_an_idea_that_is_also_open_is_not_compared_with_itself():
    base = _walk(1)
    close = _panel(MSFT=base, AAPL=base * 3.0, IDEA=_walk(7))
    flags = overlap_flags(close, AS_OF, ["MSFT", "IDEA"], ["MSFT", "AAPL"])
    assert flags == {"MSFT": ["AAPL"], "IDEA": []}  # MSFT's own correlation of 1 is ignored


def test_symbol_missing_from_the_panel_gives_an_empty_list():
    base = _walk(1)
    close = _panel(MSFT=base, IDEA=base * 2)
    assert overlap_flags(close, AS_OF, ["GHOST", "IDEA"], ["MSFT"]) == {
        "GHOST": [],
        "IDEA": ["MSFT"],
    }
    assert overlap_flags(close, AS_OF, ["IDEA"], ["GHOST", "MSFT"]) == {"IDEA": ["MSFT"]}
    assert overlap_flags(close, AS_OF, ["GHOST"], ["ALSO_GHOST"]) == {"GHOST": []}


def test_repeated_symbols_do_not_duplicate_flags():
    base = _walk(1)
    close = _panel(MSFT=base, IDEA=base * 2)
    flags = overlap_flags(close, AS_OF, ["IDEA", "IDEA"], ["MSFT", "MSFT"])
    assert flags == {"IDEA": ["MSFT"]}


def _stitched(recent: pd.Series, older: pd.Series, row: int) -> pd.Series:
    """A close series whose daily returns follow ``recent`` from ``row`` on, else ``older``."""
    rets = pd.Series(
        np.where(np.arange(N) >= row, recent.pct_change(), older.pct_change()), index=INDEX
    )
    return 100.0 * (1.0 + rets.fillna(0.0)).cumprod()


def test_only_the_last_window_sessions_count():
    # IDEA copies MSFT's returns on the last 20 sessions and is independent of it before that.
    msft = _walk(1)
    close = _panel(MSFT=msft, IDEA=_stitched(msft, _walk(7), row=80))
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], window=20) == {"IDEA": ["MSFT"]}
    # Widening the window to 60 pulls in 40 independent returns and the overlap disappears.
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], window=60) == {"IDEA": []}


def test_window_counts_returns_so_the_first_one_uses_the_prior_close():
    # 20 identical returns exist in the last 20 sessions only if the first of them (row 80) is
    # computed against the close at row 79, just outside the window: 20 is exactly the minimum.
    msft = _walk(1)
    close = _panel(MSFT=msft, IDEA=_stitched(msft, _walk(7), row=80))
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], window=20) == {"IDEA": ["MSFT"]}
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], window=19) == {"IDEA": []}


def test_sessions_after_as_of_are_ignored():
    # Identical returns through session 59, independent from session 60. Standing on session 59
    # it overlaps; standing on the last session the window is mostly the independent stretch.
    msft = _walk(1)
    close = _panel(MSFT=msft, IDEA=_stitched(_walk(7), msft, row=60))
    assert overlap_flags(close, INDEX[59], ["IDEA"], ["MSFT"]) == {"IDEA": ["MSFT"]}
    assert overlap_flags(close, INDEX[-1], ["IDEA"], ["MSFT"]) == {"IDEA": []}


def test_as_of_may_be_tz_aware():
    base = _walk(1)
    close = _panel(MSFT=base, IDEA=base * 2)
    as_of = pd.Timestamp(AS_OF).tz_localize("UTC")
    assert overlap_flags(close, as_of, ["IDEA"], ["MSFT"]) == {"IDEA": ["MSFT"]}


def test_threshold_is_a_strict_greater_than():
    msft = _walk(1)
    noisy = msft.pct_change().fillna(0.0) + 0.6 * _walk(7).pct_change().fillna(0.0)
    idea = 100.0 * (1.0 + noisy).cumprod()
    close = _panel(IDEA=idea, MSFT=msft)
    # The same correlation the function computes. Column order matters in the last bit, so the
    # frame lists the idea first, as overlap_flags does, to land on the identical float.
    rets = close.pct_change(fill_method=None).tail(60)
    corr = float(rets.corr(min_periods=20).loc["IDEA", "MSFT"])
    assert 0.5 < corr < 0.95  # a genuine partial correlation, not 0 or 1
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], threshold=corr) == {"IDEA": []}
    below = float(np.nextafter(corr, -np.inf))
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], threshold=below) == {"IDEA": ["MSFT"]}
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], threshold=0.99) == {"IDEA": []}
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], threshold=0.5) == {"IDEA": ["MSFT"]}


def test_negative_correlation_is_not_an_overlap():
    msft = _walk(1)
    inverse = 100.0 * (1.0 - msft.pct_change().fillna(0.0)).cumprod()  # mirrored returns
    close = _panel(MSFT=msft, IDEA=inverse)
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": []}


def _with_first_valid_close_at(row: int) -> pd.DataFrame:
    base = _walk(1)
    idea = (base * 2.0).where(np.arange(N) >= row)  # NaN before ``row``
    return _panel(MSFT=base, IDEA=idea)


def test_needs_at_least_20_overlapping_returns():
    # The first valid close is at row r, so the first valid return is at row r + 1 and the
    # window (rows 40..99) holds 99 - r returns that overlap MSFT's.
    enough = _with_first_valid_close_at(N - 21)  # 20 overlapping returns
    assert overlap_flags(enough, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": ["MSFT"]}
    short = _with_first_valid_close_at(N - 20)  # 19 overlapping returns
    assert overlap_flags(short, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": []}


def test_a_missing_bar_is_not_filled():
    # MSFT's walk with IDEA a scaled copy that lacks one bar mid-window. With no fill, the two
    # returns around the gap are dropped and the rest match exactly (correlation 1). Forward-
    # filling would turn them into a 0 return and a two-day return and pull correlation down.
    base = _walk(1)
    idea = base * 2.0
    idea.iloc[70] = np.nan
    close = _panel(MSFT=base, IDEA=idea)
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"], threshold=0.999999) == {"IDEA": ["MSFT"]}
    filled = close.ffill().pct_change().tail(60).corr().loc["IDEA", "MSFT"]
    assert filled < 0.999999  # the contrast that makes the assertion above mean something


def test_constant_prices_never_flag():
    close = _panel(MSFT=_walk(1), IDEA=pd.Series(50.0, index=INDEX))  # zero variance → NaN corr
    assert overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": []}


def test_defaults_match_the_risk_settings_defaults():
    settings = RiskSettings()
    base = _walk(1)
    close = _panel(MSFT=base, IDEA=base * 2)
    explicit = overlap_flags(
        close,
        AS_OF,
        ["IDEA"],
        ["MSFT"],
        window=settings.overlap_window,
        threshold=settings.overlap_corr,
    )
    assert explicit == overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"]) == {"IDEA": ["MSFT"]}


def test_the_input_panel_is_not_modified():
    base = _walk(1)
    close = _panel(MSFT=base, IDEA=base * 2)
    before = close.copy()
    overlap_flags(close, AS_OF, ["IDEA"], ["MSFT"])
    pd.testing.assert_frame_equal(close, before)
