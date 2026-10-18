"""The trade list: every candidate a setup produced, run through the one fill and exit model.

:func:`candidate_history` collects candidates over a window of past sessions, and
:func:`simulate_candidates` runs each through :func:`swing_engine.execution.simulate_trade`, so a
backtested fill can never differ from what nightly tracking would have done.

Each symbol's bars are built once as a panel-aligned frame (``open, high, low, close`` from the
panel and ``atr14`` from the indicators), NaN rows included: ``simulate_trade`` finds segment
breaks from runs of NaN, so a row is never dropped.

``known_date`` is when a trade's exit became knowable, and it is what a portfolio simulation
should free capital on. An ordinary exit is known on its own session. A DELISTED exit is not: the
bars simply stop, and nothing says so until ``MAX_GAP + 1`` more panel sessions have passed
without a bar. So its ``known_date`` is the panel session ``MAX_GAP + 1`` rows after the exit,
capped at the last panel date.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import date

import numpy as np
import numpy.typing as npt
import pandas as pd

from swing_engine.config import ExecutionParams, SetupParams, UniverseFilter
from swing_engine.data.assets import AssetKind
from swing_engine.execution import ExitReason, TradeOutcome, simulate_trade
from swing_engine.indicators import MAX_GAP, Indicators
from swing_engine.panel import Panel
from swing_engine.regime import Regime
from swing_engine.scan import build_context
from swing_engine.setups import candidates_for


def candidate_history(
    panel: Panel,
    kinds: Mapping[str, AssetKind],
    params: SetupParams,
    start: date,
    end: date,
    universe: UniverseFilter = UniverseFilter(),  # noqa: B008  (frozen dataclass)
) -> pd.DataFrame:
    """Every candidate ``params`` produced on a session in ``[start, end]``, with its regime.

    The columns are :data:`~swing_engine.setups.CANDIDATE_COLUMNS` plus ``regime``: SPY's
    regime on the candidate's date as a plain string (``uptrend``, ``mixed`` or ``downtrend``).
    The market context is built once over the panel up to ``end``. That equals building it on
    each date's own truncation, because every component is causal. Raises
    :class:`~swing_engine.scan.ScanError` when SPY has no bar on ``end``, as a scan would.
    """
    ctx = build_context(panel, kinds, end, universe)
    found = candidates_for(ctx.panel, ctx.ind, ctx.leaders, ctx.rs_pct, params)
    inside = (found["date"] >= pd.Timestamp(start)) & (found["date"] <= pd.Timestamp(end))
    found = found.loc[inside].reset_index(drop=True)
    regime = ctx.regime.map(lambda r: Regime(r).value)
    found["regime"] = regime.reindex(pd.DatetimeIndex(found["date"])).to_numpy(dtype=object)
    return found


class PanelBars:
    """A panel's prices and ATR as arrays, handing out one ``simulate_trade`` frame per symbol.

    The frames share the panel's index object, so its lookup table is built once. Hold a frame
    only while its symbol is being simulated: a full universe of them is a lot of memory.
    """

    def __init__(self, panel: Panel, ind: Indicators) -> None:
        close = panel.close
        atr = ind.atr14
        if not (atr.index.equals(close.index) and atr.columns.equals(close.columns)):
            raise ValueError(
                "ind.atr14 must have the panel's dates and symbols (compute it on the same panel)"
            )
        self.index: pd.DatetimeIndex = pd.DatetimeIndex(close.index)
        self.columns: pd.Index = close.columns
        self.open = panel.open.to_numpy(dtype="float64")
        self.high = panel.high.to_numpy(dtype="float64")
        self.low = panel.low.to_numpy(dtype="float64")
        self.close = close.to_numpy(dtype="float64")
        self.atr = atr.to_numpy(dtype="float64")

    def frame(self, col: int) -> pd.DataFrame:
        """The symbol at column ``col``: every panel row, NaN where it has no bar."""
        return pd.DataFrame(
            {
                "open": self.open[:, col],
                "high": self.high[:, col],
                "low": self.low[:, col],
                "close": self.close[:, col],
                "atr14": self.atr[:, col],
            },
            index=self.index,
        )


def positions_by_key(keys: npt.NDArray[np.intp]) -> list[npt.NDArray[np.intp]]:
    """Positions of each distinct key, in ascending key order and input order within a key."""
    if keys.size == 0:
        return []
    order = np.argsort(keys, kind="stable")
    return np.split(order, np.flatnonzero(np.diff(keys[order])) + 1)


def _symbol_columns(columns: pd.Index, symbols: npt.NDArray[np.object_]) -> npt.NDArray[np.intp]:
    """Panel column position of each candidate symbol; an unknown symbol raises."""
    cols: npt.NDArray[np.intp] = columns.get_indexer(symbols)
    if (cols < 0).any():
        missing = sorted({str(s) for s in symbols[cols < 0]})
        raise ValueError(f"candidates name symbols that are not in the panel: {missing}")
    return cols


def _dates(values: list[date | None]) -> npt.NDArray[np.datetime64]:
    """Session dates as ``datetime64[ns]``, with NaT for None."""
    dates: npt.NDArray[np.datetime64] = pd.to_datetime(pd.Series(values, dtype=object)).to_numpy(
        dtype="datetime64[ns]"
    )
    return dates


def _floats(values: list[float | None]) -> npt.NDArray[np.float64]:
    return np.array(values, dtype="float64")  # None becomes NaN


def _known_dates(
    index: pd.DatetimeIndex, exit_date: npt.NDArray[np.datetime64], reason: list[str | None]
) -> npt.NDArray[np.datetime64]:
    """``exit_date``, except a DELISTED exit waits ``MAX_GAP + 1`` panel rows (capped at the end)."""
    known = exit_date.copy()
    delisted = np.array([r == ExitReason.DELISTED for r in reason], dtype=bool)
    if delisted.any():
        pos = index.get_indexer(pd.DatetimeIndex(exit_date[delisted]))
        later = np.minimum(pos + MAX_GAP + 1, len(index) - 1)
        known[delisted] = index.to_numpy(dtype="datetime64[ns]")[later]
    return known


def simulate_candidates(
    panel: Panel,
    ind: Indicators,
    candidates: pd.DataFrame,
    params: SetupParams,
    ex: ExecutionParams,
    delisted: AbstractSet[str],
) -> pd.DataFrame:
    """Run every candidate through ``simulate_trade``: one row per candidate, in input order.

    The result is the candidate frame (every column, its index kept) followed by the
    :class:`~swing_engine.execution.TradeOutcome` fields (``status`` through ``highest_close``) and
    ``known_date`` (see the module docstring), NaT while the trade is not closed. Dates are
    ``datetime64[ns]`` (NaT for none), prices are floats (NaN for none), and ``status`` and
    ``exit_reason`` are plain strings.

    ``delisted`` is the set of symbols whose asset status is inactive: their bars end because
    they stopped trading, so an open trade at the end of the data closes as ``delisted``.
    ``ind`` must be computed on ``panel`` (same dates and symbols), and the trades run to the end
    of ``panel``, so pass the full panel, not one cut at the window's end.
    """
    bars = PanelBars(panel, ind)
    n = len(candidates)
    signal_dates = pd.DatetimeIndex(candidates["date"]).tolist()
    triggers = candidates["trigger"].to_numpy(dtype="float64").tolist()
    stops = candidates["stop"].to_numpy(dtype="float64").tolist()
    cols = _symbol_columns(bars.columns, candidates["symbol"].to_numpy(dtype=object))

    results: dict[int, TradeOutcome] = {}
    for group in positions_by_key(cols):  # one frame per symbol, candidates in input order
        col = int(cols[group[0]])
        frame = bars.frame(col)
        is_delisted = str(bars.columns[col]) in delisted
        for i in group.tolist():
            results[i] = simulate_trade(
                frame,
                signal_dates[i],
                triggers[i],
                stops[i],
                params.trail_atr,
                params.max_hold,
                ex,
                delisted=is_delisted,
            )

    done = [results[i] for i in range(n)]
    reason: list[str | None] = [o.exit_reason.value if o.exit_reason else None for o in done]
    exit_date = _dates([o.exit_date for o in done])
    columns: dict[str, object] = {
        "status": [o.status.value for o in done],
        "entry_date": _dates([o.entry_date for o in done]),
        "entry_price": _floats([o.entry_price for o in done]),
        "initial_stop": _floats([o.initial_stop for o in done]),
        "exit_date": exit_date,
        "exit_price": _floats([o.exit_price for o in done]),
        "exit_reason": reason,
        "r_multiple": _floats([o.r_multiple for o in done]),
        "sessions_held": np.array([o.sessions_held for o in done], dtype="int64"),
        "current_stop": _floats([o.current_stop for o in done]),
        "highest_close": _floats([o.highest_close for o in done]),
        "known_date": _known_dates(bars.index, exit_date, reason),
    }
    return candidates.assign(**columns)
