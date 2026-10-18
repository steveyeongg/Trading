"""Swing engine: the trade-level backtest, its statistics and the random-entry baseline.

Three layers, each checked on exact numbers:

* ``trade_stats`` / ``by_year`` / ``regime_on`` on hand-built trade tables: only closed trades
  count (a DELISTED exit is a closed trade), scratch trades count as losses, and a year is the
  year of the entry.
* ``simulate_candidates`` on small hand-built panels with zero slippage, so every fill, exit and R
  is a number worked out by hand. A cross-check on a synthetic universe compares every row with a
  direct ``simulate_trade`` call, so the backtest and the model it wraps can never disagree. The
  ``known_date`` rule for a DELISTED exit (the 11th panel session after the last priced bar, capped
  at the last panel date) is pinned on a segment break, a short trailing run and a long one.
* ``random_entry_baseline`` on a hand-built panel where the draw is forced (a single leader) or
  reconstructed independently from ``np.random.default_rng(seed)``, so the draw protocol, the
  ``D x ATR`` stop, the missing gap filter and the delisted rule are all pinned.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from swing_engine.backtest.baseline import random_entry_baseline
from swing_engine.backtest.stats import by_year, regime_on, trade_stats
from swing_engine.backtest.trades import candidate_history, simulate_candidates
from swing_engine.config import BreakoutParams, ExecutionParams, PullbackParams
from swing_engine.execution import TrackStatus, TradeOutcome, simulate_trade
from swing_engine.indicators import Indicators, compute_indicators
from swing_engine.panel import Panel
from swing_engine.regime import market_regime
from swing_engine.scan import ScanError, build_context
from swing_engine.setups import CANDIDATE_COLUMNS, candidates_for
from swing_engine.testing import scramble_after, synthetic_kinds, synthetic_universe_panel

START = "2024-01-02"
ATR = 2.0
NO_SLIP = ExecutionParams(slippage_bps=0.0, max_gap_atr=1.0)

Bar = tuple[float, float, float, float]  # open, high, low, close

# ------------------------------------------------------------------------------- the helpers


def _panel(spec: dict[str, dict[int, Bar]], n: int) -> Panel:
    """A panel of ``n`` consecutive business days; a symbol's unlisted rows are unpriced (NaN)."""
    index = pd.bdate_range(START, periods=n)
    frames = {
        f: pd.DataFrame(np.nan, index=index, columns=list(spec))
        for f in ("open", "high", "low", "close", "volume")
    }
    for symbol, rows in spec.items():
        for k, (o, h, low, c) in rows.items():
            frames["open"].at[index[k], symbol] = o
            frames["high"].at[index[k], symbol] = h
            frames["low"].at[index[k], symbol] = low
            frames["close"].at[index[k], symbol] = c
            frames["volume"].at[index[k], symbol] = 1_000_000.0
    return Panel(**frames)


def _ind(panel: Panel, atr: float = ATR) -> Indicators:
    """Indicators whose ATR is ``atr`` on every cell; ``simulate_candidates`` reads only that."""
    blank = pd.DataFrame(np.nan, index=panel.close.index, columns=panel.close.columns)
    fields = {f.name: blank for f in dataclasses.fields(Indicators)}
    fields["atr14"] = pd.DataFrame(atr, index=blank.index, columns=blank.columns)
    return Indicators(**fields)


def _candidates(
    panel: Panel,
    rows: list[tuple[int, str, float, float]],
    regime: str | list[str] = "uptrend",
    index: list[int] | None = None,
) -> pd.DataFrame:
    """Candidate rows ``(signal row, symbol, trigger, stop)`` with the engine's column set."""
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([panel.close.index[k] for k, *_ in rows]),
            "symbol": [s for _, s, _, _ in rows],
            "setup": "breakout",
            "version": "breakout-v1",
            "trigger": [t for _, _, t, _ in rows],
            "stop": [s for _, _, _, s in rows],
            "atr14": ATR,
            "rs_pct": 90.0,
            "regime": regime,
        },
        columns=[*CANDIDATE_COLUMNS, "regime"],
    )
    if index is not None:
        frame.index = pd.Index(index)
    return frame


def _day(panel: Panel, k: int) -> pd.Timestamp:
    return pd.Timestamp(panel.close.index[k])


# ------------------------------------------------------------------------------ trade_stats


def closed_trades(
    r: list[float],
    hold: list[int] | None = None,
    entry: list[str] | None = None,
    regime: list[str] | None = None,
) -> pd.DataFrame:
    """A trade table of closed trades with the given R multiples and holding periods."""
    n = len(r)
    return pd.DataFrame(
        {
            "status": "closed",
            "r_multiple": r,
            "sessions_held": hold if hold is not None else [5] * n,
            "entry_date": pd.to_datetime(entry if entry is not None else ["2023-03-01"] * n),
            "regime": regime if regime is not None else ["uptrend"] * n,
        }
    )


def test_trade_stats_on_known_r_values():
    t = closed_trades(r=[2.0, -1.0, -1.0, 1.0], hold=[10, 3, 4, 8])
    s = trade_stats(t)
    assert s["n"] == 4 and s["win_rate"] == 0.5 and s["expectancy_r"] == pytest.approx(0.25)
    assert s["profit_factor"] == pytest.approx(1.5) and s["avg_hold"] == pytest.approx(6.25)
    assert s["avg_win_r"] == pytest.approx(1.5) and s["avg_loss_r"] == pytest.approx(-1.0)
    assert set(s) == {
        "n",
        "win_rate",
        "avg_win_r",
        "avg_loss_r",
        "expectancy_r",
        "profit_factor",
        "avg_hold",
    }
    assert type(s["n"]) is int


def test_open_and_untriggered_rows_excluded_from_stats():
    closed = closed_trades(r=[2.0, -1.0, 0.5], hold=[10, 3, 7])
    closed["exit_reason"] = ["trail", "stop", "delisted"]  # a DELISTED exit is a closed trade
    others = pd.DataFrame(
        {
            "status": ["open", "not_triggered", "skipped_gap", "pending"],
            "r_multiple": [np.nan] * 4,
            "sessions_held": [40, 0, 0, 0],
            "entry_date": pd.to_datetime(["2023-03-01", None, None, None]),
            "regime": ["uptrend"] * 4,
            "exit_reason": [None] * 4,
        }
    )
    s = trade_stats(pd.concat([closed, others], ignore_index=True))
    assert s["n"] == 3
    assert s["expectancy_r"] == pytest.approx(0.5)
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["avg_hold"] == pytest.approx((10 + 3 + 7) / 3)  # the 40-session open row is out
    assert s["profit_factor"] == pytest.approx(2.5 / 1.0)


def test_no_closed_trades_gives_n_zero_and_nan_values_without_raising():
    only_open = pd.DataFrame(
        {
            "status": ["open", "pending"],
            "r_multiple": [np.nan, np.nan],
            "sessions_held": [4, 0],
            "entry_date": pd.to_datetime(["2023-03-01", None]),
            "regime": ["uptrend", "mixed"],
        }
    )
    empty = only_open.iloc[0:0]
    for table in (only_open, empty):
        s = trade_stats(table)
        assert s["n"] == 0 and type(s["n"]) is int
        for key in ("win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "avg_hold"):
            assert math.isnan(s[key]), key
        assert math.isnan(s["profit_factor"])


def test_a_scratch_trade_is_a_loss_and_an_all_winner_book_has_no_avg_loss():
    s = trade_stats(closed_trades(r=[0.0, 2.0, 0.0]))
    assert s["win_rate"] == pytest.approx(1 / 3)
    assert s["avg_win_r"] == pytest.approx(2.0) and s["avg_loss_r"] == pytest.approx(0.0)
    assert s["profit_factor"] == math.inf  # gains and no losing trade

    winners = trade_stats(closed_trades(r=[1.0, 3.0]))
    assert winners["win_rate"] == 1.0 and math.isnan(winners["avg_loss_r"])
    assert winners["profit_factor"] == math.inf

    losers = trade_stats(closed_trades(r=[-1.0, -0.5]))
    assert losers["win_rate"] == 0.0 and math.isnan(losers["avg_win_r"])
    assert losers["profit_factor"] == 0.0


def test_by_year_groups_closed_trades_by_the_year_of_the_entry():
    t = closed_trades(
        r=[2.0, -1.0, 1.0],
        hold=[10, 8, 4],
        # The December entry exits in January but belongs to 2022.
        entry=["2022-03-01", "2022-12-30", "2023-01-03"],
    )
    open_2023 = pd.DataFrame(
        {
            "status": ["open"],
            "r_multiple": [np.nan],
            "sessions_held": [2],
            "entry_date": pd.to_datetime(["2023-06-01"]),
            "regime": ["uptrend"],
        }
    )
    out = by_year(pd.concat([t, open_2023], ignore_index=True))
    assert list(out) == [2022, 2023] and all(type(y) is int for y in out)
    assert out[2022]["n"] == 2 and out[2022]["expectancy_r"] == pytest.approx(0.5)
    assert out[2022]["avg_hold"] == pytest.approx(9.0)
    assert out[2023]["n"] == 1 and out[2023]["expectancy_r"] == pytest.approx(1.0)
    assert by_year(t.iloc[0:0]) == {}


def test_regime_on_drops_downtrend_trades_and_keeps_everything_else():
    t = closed_trades(
        r=[1.0, -1.0, 2.0, -1.0],
        regime=["uptrend", "downtrend", "mixed", "downtrend"],
    )
    kept = regime_on(t)
    assert list(kept.index) == [0, 2] and list(kept["regime"]) == ["uptrend", "mixed"]
    assert list(kept.columns) == list(t.columns)
    assert len(t) == 4  # the input is not modified
    assert trade_stats(kept)["expectancy_r"] == pytest.approx(1.5)


# --------------------------------------------------------------------------- candidate_history

PANEL = synthetic_universe_panel(seed=5)
KINDS = synthetic_kinds(PANEL)
DATES = PANEL.dates()
BREAKOUT = BreakoutParams()
LO, HI = DATES[300].date(), DATES[500].date()


def test_candidate_history_columns_window_and_regime():
    hist = candidate_history(PANEL, KINDS, BREAKOUT, LO, HI)
    assert list(hist.columns) == [*CANDIDATE_COLUMNS, "regime"]
    assert len(hist) > 10
    assert hist["date"].min() >= pd.Timestamp(LO) and hist["date"].max() <= pd.Timestamp(HI)
    assert list(hist.index) == list(range(len(hist)))
    assert (hist["setup"] == "breakout").all()

    spy_regime = market_regime(PANEL.close["SPY"]).map(lambda r: r.value)
    assert list(hist["regime"]) == list(spy_regime.reindex(hist["date"]))
    assert all(type(r) is str for r in hist["regime"])
    assert {"uptrend", "mixed"} <= set(hist["regime"])  # the window is not a single regime


def test_candidate_history_window_is_inclusive_at_both_ends():
    hist = candidate_history(PANEL, KINDS, BREAKOUT, LO, HI)
    first, last = hist["date"].min(), hist["date"].max()
    exact = candidate_history(PANEL, KINDS, BREAKOUT, first.date(), last.date())
    pd.testing.assert_frame_equal(exact, hist)
    after_first = candidate_history(
        PANEL, KINDS, BREAKOUT, (first + pd.Timedelta(days=1)).date(), HI
    )
    assert (after_first["date"] > first).all() and len(after_first) < len(hist)
    before_last = candidate_history(
        PANEL, KINDS, BREAKOUT, LO, (last - pd.Timedelta(days=1)).date()
    )
    assert (before_last["date"] < last).all() and len(before_last) < len(hist)


def test_candidate_history_equals_a_per_date_truncated_scan():
    hist = candidate_history(PANEL, KINDS, BREAKOUT, LO, HI)
    days = sorted(pd.DatetimeIndex(hist["date"].unique()))
    for day in (days[0], days[len(days) // 2], days[-1]):
        ctx = build_context(PANEL, KINDS, day.date())
        found = candidates_for(ctx.panel, ctx.ind, ctx.leaders, ctx.rs_pct, BREAKOUT)
        want = found[found["date"] == day].reset_index(drop=True)
        got = hist[hist["date"] == day].reset_index(drop=True)
        assert len(want) > 0
        pd.testing.assert_frame_equal(got[CANDIDATE_COLUMNS], want[CANDIDATE_COLUMNS])
        assert (got["regime"] == ctx.regime.loc[day].value).all()


def test_candidate_history_cannot_see_past_its_end():
    scrambled = scramble_after(PANEL, HI, seed=1)
    a = candidate_history(PANEL, KINDS, BREAKOUT, LO, HI)
    b = candidate_history(scrambled, KINDS, BREAKOUT, LO, HI)
    assert len(a) > 10
    pd.testing.assert_frame_equal(a, b)


def test_candidate_history_with_no_candidates_is_an_empty_frame_with_the_columns():
    early = DATES[10].date()  # before any indicator is warm
    hist = candidate_history(PANEL, KINDS, BREAKOUT, early, early)
    assert hist.empty and list(hist.columns) == [*CANDIDATE_COLUMNS, "regime"]
    assert str(hist["date"].dtype).startswith("datetime64")


def test_candidate_history_supports_the_pullback_setup_and_requires_a_spy_bar_on_end():
    hist = candidate_history(PANEL, KINDS, PullbackParams(), LO, HI)
    assert len(hist) > 10 and (hist["setup"] == "pullback").all()
    saturday = date(2021, 1, 2)
    assert saturday.weekday() == 5
    with pytest.raises(ScanError):
        candidate_history(PANEL, KINDS, BREAKOUT, LO, saturday)


# ----------------------------------------------------------------------- simulate_candidates

SIG: Bar = (98.0, 99.0, 97.0, 98.5)
FILL: Bar = (101.0, 102.0, 100.5, 101.0)


def _trade_panel() -> Panel:
    """Six symbols over 12 sessions, one outcome each (``MAXH`` has two trades)."""
    return _panel(
        {
            # Fills at 101 on row 1, then the low on row 2 reaches the stop of 96.
            "STP": {0: SIG, 1: FILL, 2: (100.0, 100.5, 95.0, 95.5)},
            # Row 1 never reaches the trigger of 100.
            "NTR": {0: SIG, 1: (98.5, 99.5, 98.0, 99.0)},
            # Row 1 opens at 103.5, beyond the trigger + 1 ATR of 102.
            "GAP": {0: SIG, 1: (103.5, 104.0, 103.0, 103.8)},
            "MAXH": {
                0: SIG,
                1: FILL,
                2: (101.0, 102.5, 100.5, 102.0),
                3: (102.0, 103.5, 101.5, 103.0),
                4: (103.0, 104.5, 102.5, 104.0),
                5: (104.0, 106.5, 103.5, 106.0),  # the 5th held session: the time stop
                6: (106.0, 107.0, 105.0, 106.0),  # the second signal
                7: (107.5, 108.5, 107.0, 108.0),
                8: (108.0, 109.0, 107.5, 109.0),
                9: (109.0, 110.0, 108.5, 110.0),
                10: (110.0, 111.0, 109.5, 111.0),
                11: (111.0, 113.5, 110.5, 113.0),
            },
            # Filled on row 9, three sessions held when the data ends.
            "OPEN": {
                8: SIG,
                9: FILL,
                10: (101.0, 102.5, 100.8, 102.0),
                11: (102.0, 103.0, 101.5, 102.5),
            },
            "PEND": {11: SIG},  # signalled on the last session: nothing to trade yet
        },
        n=12,
    )


def _trade_candidates(panel: Panel) -> pd.DataFrame:
    return _candidates(
        panel,
        [
            (0, "MAXH", 100.0, 96.0),
            (0, "STP", 100.0, 96.0),
            (8, "OPEN", 100.0, 96.0),
            (6, "MAXH", 107.0, 103.0),
            (0, "NTR", 100.0, 96.0),
            (11, "PEND", 100.0, 96.0),
            (0, "GAP", 100.0, 96.0),
        ],
        regime=["uptrend", "mixed", "uptrend", "downtrend", "mixed", "uptrend", "mixed"],
        index=[30, 31, 32, 33, 34, 35, 36],
    )


def test_simulate_candidates_returns_one_row_per_candidate_in_input_order_with_outcomes():
    panel = _trade_panel()
    cands = _trade_candidates(panel)
    params = BreakoutParams(max_hold=5)
    out = simulate_candidates(panel, _ind(panel), cands, params, NO_SLIP, delisted=set())

    # Every candidate column comes back untouched and in place, outcome columns follow.
    assert list(out.columns[: len(cands.columns)]) == list(cands.columns)
    pd.testing.assert_frame_equal(out[list(cands.columns)], cands)
    assert list(out.columns[len(cands.columns) :]) == [
        "status",
        "entry_date",
        "entry_price",
        "initial_stop",
        "exit_date",
        "exit_price",
        "exit_reason",
        "r_multiple",
        "sessions_held",
        "current_stop",
        "highest_close",
        "known_date",
    ]
    assert list(out.index) == list(cands.index)
    assert list(out["symbol"]) == ["MAXH", "STP", "OPEN", "MAXH", "NTR", "PEND", "GAP"]

    d = lambda k: _day(panel, k)  # noqa: E731
    nat = pd.NaT
    # (status, entry k, entry price, exit k, exit price, reason, R, sessions held)
    want = [
        ("closed", 1, 101.0, 5, 106.0, "max_hold", 1.0, 5),
        ("closed", 1, 101.0, 2, 96.0, "stop", -1.0, 2),
        ("open", 9, 101.0, None, None, None, None, 3),
        ("closed", 7, 107.5, 11, 113.0, "max_hold", 5.5 / 4.5, 5),
        ("not_triggered", None, None, None, None, None, None, 0),
        ("pending", None, None, None, None, None, None, 0),
        ("skipped_gap", None, None, None, None, None, None, 0),
    ]
    for i, (status, ek, ep, xk, xp, reason, r, held) in enumerate(want):
        row = out.iloc[i]
        assert row["status"] == status, i
        assert row["sessions_held"] == held, i
        assert row["initial_stop"] == cands.iloc[i]["stop"], i
        if ek is None:
            assert row["entry_date"] is nat and math.isnan(row["entry_price"]), i
        else:
            assert row["entry_date"] == d(ek) and row["entry_price"] == pytest.approx(ep), i
        if xk is None:
            assert row["exit_date"] is nat and row["known_date"] is nat, i
            assert math.isnan(row["exit_price"]) and math.isnan(row["r_multiple"]), i
            assert row["exit_reason"] is None, i
        else:
            assert row["exit_date"] == d(xk) and row["known_date"] == d(xk), i
            assert row["exit_price"] == pytest.approx(xp) and row["exit_reason"] == reason, i
            assert row["r_multiple"] == pytest.approx(r), i

    assert str(out["entry_date"].dtype) == "datetime64[ns]"
    assert str(out["exit_date"].dtype) == "datetime64[ns]"
    assert str(out["known_date"].dtype) == "datetime64[ns]"
    # Only the closed rows feed the statistics.
    assert trade_stats(out)["n"] == 3
    assert trade_stats(out)["expectancy_r"] == pytest.approx((1.0 - 1.0 + 5.5 / 4.5) / 3)
    assert trade_stats(regime_on(out))["n"] == 2  # the second MAXH trade was in a downtrend


def test_simulate_candidates_with_no_candidates_returns_an_empty_frame_with_every_column():
    panel = _trade_panel()
    empty = _trade_candidates(panel).iloc[0:0]
    out = simulate_candidates(panel, _ind(panel), empty, BreakoutParams(), NO_SLIP, set())
    assert out.empty and {"status", "r_multiple", "known_date", "regime"} <= set(out.columns)


def test_simulate_candidates_rejects_misaligned_inputs():
    panel = _trade_panel()
    cands = _trade_candidates(panel)
    short = _ind(panel)
    short = dataclasses.replace(short, atr14=short.atr14.iloc[:-1])
    with pytest.raises(ValueError, match="atr14"):
        simulate_candidates(panel, short, cands, BreakoutParams(), NO_SLIP, set())
    unknown = cands.copy()
    unknown.loc[30, "symbol"] = "ZZZ"
    with pytest.raises(ValueError, match="ZZZ"):
        simulate_candidates(panel, _ind(panel), unknown, BreakoutParams(), NO_SLIP, set())


def _assert_matches_direct(
    panel: Panel,
    ind: Indicators,
    trades: pd.DataFrame,
    params: BreakoutParams | PullbackParams,
    ex: ExecutionParams,
    delisted: set[str],
) -> None:
    """Every row equals ``simulate_trade`` on a freshly built frame for its symbol."""
    for row in trades.itertuples(index=False):
        sym = str(row.symbol)
        frame = pd.DataFrame(
            {
                "open": panel.open[sym],
                "high": panel.high[sym],
                "low": panel.low[sym],
                "close": panel.close[sym],
                "atr14": ind.atr14[sym],
            }
        )
        direct = simulate_trade(
            frame,
            row.date,
            row.trigger,
            row.stop,
            params.trail_atr,
            params.max_hold,
            ex,
            delisted=sym in delisted,
        )
        for f in dataclasses.fields(TradeOutcome):
            want = getattr(direct, f.name)
            got = getattr(row, f.name)
            if want is None:
                assert pd.isna(got), (sym, row.date, f.name)
            elif f.name.endswith("_date"):
                assert got == pd.Timestamp(want), (sym, row.date, f.name)
            elif isinstance(want, float):
                assert got == pytest.approx(want, rel=1e-12), (sym, row.date, f.name)
            else:
                assert str(got) == str(want), (sym, row.date, f.name)


@pytest.mark.parametrize(
    "params", [BreakoutParams(), PullbackParams()], ids=["breakout", "pullback"]
)
def test_simulate_candidates_agrees_with_simulate_trade_on_a_synthetic_universe(params):
    ind = compute_indicators(PANEL)
    cands = candidate_history(PANEL, KINDS, params, DATES[0].date(), DATES[-1].date())
    ex = ExecutionParams()
    trades = simulate_candidates(PANEL, ind, cands, params, ex, delisted=set())
    assert len(trades) == len(cands) > 100
    pd.testing.assert_frame_equal(trades[list(cands.columns)], cands)
    # A real mix: filled and closed, never triggered, and still open when the data ends.
    assert {"closed", "not_triggered", "open"} <= set(trades["status"])
    assert trades["exit_reason"].dropna().isin(["stop", "trail", "max_hold"]).all()
    _assert_matches_direct(PANEL, ind, trades, params, ex, set())
    assert (
        trades.loc[trades["status"] == "closed", "known_date"]
        == trades.loc[trades["status"] == "closed", "exit_date"]
    ).all()

    closed = trades[trades["status"] == "closed"]
    stats = trade_stats(trades)
    assert stats["n"] == len(closed)
    assert stats["expectancy_r"] == pytest.approx(closed["r_multiple"].mean())
    safe = regime_on(trades)
    assert 0 < trade_stats(safe)["n"] <= stats["n"]
    assert sum(v["n"] for v in by_year(trades).values()) == stats["n"]


def test_a_delisted_flag_closes_open_trades_at_the_last_bar_and_known_date_is_the_last_session():
    params = BreakoutParams()
    ind = compute_indicators(PANEL)
    cands = candidate_history(PANEL, KINDS, params, DATES[0].date(), DATES[-1].date())
    ex = ExecutionParams()
    plain = simulate_candidates(PANEL, ind, cands, params, ex, delisted=set())
    flagged = simulate_candidates(PANEL, ind, cands, params, ex, delisted=set(PANEL.symbols()))
    assert (plain["status"] == "open").any() and not (flagged["status"] == "open").any()
    newly = flagged[plain["status"].eq("open")]
    assert (newly["exit_reason"] == "delisted").all() and (newly["status"] == "closed").all()
    # These symbols have bars to the end of the panel, so the exit is the last session and
    # the 11-session wait is capped there.
    assert (newly["exit_date"] == DATES[-1]).all() and (newly["known_date"] == DATES[-1]).all()
    _assert_matches_direct(PANEL, ind, flagged, params, ex, set(PANEL.symbols()))
    same = plain["status"].ne("open")
    pd.testing.assert_frame_equal(flagged[same], plain[same])


# -------------------------------------------------------------------------------- delisting


def _rising(signal: int, last: int) -> dict[int, Bar]:
    """A quiet uptrend: the signal bar on ``signal``, a 101 fill on the next row, +0.5 a day.

    The row after the signal opens and closes at 101; row ``signal + 1 + j`` closes at
    ``101 + 0.5 j``. Nothing comes near the stop of 96 or the 3-ATR trail.
    """
    rows: dict[int, Bar] = {signal: SIG}
    prev = 101.0
    for k in range(signal + 1, last + 1):
        close = 101.0 + 0.5 * (k - signal - 1)
        open_ = 101.0 if k == signal + 1 else prev
        rows[k] = (open_, max(open_, close) + 0.5, min(open_, close) - 0.5, close)
        prev = close
    return rows


@pytest.mark.parametrize("flagged", [False, True])
def test_a_segment_break_closes_as_delisted_and_known_date_is_eleven_sessions_later(flagged):
    # OLD trades for 10 sessions, goes dark for 21, then a different company reuses the ticker.
    rows = _rising(2, 9)
    rows.update({k: (5.0, 5.1, 4.9, 5.0) for k in range(31, 40)})
    panel = _panel({"OLD": rows}, n=40)
    cands = _candidates(panel, [(2, "OLD", 100.0, 96.0)])
    out = simulate_candidates(
        panel, _ind(panel), cands, BreakoutParams(), NO_SLIP, {"OLD"} if flagged else set()
    )
    row = out.iloc[0]
    assert row["status"] == "closed" and row["exit_reason"] == "delisted"
    assert row["exit_date"] == _day(panel, 9)  # the last bar of the old company
    assert row["exit_price"] == pytest.approx(104.0)  # row 9's close: 101 + 0.5 * 6
    assert row["sessions_held"] == 7
    assert row["r_multiple"] == pytest.approx(3.0 / 5.0)
    # Eleven unpriced rows must pass before the break is knowable: rows 10..20.
    assert row["known_date"] == _day(panel, 9 + 11) == _day(panel, 20)


def test_a_flagged_symbol_whose_bars_end_near_the_panel_end_is_known_on_the_last_session():
    rows = _rising(2, 24)  # bars to row 24, then 5 unpriced rows to row 29
    panel = _panel({"GONE": rows}, n=30)
    cands = _candidates(panel, [(2, "GONE", 100.0, 96.0)])
    ind = _ind(panel)

    flagged = simulate_candidates(panel, ind, cands, BreakoutParams(), NO_SLIP, {"GONE"})
    row = flagged.iloc[0]
    assert row["status"] == "closed" and row["exit_reason"] == "delisted"
    assert row["exit_date"] == _day(panel, 24) and row["sessions_held"] == 22
    assert row["exit_price"] == pytest.approx(101.0 + 0.5 * 21)
    assert row["r_multiple"] == pytest.approx((101.0 + 0.5 * 21 - 101.0) / 5.0)
    assert row["known_date"] == _day(panel, 29)  # 24 + 11 = 35 is capped at the last session

    unflagged = simulate_candidates(panel, ind, cands, BreakoutParams(), NO_SLIP, set())
    assert unflagged.iloc[0]["status"] == "open" and unflagged.iloc[0]["known_date"] is pd.NaT


@pytest.mark.parametrize("flagged", [False, True])
def test_a_long_trailing_gap_is_a_delisting_known_eleven_sessions_after_the_last_bar(flagged):
    panel = _panel({"GONE": _rising(2, 19)}, n=40)  # 20 unpriced rows to the end
    cands = _candidates(panel, [(2, "GONE", 100.0, 96.0)])
    out = simulate_candidates(
        panel, _ind(panel), cands, BreakoutParams(), NO_SLIP, {"GONE"} if flagged else set()
    )
    row = out.iloc[0]
    assert row["exit_reason"] == "delisted" and row["exit_date"] == _day(panel, 19)
    assert row["known_date"] == _day(panel, 30)


def test_delisted_candidate_counted_with_delisted_exit():
    panel = _panel({"GONE": _rising(2, 24)}, n=30)
    cands = _candidates(panel, [(2, "GONE", 100.0, 96.0)])
    ind = _ind(panel)

    flagged = simulate_candidates(panel, ind, cands, BreakoutParams(), NO_SLIP, {"GONE"})
    s = trade_stats(flagged)
    assert s["n"] == 1 and s["win_rate"] == 1.0
    assert s["expectancy_r"] == pytest.approx(10.5 / 5.0)
    assert s["avg_hold"] == pytest.approx(22.0)
    assert by_year(flagged) == {2024: s}

    unflagged = simulate_candidates(panel, ind, cands, BreakoutParams(), NO_SLIP, set())
    assert trade_stats(unflagged)["n"] == 0  # still open: not a result yet


# ---------------------------------------------------------------------- random_entry_baseline

BASE_PARAMS = BreakoutParams(max_hold=3)  # trail 3 ATR
BASE_EX = ExecutionParams(slippage_bps=10.0, max_gap_atr=0.0)  # a real trade would skip any gap
SLIP = 0.001


def _baseline_panel() -> Panel:
    quiet = (109.0, 111.0, 108.0, 110.0)
    return _panel(
        {
            "L": {
                **{k: quiet for k in range(4)},
                4: (120.0, 122.0, 119.0, 121.0),  # a gap up from 110: no gap filter applies
                5: (121.0, 123.0, 120.0, 122.0),
                6: (122.0, 124.0, 121.0, 123.0),
                7: (123.0, 125.0, 122.0, 124.0),
                8: (124.0, 126.0, 123.0, 125.0),
                9: (125.0, 127.0, 124.0, 126.0),
                10: (126.0, 128.0, 125.0, 127.0),
                11: (127.0, 129.0, 126.0, 128.0),
            },
            "X": {
                **{k: (49.0, 51.0, 48.0, 50.0) for k in range(4)},
                4: (50.0, 51.0, 40.0, 41.0),  # crashes through any 2-ATR stop
                **{k: (41.0, 42.0, 40.0, 41.0) for k in range(5, 12)},
            },
            "Y": {k: (80.0, 81.0, 79.0, 80.0) for k in range(12)},
        },
        n=12,
    )


def _baseline_candidates(panel: Panel) -> pd.DataFrame:
    return _candidates(
        panel,
        [
            # D = median(1, 2, 4) = 2 ATR on row 3, and 3 ATR on row 6.
            (3, "L", 110.0, 108.0),
            (3, "X", 50.0, 46.0),
            (3, "Y", 80.0, 72.0),
            (6, "L", 112.0, 106.0),
        ],
    )


def _leaders(panel: Panel, *symbols: str) -> pd.DataFrame:
    out = pd.DataFrame(False, index=panel.close.index, columns=panel.close.columns)
    out[list(symbols)] = True
    return out


# Hand-worked R of a draw on L: the entry is the next open plus 10 bps, the stop sits D x ATR
# below that entry, and the 3-session time stop sells the third close less 10 bps.
r_l_row3 = (123.0 * (1 - SLIP) - 120.0 * (1 + SLIP)) / (2.0 * ATR)  # enters row 4, D = 2
r_l_row6 = (126.0 * (1 - SLIP) - 123.0 * (1 + SLIP)) / (3.0 * ATR)  # enters row 7, D = 3


def test_baseline_with_one_leader_draws_it_every_time_at_next_open_with_a_d_atr_stop():
    panel = _baseline_panel()
    out = random_entry_baseline(
        panel,
        _ind(panel),
        _leaders(panel, "L"),
        _baseline_candidates(panel),
        BASE_PARAMS,
        BASE_EX,
        seeds=4,
    )
    assert [r_l_row3, r_l_row6] == pytest.approx([0.68925, 0.4585])
    assert out.shape == (4,)
    np.testing.assert_allclose(out, (r_l_row3 + r_l_row6) / 2.0, rtol=1e-12)


def _direct_r(
    panel: Panel,
    symbol: str,
    signal_row: int,
    d_atr: float,
    delisted: frozenset[str] = frozenset(),
    ex: ExecutionParams = BASE_EX,
    params: BreakoutParams = BASE_PARAMS,
) -> float:
    """R of one random-entry draw, built on the primitives: trigger 0, stop = entry - D x ATR."""
    frame = pd.DataFrame(
        {
            "open": panel.open[symbol],
            "high": panel.high[symbol],
            "low": panel.low[symbol],
            "close": panel.close[symbol],
            "atr14": ATR,
        }
    )
    entry = frame["open"].iloc[signal_row + 1] * (1.0 + ex.slippage_bps / 10_000.0)
    out = simulate_trade(
        frame,
        frame.index[signal_row],
        0.0,
        entry - d_atr * ATR,
        params.trail_atr,
        params.max_hold,
        ExecutionParams(slippage_bps=ex.slippage_bps, max_gap_atr=math.inf),
        delisted=symbol in delisted,
    )
    assert out.status == TrackStatus.CLOSED and out.r_multiple is not None
    return out.r_multiple


def test_baseline_draw_protocol_is_default_rng_per_seed_without_replacement_per_date():
    panel = _baseline_panel()
    out = random_entry_baseline(
        panel,
        _ind(panel),
        _leaders(panel, "L", "X"),
        _baseline_candidates(panel),
        BASE_PARAMS,
        BASE_EX,
        seeds=10,
    )
    assert _direct_r(panel, "L", 3, 2.0) == pytest.approx(r_l_row3)

    picks = []
    for seed in range(10):
        rng = np.random.default_rng(seed)
        # Row 3 has 3 candidates but only 2 leaders: both are taken and the rng is not touched.
        # Row 6 has 1 candidate: one of the two leaders, in column order [L, X].
        pick = int(rng.choice(2, size=1, replace=False)[0])
        picks.append(pick)
        want = np.mean(
            [
                _direct_r(panel, "L", 3, 2.0),
                _direct_r(panel, "X", 3, 2.0),
                _direct_r(panel, ["L", "X"][pick], 6, 3.0),
            ]
        )
        assert out[seed] == pytest.approx(want, rel=1e-12), seed
    assert set(picks) == {0, 1}  # the seeds really do draw different symbols
    assert len(set(np.round(out, 9))) == 2


def test_baseline_draws_k_distinct_leaders_when_there_are_more_leaders_than_candidates():
    panel = _baseline_panel()
    cands = _candidates(panel, [(3, "L", 110.0, 108.0), (3, "X", 50.0, 46.0)])  # D = 1.5, k = 2
    out = random_entry_baseline(
        panel,
        _ind(panel),
        _leaders(panel, "L", "X", "Y"),
        cands,
        BASE_PARAMS,
        BASE_EX,
        seeds=10,
    )
    r = {s: _direct_r(panel, s, 3, 1.5) for s in "LXY"}
    assert len({round(v, 6) for v in r.values()}) == 3  # each symbol is told apart by its R
    pairs = []
    for seed in range(10):
        picked = np.random.default_rng(seed).choice(3, 2, replace=False)
        pairs.append(tuple(sorted(picked)))
        assert out[seed] == pytest.approx(np.mean([r["LXY"[i]] for i in picked]), rel=1e-12), seed
    assert len(set(pairs)) == 3  # all three pairs come up across the seeds


def test_baseline_skips_a_draw_with_no_next_open_and_a_signal_on_the_last_session():
    panel = _baseline_panel()
    only_l = _leaders(panel, "L")
    cands = _baseline_candidates(panel)

    # Row 7 unpriced: the row-6 draw has no open to enter at, so only the row-3 trade counts.
    gapped = _baseline_panel()
    for frame in (gapped.open, gapped.high, gapped.low, gapped.close):
        frame.at[frame.index[7], "L"] = np.nan
    out = random_entry_baseline(gapped, _ind(gapped), only_l, cands, BASE_PARAMS, BASE_EX, seeds=2)
    np.testing.assert_allclose(out, r_l_row3, rtol=1e-12)

    # A candidate on the very last session has no next session at all: it adds nothing.
    last = pd.concat([cands, _candidates(panel, [(11, "L", 130.0, 126.0)])], ignore_index=True)
    out = random_entry_baseline(panel, _ind(panel), only_l, last, BASE_PARAMS, BASE_EX, seeds=2)
    np.testing.assert_allclose(out, (r_l_row3 + r_l_row6) / 2.0, rtol=1e-12)


def test_baseline_ignores_the_gap_filter_but_honours_slippage():
    panel = _baseline_panel()
    args = (panel, _ind(panel), _leaders(panel, "L"), _baseline_candidates(panel), BASE_PARAMS)
    strict = random_entry_baseline(*args, ExecutionParams(slippage_bps=10.0, max_gap_atr=0.0), 1)
    loose = random_entry_baseline(*args, ExecutionParams(slippage_bps=10.0, max_gap_atr=9.0), 1)
    assert np.array_equal(strict, loose) and np.isfinite(strict).all()
    free = random_entry_baseline(*args, ExecutionParams(slippage_bps=0.0, max_gap_atr=0.0), 1)
    want = ((123.0 - 120.0) / (2 * ATR) + (126.0 - 123.0) / (3 * ATR)) / 2.0
    assert free[0] == pytest.approx(want)
    assert free[0] > strict[0]  # slippage costs R


def test_baseline_counts_a_delisted_exit_as_a_closed_trade():
    rows = {
        **{k: (109.0, 111.0, 108.0, 110.0) for k in range(4)},
        4: (112.0, 114.0, 111.0, 113.0),
        5: (113.0, 115.0, 112.0, 114.0),
        6: (114.0, 116.0, 113.0, 115.0),
        7: (115.0, 117.0, 114.0, 116.0),
        8: (116.0, 118.0, 115.0, 117.0),
    }  # bars end on row 8; rows 9..11 are unpriced
    panel = _panel({"GONE": rows}, n=12)
    cands = _candidates(panel, [(3, "GONE", 112.0, 108.0)])  # D = 2 ATR
    params = BreakoutParams(max_hold=30)
    args = (panel, _ind(panel), _leaders(panel, "GONE"), cands, params, NO_SLIP)

    flagged = random_entry_baseline(*args, 3, frozenset({"GONE"}))
    np.testing.assert_allclose(flagged, (117.0 - 112.0) / (2 * ATR), rtol=1e-12)  # 1.25
    assert _direct_r(panel, "GONE", 3, 2.0, frozenset({"GONE"}), NO_SLIP, params) == pytest.approx(
        1.25
    )

    unflagged = random_entry_baseline(*args, 3)  # the trade is still open: no closed trade
    assert unflagged.shape == (3,) and np.isnan(unflagged).all()


def test_baseline_with_no_candidates_or_no_seeds():
    panel = _baseline_panel()
    empty = _baseline_candidates(panel).iloc[0:0]
    args = (panel, _ind(panel), _leaders(panel, "L"))
    out = random_entry_baseline(*args, empty, BASE_PARAMS, BASE_EX, seeds=3)
    assert out.shape == (3,) and np.isnan(out).all()
    cands = _baseline_candidates(panel)
    assert random_entry_baseline(*args, cands, BASE_PARAMS, BASE_EX, seeds=0).shape == (0,)


def test_random_baseline_is_deterministic_per_seed_and_uses_leaders_only():
    ctx = build_context(PANEL, KINDS, DATES[-1].date())
    cands = candidate_history(PANEL, KINDS, BREAKOUT, DATES[0].date(), DATES[-1].date())
    args = (ctx.panel, ctx.ind, ctx.leaders, cands, BREAKOUT, ExecutionParams())
    a = random_entry_baseline(*args, seeds=5)
    b = random_entry_baseline(*args, seeds=5)
    assert np.array_equal(a, b) and a.shape == (5,)
    assert np.isfinite(a).all() and len(set(a)) > 1  # seeds really differ

    # Each seed is its own stream: asking for fewer seeds gives a prefix, and the order the
    # candidates arrive in changes nothing.
    assert np.array_equal(random_entry_baseline(*args, seeds=3), a[:3])
    shuffled = cands.sample(frac=1.0, random_state=0)
    assert np.array_equal(
        random_entry_baseline(
            ctx.panel, ctx.ind, ctx.leaders, shuffled, BREAKOUT, ExecutionParams(), seeds=5
        ),
        a,
    )

    # Only leaders are ever drawn: with no leader on any date there is nothing to trade.
    nobody = ctx.leaders & False
    none = random_entry_baseline(ctx.panel, ctx.ind, nobody, cands, BREAKOUT, ExecutionParams(), 2)
    assert none.shape == (2,) and np.isnan(none).all()
