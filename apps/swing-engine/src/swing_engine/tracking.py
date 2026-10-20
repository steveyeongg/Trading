"""Forward tracking of published ideas, and the live / reconstructed rule.

Every idea the nightly scan publishes is a buy-stop order for the next session. :func:`track_ideas`
finds out what became of each one by re-simulating it from its signal session through the
context's session with :func:`~swing_engine.execution.simulate_trade`, the same fill and exit
model the backtests use, with the setup's own ``trail_atr`` and ``max_hold``
(:func:`~swing_engine.validation.run.params_for`) and the default fill assumptions. Nothing is
kept between runs: the result is a pure function of the idea and the bars so far, so running it
every night (or twice a night) gives the same rows, and a trade that has closed reads the same
from any later session. The live track record therefore builds up with no effort from the user.

An idea counts as *live* only if it was published before the open of its target session
(:func:`is_live`). One published after the fact (a catch-up scan after the Mac was asleep) is
*reconstructed*: it is tracked like any other, but :func:`live_stats` keeps its record apart from
the live one, so a hindsight idea never inflates the live statistics. Watch-only ideas
(parked while the market is in a downtrend) are tracked too but belong to neither record.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from atlas_shared.logging import get_logger

from swing_engine.backtest.stats import trade_stats
from swing_engine.backtest.trades import PanelBars, known_date_for
from swing_engine.config import ExecutionParams
from swing_engine.execution import TrackStatus, TradeOutcome, simulate_trade
from swing_engine.scan import ScanContext
from swing_engine.validation.run import params_for

log = get_logger("swing.tracking")

_ET = ZoneInfo("America/New_York")
_OPEN = time(9, 30)


def is_live(published_at: datetime, target_session: date) -> bool:
    """True iff ``published_at`` is before 09:30 New York time on ``target_session``.

    That is the regular open, whatever the day's close time, so a half day changes nothing. The
    comparison is in absolute time, so it holds across the DST switch. ``published_at`` must be
    timezone-aware (``ValueError`` otherwise): a naive time has no instant to compare.
    """
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError(f"published_at must be timezone-aware, got a naive time: {published_at}")
    opens = datetime.combine(target_session, _OPEN, tzinfo=_ET)
    return published_at.astimezone(UTC) < opens.astimezone(UTC)


def _float(value: float | None) -> float | None:
    return None if value is None else float(value)


def _row(idea_id: Any, outcome: TradeOutcome | None, known: date | None) -> dict[str, Any]:
    """The tracking columns of one idea; ``outcome=None`` is an idea with nothing to track yet."""
    if outcome is None:
        return {
            "id": idea_id,
            "track_status": TrackStatus.PENDING.value,
            "entry_date": None,
            "entry_price": None,
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
            "known_date": None,
            "r_multiple": None,
            "current_stop": None,
        }
    return {
        "id": idea_id,
        "track_status": outcome.status.value,
        "entry_date": outcome.entry_date,
        "entry_price": _float(outcome.entry_price),
        "exit_date": outcome.exit_date,
        "exit_price": _float(outcome.exit_price),
        "exit_reason": outcome.exit_reason.value if outcome.exit_reason is not None else None,
        "known_date": known,
        "r_multiple": _float(outcome.r_multiple),
        "current_stop": _float(outcome.current_stop),
    }


def track_ideas(
    ideas: Sequence[Mapping[str, Any]],
    ctx: ScanContext,
    delisted: AbstractSet[str],
    repo: Path,
) -> list[dict[str, Any]]:
    """Where each published idea stands as of ``ctx.session``: one row per idea, in input order.

    An idea needs ``id, session, symbol, setup, version, trigger, stop`` (``session`` is its
    signal date). Each row has the idea's ``id`` and the tracking columns ``track_status``
    (a :class:`~swing_engine.execution.TrackStatus` value), ``entry_date, entry_price, exit_date,
    exit_price, exit_reason``, ``known_date`` (when the exit became knowable; see
    :func:`~swing_engine.backtest.trades.known_date_for`), ``r_multiple`` and ``current_stop``;
    a field that does not apply yet is None. The input is not changed.

    ``delisted`` is the set of symbols whose asset status is inactive: an open trade in one
    closes as DELISTED at its last priced close. ``repo`` is where the pre-registered parameters
    are read from.

    Bars are the panel's: every symbol's frame keeps its NaN rows, because ``simulate_trade``
    finds a halt or a segment break from them. Each symbol's frame is built once however many
    ideas it has. An idea that cannot be tracked (its symbol is not in the panel or has no bar
    on the signal date, the signal date is not a panel session, or the idea is not one the model
    accepts) stays ``pending`` and is logged, and never stops the others.
    """
    bars = PanelBars(ctx.panel, ctx.ind)
    frames: dict[int, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    for idea in ideas:
        idea_id = idea["id"]
        symbol = str(idea["symbol"])
        signal = pd.Timestamp(idea["session"]).normalize()
        at = bars.index.get_indexer([signal])[0]
        col = bars.columns.get_indexer([symbol])[0]
        if col < 0 or at < 0 or math.isnan(bars.close[at, col]):
            log.debug("tracking.no_bar", id=str(idea_id), symbol=symbol, session=str(signal.date()))
            rows.append(_row(idea_id, None, None))
            continue
        if col not in frames:
            frames[col] = bars.frame(col)
        try:
            params = params_for(str(idea["setup"]), idea.get("version"), repo)
            outcome = simulate_trade(
                frames[col],
                signal,
                float(idea["trigger"]),
                float(idea["stop"]),
                params.trail_atr,
                params.max_hold,
                ExecutionParams(),
                delisted=symbol in delisted,
            )
        except ValueError as exc:
            log.warning("tracking.rejected", id=str(idea_id), symbol=symbol, error=str(exc))
            rows.append(_row(idea_id, None, None))
            continue
        known = known_date_for(bars.index, outcome.exit_date, outcome.exit_reason)
        rows.append(_row(idea_id, outcome, known))
    return rows


def live_stats(rows: Sequence[Mapping[str, Any]], reconstructed: bool) -> dict[str, Any]:
    """The track record of the live ideas (``reconstructed=False``) or of the reconstructed ones.

    ``rows`` are stored ideas with their tracking columns (``symbol, track_status, entry_date,
    known_date, r_multiple, watch_only, reconstructed``). Watch-only ideas are left out of both
    records, and so is every idea of the other kind. What is left goes through
    :func:`~swing_engine.backtest.stats.trade_stats` exactly as a backtest's trade table does, so
    ``n`` counts the closed trades and ``n_distinct`` the closed trades entered while no earlier
    trade on the same symbol was still open: an open trade, which is in the table though it is
    not a result yet, blocks a later entry on its symbol just as it does in a backtest. The dict
    also has ``open``, how many of these ideas are filled and still open.

    The stored ideas do not carry ``sessions_held``, so ``avg_hold`` is NaN unless the rows
    do. With no closed trade ``n`` and ``n_distinct`` are 0 and every other statistic is NaN.
    """
    mine = [
        row
        for row in rows
        if not row.get("watch_only") and bool(row.get("reconstructed")) == reconstructed
    ]
    trades = pd.DataFrame(
        {
            "symbol": pd.Series([str(row["symbol"]) for row in mine], dtype=object),
            "status": pd.Series([row["track_status"] for row in mine], dtype=object),
            "entry_date": pd.to_datetime(
                pd.Series([row.get("entry_date") for row in mine], dtype=object)
            ),
            "known_date": pd.to_datetime(
                pd.Series([row.get("known_date") for row in mine], dtype=object)
            ),
            "r_multiple": pd.Series([row.get("r_multiple") for row in mine], dtype="float64"),
            "sessions_held": pd.Series([row.get("sessions_held") for row in mine], dtype="float64"),
        }
    )
    stats = trade_stats(trades)
    stats["open"] = int((trades["status"] == TrackStatus.OPEN.value).sum())
    return stats
