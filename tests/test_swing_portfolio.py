"""Swing engine: the capacity-limited portfolio simulation and the SPY buy-and-hold benchmark.

Three layers, each checked on exact numbers worked out by hand:

* ``simulate_portfolio`` on small hand-built trade tables and close frames, so every share count,
  cash balance and equity mark is a number a reader can redo: the slot and total-risk limits, the
  cash account (a position is cut to what the cash buys), the daily order (exits known that
  session, then entries by relative strength, then same-day exits, then the close mark), capital
  freed on ``known_date`` rather than ``exit_date``, and the window (only trades entered inside
  it, positions still held at its end marked and not booked).
* ``buy_and_hold`` on hand-built closes: normalised to 1.0 at the start, NaN closes carried
  forward, and the same negative drawdown sign as the portfolio.
* ``simulate_portfolio`` on the real ``simulate_candidates`` output of a synthetic universe,
  checked against limits recounted independently from the ``taken`` table.

Settings use ``equity=100_000``, so the default 1% risk is $1,000 a trade, the 20% position cap is
$20,000, and the 6% total-risk cap is $6,000. A trade entering at 50 with a stop at 45 risks $5 a
share and so sizes to 200 shares ($10,000); all of these numbers are exact in floating point.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from swing_engine.backtest.portfolio import PortfolioResult, buy_and_hold, simulate_portfolio
from swing_engine.backtest.trades import candidate_history, simulate_candidates
from swing_engine.config import BreakoutParams, ExecutionParams, RiskSettings
from swing_engine.indicators import compute_indicators
from swing_engine.setups import CANDIDATE_COLUMNS
from swing_engine.testing import synthetic_kinds, synthetic_universe_panel

D = pd.bdate_range("2024-01-02", periods=10)  # D[0] .. D[9], consecutive business days
BASE = RiskSettings(equity=100_000.0)  # 1% risk, 20% position cap, 8 slots, 6% total risk

OUTCOME_COLUMNS = [
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
]
TRADE_COLUMNS = [*CANDIDATE_COLUMNS, "regime", *OUTCOME_COLUMNS, "known_date"]


# ------------------------------------------------------------------------------- the helpers


def _ts(k: int | None) -> pd.Timestamp:
    return pd.NaT if k is None else D[k]


def _trade(
    symbol: str,
    entry: int | None,
    price: float = 50.0,
    stop: float = 45.0,
    rs: float = 90.0,
    regime: str = "uptrend",
    exit: int | None = None,
    exit_price: float = math.nan,
    known: int | None = None,
) -> dict[str, object]:
    """One row of a ``simulate_candidates`` table; ``entry``/``exit``/``known`` are D[k] rows.

    ``entry=None`` is a candidate that never filled. ``known`` defaults to ``exit``, as it does
    for any exit that is not a delisting.
    """
    filled = entry is not None
    closed = exit is not None
    return {
        "date": _ts(None if entry is None else max(entry - 1, 0)),
        "symbol": symbol,
        "setup": "breakout",
        "version": "breakout-v1",
        "trigger": price,
        "stop": stop,
        "atr14": 2.0,
        "rs_pct": rs,
        "regime": regime,
        "status": "closed" if closed else "open" if filled else "not_triggered",
        "entry_date": _ts(entry),
        "entry_price": price if filled else math.nan,
        "initial_stop": stop,
        "exit_date": _ts(exit),
        "exit_price": exit_price,
        "exit_reason": "trail" if closed else None,
        "r_multiple": math.nan,
        "sessions_held": 0,
        "current_stop": stop if filled else math.nan,
        "highest_close": math.nan,
        "known_date": _ts(known if known is not None else exit),
    }


def _trades(rows: list[dict[str, object]]) -> pd.DataFrame:
    """A trade table with the engine's columns and dtypes (``datetime64[ns]`` dates)."""
    frame = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    for col in ("date", "entry_date", "exit_date", "known_date"):
        frame[col] = pd.to_datetime(frame[col]).astype("datetime64[ns]")
    return frame


def _close(columns: dict[str, list[float]]) -> pd.DataFrame:
    """A close frame on the first ``len(list)`` sessions of ``D``, one column per symbol."""
    n = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=D[:n])


def _flat(symbols: list[str], price: float, n: int) -> pd.DataFrame:
    return _close({s: [price] * n for s in symbols})


def _run(
    rows: list[dict[str, object]],
    close: pd.DataFrame,
    settings: RiskSettings = BASE,
    start: int = 0,
    end: int | None = None,
) -> PortfolioResult:
    last = len(close) - 1 if end is None else end
    return simulate_portfolio(_trades(rows), close, settings, D[start].date(), D[last].date())


def _sharpe(values: list[float]) -> float:
    """Annualised Sharpe of the daily returns of an equity path, from the plain formula."""
    e = np.array(values)
    r = e[1:] / e[:-1] - 1.0
    return float(r.mean() / r.std(ddof=1) * math.sqrt(252))


# ------------------------------------------------------------------------- the position limits


def test_capacity_limits_concurrent_positions_to_max():
    rs = [91, 99, 93, 100, 95, 92, 98, 94, 96, 97]  # S0 .. S9, deliberately not in symbol order
    rows = [_trade(f"S{i}", entry=1, rs=r) for i, r in enumerate(rs)]
    loose = dataclasses.replace(BASE, max_total_risk_pct=1.0)  # isolate the slot limit
    res = _run(rows, _flat([f"S{i}" for i in range(10)], 50.0, 3), loose)
    # The 8 best by RS, best first; S0 (91) and S5 (92) are the two left out.
    assert list(res.taken["symbol"]) == ["S3", "S1", "S6", "S9", "S8", "S4", "S7", "S2"]
    assert set(res.taken["shares"]) == {200}
    assert res.equity.iloc[1] == pytest.approx(100_000.0)  # marked at the entry price: no P&L


def test_total_open_risk_cap_6_percent():
    rows = [_trade(f"S{i}", entry=1, rs=100.0 - i) for i in range(7)]
    res = _run(rows, _flat([f"S{i}" for i in range(7)], 50.0, 3))
    # Each position risks 200 x $5 = $1,000, so six reach the $6,000 cap exactly (the cap is
    # inclusive) and the seventh would take open risk to $7,000, with a free slot to spare.
    assert list(res.taken["symbol"]) == [f"S{i}" for i in range(6)]
    assert list(res.taken["shares"]) == [200] * 6


def test_a_rejected_entry_does_not_block_smaller_ones_behind_it():
    rows = [_trade(f"U{i}", entry=1, rs=100.0 - i) for i in range(5)]  # $1,000 each: $5,000
    rows += [
        _trade("M1", entry=1, rs=95.0, regime="mixed"),  # half size: $500, total $5,500
        _trade("U5", entry=1, rs=94.0),  # $1,000 would make $6,500: rejected
        _trade("M2", entry=1, rs=93.0, regime="mixed"),  # $500 makes exactly $6,000: accepted
    ]
    symbols = [r["symbol"] for r in rows]
    res = _run(rows, _flat([str(s) for s in symbols], 50.0, 3))
    assert list(res.taken["symbol"]) == ["U0", "U1", "U2", "U3", "U4", "M1", "M2"]
    assert list(res.taken["shares"]) == [200] * 5 + [100, 100]


def test_a_downtrend_trade_is_skipped_and_a_mixed_one_is_half_size():
    rows = [
        _trade("DN", entry=1, rs=99.0, regime="downtrend"),
        _trade("MX", entry=1, rs=98.0, regime="mixed"),
        _trade("UP", entry=1, rs=97.0, regime="uptrend"),
    ]
    res = _run(rows, _flat(["DN", "MX", "UP"], 50.0, 3))
    assert list(res.taken["symbol"]) == ["MX", "UP"]
    assert list(res.taken["shares"]) == [100, 200]


@pytest.mark.parametrize(
    ("pairs", "first"),
    [
        ([("BBB", 90.0), ("AAA", 90.0)], "AAA"),  # a tie goes to the earlier symbol
        ([("AAA", 80.0), ("BBB", 95.0), ("CCC", 90.0)], "BBB"),  # higher RS first
        ([("AAA", math.nan), ("ZZZ", 10.0)], "ZZZ"),  # an unranked trade goes last
    ],
)
def test_entries_are_ordered_by_rs_desc_then_symbol_asc(pairs, first):
    rows = [_trade(s, entry=1, rs=r) for s, r in pairs]
    one_slot = dataclasses.replace(BASE, max_positions=1)
    res = _run(rows, _flat([s for s, _ in pairs], 50.0, 3), one_slot)
    assert list(res.taken["symbol"]) == [first]


# ------------------------------------------------------------------ one position per symbol


def test_a_second_signal_for_a_held_symbol_is_skipped_while_another_symbol_enters():
    rows = [
        _trade("X", entry=1, rs=90.0),  # held from D1, no exit
        _trade("X", entry=2, rs=99.0),  # a fresh signal on the held name: skipped, despite best RS
        _trade("Y", entry=2, rs=80.0),  # another name the same day: taken
    ]
    res = _run(rows, _flat(["X", "Y"], 50.0, 4))
    assert list(res.taken["symbol"]) == ["X", "Y"]
    assert list(res.taken.index) == [0, 2]  # the second X row (index 1) never entered
    assert list(res.taken["shares"]) == [200, 200]
    assert res.equity.tolist() == pytest.approx([100_000.0] * 4)
    assert res.exposure == pytest.approx((0.0 + 0.1 + 0.2 + 0.2) / 4)  # X from D1, Y from D2


def test_two_signals_for_one_symbol_on_the_same_day_take_the_better_ranked_one():
    rows = [_trade("X", entry=1, rs=80.0), _trade("X", entry=1, rs=95.0)]
    res = _run(rows, _flat(["X"], 50.0, 3))
    assert list(res.taken.index) == [1] and list(res.taken["rs_pct"]) == [95.0]


def test_a_symbol_can_be_entered_again_once_its_first_position_is_booked_out():
    rows = [
        _trade("X", entry=1, rs=90.0, exit=3, exit_price=55.0),  # held D1 .. D3
        _trade("X", entry=2, rs=95.0),  # D2: still held, skipped
        _trade("X", entry=3, rs=80.0),  # D3: the first is booked out in step 1, so it enters
    ]
    res = _run(rows, _flat(["X"], 50.0, 5))
    assert list(res.taken.index) == [0, 2]
    # D3: the first 200 shares are sold at 55 (cash 101,000), then 200 more are bought at 50
    # (sized on D2's 100,000 mark) and marked at the 50 close.
    assert list(res.taken["shares"]) == [200, 200]
    assert res.equity.tolist() == pytest.approx([100_000.0] * 3 + [101_000.0] * 2)


def test_a_symbol_stays_held_until_known_date_even_after_its_exit_date():
    # X's exit is on D2 but known only on D5 (the delisting wait), so the name is blocked until
    # D5; the D3 signal is skipped and the D5 one is taken once the exit is booked.
    rows = [
        _trade("X", entry=1, rs=90.0, exit=2, exit_price=39.0, known=5),
        _trade("X", entry=3, rs=99.0),
        _trade("X", entry=5, rs=80.0),
    ]
    res = _run(rows, _close({"X": [50.0, 50.0, 40.0, 40.0, 40.0, 40.0]}))
    assert list(res.taken.index) == [0, 2]
    # D5: the sale at 39 (cash 97,800), then 196 shares (980 / 5 on D4's 98,000 mark) at 50,
    # marked at 40: 97,800 - 9,800 + 7,840.
    assert list(res.taken["shares"]) == [200, 196]
    assert res.equity.iloc[-1] == pytest.approx(95_840.0)


# ----------------------------------------------------------------------------- the cash account


def test_an_entry_the_cash_cannot_cover_is_cut_to_what_it_buys_and_then_skipped():
    big = RiskSettings(
        equity=100_000.0, risk_pct=0.05, max_position_pct=0.9, max_total_risk_pct=0.095
    )
    rows = [
        _trade("A", entry=1, price=100.0, stop=90.0, rs=99.0),  # 500 shares, $50,000: cash 50,000
        _trade("B", entry=1, price=120.0, stop=110.0, rs=95.0),  # wants 500 ($60,000)
        _trade("C", entry=1, price=100.0, stop=90.0, rs=90.0),  # wants 500, cash is $80
    ]
    close = _close({"A": [100.0] * 3, "B": [120.0] * 3, "C": [100.0] * 3})
    res = _run(rows, close, big)
    # B is cut to floor(50,000 / 120) = 416 shares ($49,920), leaving $80, so C buys none.
    assert list(res.taken["symbol"]) == ["A", "B"]
    assert list(res.taken["shares"]) == [500, 416]
    # The total-risk check sees the filled 416 shares: $5,000 + $4,160 = $9,160 fits under the
    # $9,500 cap, where the 500 B was sized for ($10,000) would not have.
    assert res.equity.tolist() == pytest.approx([100_000.0] * 3)
    assert res.exposure == pytest.approx((0.0 + 2 * 99_920.0 / 100_000.0) / 3)


# ------------------------------------------------------------------- equity path and metrics


def test_equity_marks_to_market_and_books_exit_price():
    # 200 shares bought at 50 on D1, sold at 53 on D3 (the close that day is 55, so the sale is
    # at the exit price and not the close).
    rows = [_trade("X", entry=1, exit=3, exit_price=53.0)]
    close = _close({"X": [50.0, 52.0, 49.0, 55.0, 60.0]})
    res = _run(rows, close)
    # cash 90,000 + 200 x (52, 49) while held; 90,000 + 200 x 53 = 100,600 once sold.
    path = [100_000.0, 100_400.0, 99_800.0, 100_600.0, 100_600.0]
    assert res.equity.tolist() == pytest.approx(path)
    assert list(res.equity.index) == list(D[:5])
    assert res.equity.iloc[0] == 100_000.0  # starts at the account's equity
    assert list(res.taken["shares"]) == [200]
    assert res.cagr == pytest.approx((100_600 / 100_000) ** (252 / 4) - 1)
    assert res.sharpe == pytest.approx(_sharpe(path))
    # Held at the close of D1 and D2 only: (10,400 / 100,400 + 9,800 / 99,800) / 5 of the book.
    assert res.exposure == pytest.approx((10_400 / 100_400 + 9_800 / 99_800) / 5)
    assert isinstance(res, PortfolioResult)


def test_max_dd_is_a_negative_fraction():
    rows = [_trade("X", entry=1, exit=3, exit_price=53.0)]
    res = _run(rows, _close({"X": [50.0, 52.0, 49.0, 55.0, 60.0]}))
    # Peak 100,400 (D1) to trough 99,800 (D2): a 0.5976% drawdown, reported as -0.005976.
    assert res.max_dd == pytest.approx(-600.0 / 100_400.0)
    assert res.max_dd < 0


def test_sizing_uses_the_start_of_day_marked_equity():
    rows = [
        # 100 shares at 100 on D1, marked at 110 on D1, sold at 120 on D2.
        _trade("A", entry=1, price=100.0, stop=90.0, rs=99.0, exit=2, exit_price=120.0),
        _trade("B", entry=2, price=50.0, stop=45.0, rs=90.0),
    ]
    close = _close({"A": [100.0, 110.0, 120.0, 120.0], "B": [50.0] * 4})
    res = _run(rows, close)
    # A is sized on the $100,000 starting equity (1,000 / 10 = 100 shares). B is sized on the
    # $101,000 that D1 closed at: 1,010 / 5 = 202 shares, not 200 and not the 204 that D2's
    # sale at 120 (cash 102,000) would give.
    assert list(res.taken["symbol"]) == ["A", "B"]
    assert list(res.taken["shares"]) == [100, 202]
    assert res.equity.tolist() == pytest.approx([100_000.0, 101_000.0, 102_000.0, 102_000.0])


def test_an_entry_on_the_first_session_is_skipped_so_equity_starts_at_settings_equity():
    rows = [
        # The base day: this entry would lose 2,000 (200 shares, 50 -> 40) before the first mark.
        _trade("FIRST", entry=0, rs=99.0, exit=1, exit_price=40.0),
        _trade("SECOND", entry=1, rs=50.0),
    ]
    res = _run(rows, _flat(["FIRST", "SECOND"], 50.0, 3))
    assert list(res.taken["symbol"]) == ["SECOND"]  # FIRST, with the better RS, never enters
    assert res.equity.iloc[0] == 100_000.0  # exactly, not a mark after a day of trading
    assert res.equity.tolist() == [100_000.0] * 3 and res.max_dd == 0.0
    # The same holds when the window opens later: its first session is the base day.
    later = _run(rows, _flat(["FIRST", "SECOND"], 50.0, 3), start=1)
    assert list(later.taken["symbol"]) == [] and later.equity.iloc[0] == 100_000.0
    assert list(later.equity.index) == list(D[1:3])


def test_a_loss_after_the_base_day_shows_in_the_drawdown_cagr_and_sharpe():
    # 200 shares enter at 50 on the second session and are sold at 40 on the third.
    rows = [_trade("X", entry=1, exit=2, exit_price=40.0)]
    res = _run(rows, _close({"X": [50.0, 50.0, 40.0, 40.0]}))
    path = [100_000.0, 100_000.0, 98_000.0, 98_000.0]  # 90,000 + 200 x 40 = 98,000
    assert res.equity.tolist() == pytest.approx(path)
    assert res.max_dd == pytest.approx(-0.02) and res.max_dd < 0  # -2,000 from the 100,000 peak
    assert res.cagr == pytest.approx(0.98 ** (252 / 3) - 1)
    assert res.sharpe == pytest.approx(_sharpe(path))


def test_no_trade_leaves_equity_flat_with_empty_taken():
    rows = [_trade("X", entry=None)]  # never filled
    res = _run(rows, _flat(["X"], 50.0, 4))
    assert res.equity.tolist() == [100_000.0] * 4
    assert res.cagr == 0.0 and res.max_dd == 0.0 and res.exposure == 0.0
    assert math.isnan(res.sharpe)  # zero volatility
    assert res.taken.empty and list(res.taken.columns) == [*TRADE_COLUMNS, "shares"]
    assert res.taken["shares"].dtype == np.int64


# ------------------------------------------------------------------------------ the daily order


def test_a_same_day_entry_and_exit_books_the_exit_and_holds_the_slot_while_entries_run():
    one_slot = dataclasses.replace(BASE, max_positions=1)
    rows = [
        _trade("A", entry=1, rs=95.0, exit=1, exit_price=47.0),  # in and out on D1
        _trade("B", entry=1, rs=90.0),  # finds A's slot taken: A exits only after entries
        _trade("C", entry=2, rs=80.0),  # D2: A's slot is long gone
    ]
    close = _close({"A": [50.0, 48.0, 48.0, 48.0], "B": [50.0] * 4, "C": [50.0] * 4})
    res = _run(rows, close, one_slot)
    assert list(res.taken["symbol"]) == ["A", "C"]
    # A: 200 shares in at 50 and out at 47, a $600 loss, and it is not held at the D1 close
    # (whatever the symbol closed at). C is sized on D1's 99,400 (994 / 5 = 198 shares) and held
    # at its own entry price from D2.
    assert list(res.taken["shares"]) == [200, 198]
    assert res.equity.tolist() == pytest.approx([100_000.0, 99_400.0, 99_400.0, 99_400.0])
    assert res.exposure == pytest.approx(2 * (198 * 50.0 / 99_400.0) / 4)


def test_capital_is_freed_on_known_date_not_exit_date():
    # X: 200 shares at 50 on D1; its bars stop after D2 (last close 40), the exit is 39 on D2
    # but is known only on D4 (the delisting wait).
    one_slot = dataclasses.replace(BASE, max_positions=1)
    rows = [
        _trade("X", entry=1, rs=99.0, exit=2, exit_price=39.0, known=4),
        _trade("Y", entry=3, price=20.0, stop=18.0, rs=95.0),  # D3: X still holds the slot
        _trade("Z", entry=4, price=20.0, stop=18.0, rs=90.0),  # D4: X is booked first
    ]
    nan = math.nan
    close = _close(
        {
            "X": [50.0, 50.0, 40.0, nan, nan, nan],
            "Y": [20.0] * 6,
            "Z": [20.0] * 6,
        }
    )
    res = _run(rows, close, one_slot)
    assert list(res.taken["symbol"]) == ["X", "Z"]
    # Z is sized on D3's mark of 98,000 (X at its carried-forward 40): 980 / 2 = 490 shares.
    assert list(res.taken["shares"]) == [200, 490]
    # D2, D3: X marked at 40 (carried over D3's missing bar). D4: X sold at 39 (cash 97,800),
    # Z bought for 9,800 and marked at 20, so equity is 97,800, not 98,000.
    path = [100_000.0, 100_000.0, 98_000.0, 98_000.0, 97_800.0, 97_800.0]
    assert res.equity.tolist() == pytest.approx(path)


def test_a_trade_known_after_the_window_ends_is_held_and_marked_not_booked():
    rows = [_trade("X", entry=1, rs=99.0, exit=2, exit_price=39.0, known=4)]
    nan = math.nan
    res = _run(rows, _close({"X": [50.0, 50.0, 40.0, nan, nan, nan]}), end=3)
    assert list(res.equity.index) == list(D[:4])
    assert list(res.taken["symbol"]) == ["X"]
    # At D3 the exit is not yet known: 90,000 + 200 x the last close, 40; not 39.
    assert res.equity.iloc[-1] == pytest.approx(98_000.0)


def test_a_trade_exiting_after_the_window_ends_is_marked_at_the_close_not_the_exit_price():
    rows = [_trade("X", entry=1, exit=4, exit_price=70.0)]
    res = _run(rows, _close({"X": [50.0, 50.0, 55.0, 60.0, 65.0]}), end=3)
    assert res.equity.iloc[-1] == pytest.approx(90_000.0 + 200 * 60.0)


def test_only_trades_entered_inside_the_window_are_considered():
    rows = [
        _trade("EARLY", entry=0, rs=99.0),  # entered before the window opens
        _trade("NEVER", entry=None, rs=98.0),  # never filled
        _trade("LATE", entry=6, rs=97.0),  # entered after the window closes
        _trade("IN", entry=2, rs=50.0),
    ]
    res = _run(rows, _flat(["EARLY", "NEVER", "LATE", "IN"], 50.0, 8), start=1, end=5)
    assert list(res.equity.index) == list(D[1:6])
    assert list(res.taken["symbol"]) == ["IN"]
    assert res.equity.iloc[0] == 100_000.0


# --------------------------------------------------------------------------- input validation


def test_a_trade_on_a_symbol_missing_from_close_raises():
    with pytest.raises(ValueError, match="GHOST"):
        _run([_trade("GHOST", entry=1)], _flat(["X"], 50.0, 3))


def test_an_unknown_regime_raises():
    with pytest.raises(ValueError, match="sideways"):
        _run([_trade("X", entry=1, regime="sideways")], _flat(["X"], 50.0, 3))


def test_a_trade_table_without_known_date_raises():
    trades = _trades([_trade("X", entry=1)]).drop(columns=["known_date"])
    with pytest.raises(ValueError, match="known_date"):
        simulate_portfolio(trades, _flat(["X"], 50.0, 3), BASE, D[0].date(), D[2].date())


def test_a_window_with_no_session_raises():
    far = date(2030, 1, 2)
    with pytest.raises(ValueError, match="session"):
        simulate_portfolio(_trades([]), _flat(["X"], 50.0, 3), BASE, far, far)


# ----------------------------------------------------------------------------- buy and hold


def test_buy_and_hold_max_dd_matches_hand_value():
    res = buy_and_hold(pd.Series([100.0, 120.0, 90.0, 110.0], index=D[:4]), D[0], D[3])
    assert res.equity.tolist() == pytest.approx([1.0, 1.2, 0.9, 1.1])
    # Peak 1.2 to trough 0.9: a 25% drawdown, negative like the portfolio's.
    assert res.max_dd == pytest.approx(-0.25)
    assert res.cagr == pytest.approx(1.1 ** (252 / 3) - 1)
    assert res.sharpe == pytest.approx(_sharpe([1.0, 1.2, 0.9, 1.1]))
    assert res.exposure == 1.0


def test_buy_and_hold_uses_the_window_and_carries_missing_closes_forward():
    nan = math.nan
    close = pd.Series([10.0, 11.0, nan, 12.0, 12.1, nan, 13.0, 14.0], index=D[:8])
    res = buy_and_hold(close, D[1], D[6])
    assert list(res.equity.index) == list(D[1:7])
    # Over D1 .. D6 the closes are 11, 11 (carried), 12, 12.1, 12.1 (carried), 13; from 11.
    assert res.equity.tolist() == pytest.approx([1.0, 1.0, 12 / 11, 12.1 / 11, 12.1 / 11, 13 / 11])
    assert res.max_dd == 0.0
    # A window opening on a missing close starts from the last close before it.
    from_gap = buy_and_hold(close, D[2], D[3])
    assert from_gap.equity.tolist() == pytest.approx([1.0, 12 / 11])


def test_buy_and_hold_taken_is_empty_with_the_trade_columns():
    res = buy_and_hold(pd.Series([1.0, 2.0], index=D[:2]), D[0], D[1])
    assert res.taken.empty
    portfolio = _run([_trade("X", entry=None)], _flat(["X"], 50.0, 2))
    assert list(res.taken.columns) == list(portfolio.taken.columns) == [*TRADE_COLUMNS, "shares"]


def test_buy_and_hold_with_no_close_at_or_before_the_start_raises():
    close = pd.Series([math.nan, math.nan, 5.0], index=D[:3])
    with pytest.raises(ValueError, match="close"):
        buy_and_hold(close, D[0], D[2])


# ----------------------------------------------------------- the real trade table end to end

PANEL = synthetic_universe_panel(seed=5)
DATES = PANEL.dates()
LO, HI = DATES[300].date(), DATES[500].date()


def test_simulate_portfolio_on_a_synthetic_universe_respects_every_limit():
    params = BreakoutParams()
    ind = compute_indicators(PANEL)
    cands = candidate_history(PANEL, synthetic_kinds(PANEL), params, LO, HI)
    trades = simulate_candidates(PANEL, ind, cands, params, ExecutionParams(), delisted=set())
    settings = RiskSettings(equity=25_000.0)
    res = simulate_portfolio(trades, PANEL.close, settings, LO, HI)

    window = PANEL.close.loc[pd.Timestamp(LO) : pd.Timestamp(HI)].index
    assert list(res.equity.index) == list(window)
    assert res.equity.iloc[0] == 25_000.0 and (res.equity > 0).all()
    taken = res.taken
    assert len(taken) > 10
    assert list(taken.columns) == [*trades.columns, "shares"]
    assert taken.index.isin(trades.index).all() and (taken["shares"] > 0).all()
    assert taken["entry_date"].between(pd.Timestamp(LO), pd.Timestamp(HI)).all()
    assert 0.0 < res.exposure <= 1.0 and res.max_dd < 0 and math.isfinite(res.sharpe)

    # Recount the book each session from the taken table alone: slots, risk and the cash account.
    risk = taken["shares"] * (taken["entry_price"] - taken["initial_stop"])
    cost = taken["shares"] * taken["entry_price"]
    proceeds = taken["shares"] * taken["exit_price"]  # NaN while the trade is open
    prev = 25_000.0
    for day, equity in res.equity.items():
        held = (taken["entry_date"] <= day) & ~(taken["known_date"] <= day)
        entered_today = (taken["entry_date"] == day).any()
        assert held.sum() <= settings.max_positions
        assert taken.loc[held, "symbol"].is_unique  # one open position per symbol
        if entered_today:
            assert risk[held].sum() <= settings.max_total_risk_pct * prev + 1e-6
        bought = cost[taken["entry_date"] <= day].sum()
        sold = proceeds[taken["known_date"] <= day].sum()
        assert settings.equity - bought + sold >= -1e-6  # a cash account never goes negative
        prev = float(equity)

    spy = buy_and_hold(PANEL.close["SPY"], LO, HI)
    assert list(spy.equity.index) == list(window) and spy.equity.iloc[0] == 1.0
    assert list(spy.taken.columns) == list(taken.columns) and spy.exposure == 1.0
