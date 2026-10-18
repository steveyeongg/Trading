"""A portfolio run over a trade table with the live engine's risk rules, and the SPY benchmark.

:func:`simulate_portfolio` takes the trades a setup produced (from
:func:`~swing_engine.backtest.trades.simulate_candidates`) and plays them through one cash
account. Only entered trades count: those with an ``entry_date`` inside the window. A trade still
held when the window ends, or whose exit is not yet known by then, is marked to market through
the last session and never sold.

Each session ``d`` runs in this order:

1. Book the exits with ``known_date == d`` of positions held from earlier sessions: the cash comes
   in at ``exit_price`` and the slot and the open risk are freed. Capital is freed on
   ``known_date`` and not ``exit_date``, because a DELISTED exit is not known for ``MAX_GAP + 1``
   sessions after the bars stop (see :mod:`~swing_engine.backtest.trades`).
2. Take the entries with ``entry_date == d``, best ``rs_pct`` first and then by symbol. An entry
   is skipped first of all when its symbol is already held: one open position per symbol, so a
   fresh signal on a held name (or a second same-day signal on one name) adds nothing. A symbol
   booked out in step 1 is free again that same session. Every other entry is sized by
   :func:`~swing_engine.sizing.size_idea` on the equity marked at the previous close (the starting
   equity on the first session) and the regime factor of its candidate date. It is skipped when
   that is 0 shares (a downtrend, or a stop not under the entry). A cash account has no margin, so
   shares are then cut to ``floor(cash / entry_price)``, and the entry is skipped when that is 0.
   It is taken only when fewer than ``max_positions`` are held and the open risk after it, the sum
   of ``shares * (entry_price - initial_stop)`` over the held positions, stays within
   ``max_total_risk_pct`` of that same equity. A skipped entry never stops one behind it.
3. Book the exits of the positions just entered whose ``known_date == d`` (a same-day round trip).
   They held their slot and risk through step 2.
4. Mark to market at the close: equity is cash plus each held position at its symbol's last known
   close (a missing bar carries the previous close forward). Cash earns nothing.

Prices in the trade table already include slippage, and no commission is charged.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd
from backtest_service.metrics import max_drawdown, sharpe

from swing_engine.config import REGIME_FACTOR, RiskSettings
from swing_engine.execution import TradeOutcome
from swing_engine.setups import CANDIDATE_COLUMNS
from swing_engine.sizing import size_idea

TRADING_DAYS = 252

TAKEN_COLUMNS = [
    *CANDIDATE_COLUMNS,
    "regime",
    *(f.name for f in dataclasses.fields(TradeOutcome)),
    "known_date",
    "shares",
]
"""The columns of ``PortfolioResult.taken``: the trade table's, then the filled ``shares``."""

_REQUIRED = (
    "symbol",
    "rs_pct",
    "regime",
    "entry_date",
    "entry_price",
    "initial_stop",
    "exit_price",
    "known_date",
)


@dataclass(frozen=True)
class PortfolioResult:
    """A simulated account.

    ``equity`` is the marked equity at each session close of the window, indexed by session.
    ``taken`` is the accepted trades in the order they were accepted, with the table's own
    columns and index plus ``shares`` as actually filled (after the cash cap). ``cagr`` is
    ``(E_end / E_start) ** (252 / n_returns) - 1`` over the daily returns of ``equity``, and
    ``sharpe`` is the annualised Sharpe of those returns. ``max_dd`` is NEGATIVE (-0.25 is a 25%
    drawdown) and ``exposure`` is the mean over sessions of the value held divided by equity.
    """

    equity: pd.Series
    taken: pd.DataFrame
    cagr: float
    sharpe: float
    max_dd: float
    exposure: float


def _sessions(index: pd.Index, start: date, end: date) -> pd.DatetimeIndex:
    """The sessions of ``index`` in ``[start, end]``; raises when there is none."""
    sessions = pd.DatetimeIndex(index)
    if not sessions.is_monotonic_increasing:
        raise ValueError("close must be indexed by session dates in ascending order")
    sessions = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    if sessions.empty:
        raise ValueError(f"close has no session in [{start}, {end}]")
    return sessions


def _result(equity: pd.Series, taken: pd.DataFrame, exposure: float) -> PortfolioResult:
    """The metrics of an equity path, whose first value is its starting equity."""
    values = equity.to_numpy(dtype="float64")
    returns = values[1:] / values[:-1] - 1.0
    cagr = math.nan
    if returns.size and values[0] > 0:
        cagr = float((values[-1] / values[0]) ** (TRADING_DAYS / returns.size) - 1.0)
    return PortfolioResult(
        equity=equity,
        taken=taken,
        cagr=cagr,
        sharpe=sharpe(returns),
        max_dd=0.0 - max_drawdown(values),  # max_drawdown is a positive fraction; 0.0 - 0 is +0
        exposure=exposure,
    )


def _entered(trades: pd.DataFrame, lo: pd.Timestamp, hi: pd.Timestamp) -> pd.DataFrame:
    """The trades entered in ``[lo, hi]``, in the order a session considers them."""
    missing = [c for c in _REQUIRED if c not in trades.columns]
    if missing:
        raise ValueError(f"trades is missing columns: {missing}")
    entry = pd.to_datetime(trades["entry_date"])  # NaT (never filled) compares False below
    inside = ((entry >= lo) & (entry <= hi)).to_numpy(dtype=bool)
    entered = trades.loc[inside].sort_values(
        ["entry_date", "rs_pct", "symbol"], ascending=[True, False, True]
    )
    unknown = sorted(set(entered["regime"]) - set(REGIME_FACTOR))
    if unknown:
        raise ValueError(f"trades have regimes with no factor: {unknown}")
    return entered


def _marks(
    close: pd.DataFrame, symbols: pd.Series, sessions: pd.DatetimeIndex
) -> tuple[npt.NDArray[np.float64], list[int]]:
    """Closes over ``sessions`` for the traded symbols (last known close carried over NaN).

    Returns the matrix (sessions by distinct symbols) and each trade's column in it.
    """
    names = list(dict.fromkeys(symbols))
    absent = [s for s in names if s not in close.columns]
    if absent:
        raise ValueError(f"trades name symbols that are not in close: {absent}")
    filled = close.loc[close.index <= sessions[-1], names].ffill()
    marks = filled.reindex(sessions).to_numpy(dtype="float64")
    return marks, pd.Index(names).get_indexer(symbols).tolist()


def _book_exits(
    held: list[int], exit_row: list[int], shares: list[int], sale: list[float], d: int
) -> float:
    """Sell the held positions whose exit is known on session ``d``; the cash they bring in."""
    due = [k for k in held if exit_row[k] == d]
    for k in due:
        held.remove(k)
    return float(sum(shares[k] * sale[k] for k in due))


def simulate_portfolio(
    trades: pd.DataFrame,
    close: pd.DataFrame,
    settings: RiskSettings,
    start: date,
    end: date,
) -> PortfolioResult:
    """Run ``trades`` through one cash account over the sessions of ``close`` in ``[start, end]``.

    ``trades`` is a ``simulate_candidates`` table (it needs ``symbol``, ``rs_pct``, ``regime``,
    ``entry_date``, ``entry_price``, ``initial_stop``, ``exit_price`` and ``known_date``) and
    ``close`` is the panel's close frame. The module docstring gives the daily order. The account
    starts flat with ``settings.equity`` in cash, so a trade entered before ``start`` is ignored,
    not carried in. Equity is marked at every close, and the first mark is the first value of
    ``equity``: a position entered on the first session has its first-session move left out of
    the returns, which start from that mark.

    Raises ``ValueError`` when the window holds no session, a considered trade names a symbol
    missing from ``close`` or a regime with no factor, an ``entry_date`` or a ``known_date`` in
    the window is not a session of ``close``, or a required column is missing.
    """
    sessions = _sessions(close.index, start, end)
    entered = _entered(trades, pd.Timestamp(start), pd.Timestamp(end))
    n_days, n_trades = len(sessions), len(entered)

    entry_row = sessions.get_indexer(pd.DatetimeIndex(pd.to_datetime(entered["entry_date"])))
    if (entry_row < 0).any():
        raise ValueError("every entry_date must be a session of close")
    known = pd.DatetimeIndex(pd.to_datetime(entered["known_date"]))
    due = np.asarray(known <= pd.Timestamp(end), dtype=bool)  # NaT: not (yet) known
    exit_arr = np.full(n_trades, n_days, dtype=np.intp)  # n_days: never booked in the window
    exit_arr[due] = sessions.get_indexer(known[due])
    if (exit_arr < 0).any():
        raise ValueError("every known_date in the window must be a session of close")

    marks, col = _marks(close, entered["symbol"], sessions)
    entry_day = entry_row.tolist()
    exit_row = exit_arr.tolist()
    price = entered["entry_price"].to_numpy(dtype="float64").tolist()
    stop = entered["initial_stop"].to_numpy(dtype="float64").tolist()
    sale = entered["exit_price"].to_numpy(dtype="float64").tolist()
    factor = [REGIME_FACTOR[r] for r in entered["regime"]]

    shares = [0] * n_trades
    held: list[int] = []
    accepted: list[int] = []
    equity = np.empty(n_days)
    invested = np.empty(n_days)
    cash = settings.equity
    nxt = 0
    for d in range(n_days):
        marked = float(equity[d - 1]) if d else settings.equity
        cash += _book_exits(held, exit_row, shares, sale, d)
        sizing = dataclasses.replace(settings, equity=marked)
        while nxt < n_trades and entry_day[nxt] == d:
            j, nxt = nxt, nxt + 1
            if any(col[k] == col[j] for k in held):  # one open position per symbol
                continue
            if len(held) >= settings.max_positions:
                continue
            fit = size_idea(price[j], stop[j], sizing, factor[j]).shares
            if fit == 0:
                continue
            n = min(fit, math.floor(cash / price[j]))  # no margin: only what the cash buys
            if n == 0:
                continue
            open_risk = sum(shares[k] * (price[k] - stop[k]) for k in held)
            if open_risk + n * (price[j] - stop[j]) > settings.max_total_risk_pct * marked:
                continue
            cash -= n * price[j]
            shares[j] = n
            held.append(j)
            accepted.append(j)
        cash += _book_exits(held, exit_row, shares, sale, d)
        value = float(sum(shares[k] * marks[d, col[k]] for k in held))
        invested[d] = value
        equity[d] = cash + value

    taken = entered.iloc[accepted].assign(
        shares=np.array([shares[j] for j in accepted], dtype="int64")
    )
    return _result(
        pd.Series(equity, index=sessions, name="equity"),
        taken,
        float(np.mean(invested / equity)),
    )


def buy_and_hold(close: pd.Series, start: date, end: date) -> PortfolioResult:
    """Buying at the close of the first session in ``[start, end]`` and holding to the last.

    ``equity`` is ``close`` divided by that first close, so it starts at 1.0. A missing close
    carries the previous one forward, including from before ``start``. The metrics have the
    definitions of :func:`simulate_portfolio`, ``exposure`` is 1.0 and ``taken`` is empty.
    Raises ``ValueError`` when the window holds no session or there is no close at or before its
    first session.
    """
    sessions = _sessions(close.index, start, end)
    prices = close.ffill().loc[sessions]
    base = float(prices.iloc[0])
    if math.isnan(base):
        raise ValueError(f"close has no price at or before {sessions[0].date()}")
    equity = (prices / base).rename("equity")
    return _result(equity, pd.DataFrame(columns=TAKEN_COLUMNS), 1.0)
