"""Fixed-fractional position sizing and open-position overlap flags.

Sizing risks a fixed slice of equity on every idea. The slice is scaled by the market-regime
factor, divided by the distance from trigger to stop, and rounded down to whole shares. A
position-value cap then stops a tight stop from turning into an oversized position.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from swing_engine.config import RiskSettings

MIN_OVERLAP_RETURNS = 20
"""Fewest daily returns two symbols must share before their correlation counts."""


@dataclass(frozen=True)
class Sizing:
    """A sized trade idea.

    ``risk_dollars`` is the loss if price goes from trigger to stop and ``position_value`` is the
    cost at the trigger; both are 0.0 when ``shares`` is 0. ``capped_by`` is ``"max_position"``
    when the position-value cap (not the risk budget) set the share count, ``"no_risk"`` when
    the inputs gave no valid risk distance, and None otherwise.
    """

    shares: int
    risk_dollars: float
    position_value: float
    capped_by: str | None


@dataclass(frozen=True)
class Capacity:
    """How much of the book is in use: position slots and total open risk, in dollars."""

    open_positions: int
    max_positions: int
    open_risk_dollars: float
    max_risk_dollars: float


def _sized(shares: int, trigger: float, stop: float, capped_by: str | None) -> Sizing:
    return Sizing(
        shares=shares,
        risk_dollars=float(shares * (trigger - stop)),
        position_value=float(shares * trigger),
        capped_by=capped_by,
    )


def size_idea(trigger: float, stop: float, settings: RiskSettings, regime_factor: float) -> Sizing:
    """Shares = floor(equity * risk_pct * regime_factor / (trigger - stop)), capped by value.

    The cap is floor(equity * max_position_pct / trigger) shares. When it is lower than the risk
    share count the result is capped and ``capped_by`` is ``"max_position"``; a tie is not
    reported as capped. Never raises: a stop at or above the trigger, a non-positive trigger or
    any non-finite input gives 0 shares with ``capped_by="no_risk"``, and a regime factor of 0 or
    less (a downtrend: no trade, but nothing wrong with the stop) gives 0 shares with None.
    """
    risk_budget = settings.equity * settings.risk_pct * regime_factor
    position_cap = settings.equity * settings.max_position_pct
    finite = all(
        math.isfinite(x) for x in (trigger, stop, regime_factor, risk_budget, position_cap)
    )
    if not finite or trigger <= 0 or trigger <= stop:
        return _sized(0, 0.0, 0.0, "no_risk")
    if risk_budget <= 0:
        return _sized(0, 0.0, 0.0, None)

    by_risk = math.floor(risk_budget / (trigger - stop))
    by_value = max(0, math.floor(position_cap / trigger))
    if by_value < by_risk:
        return _sized(by_value, trigger, stop, "max_position")
    return _sized(by_risk, trigger, stop, None)


def overlap_flags(
    close: pd.DataFrame,
    as_of: pd.Timestamp,
    idea_symbols: Sequence[str],
    open_symbols: Sequence[str],
    window: int = 60,
    threshold: float = 0.8,
) -> dict[str, list[str]]:
    """Open positions each idea moves with, by daily-return correlation.

    Returns come from the close columns up to ``as_of`` with no fill, so a missing bar leaves
    NaN returns on either side of it. Only the last ``window`` returns are used, and each pair is
    correlated over the sessions both have a return for; a pair sharing fewer than
    ``MIN_OVERLAP_RETURNS`` is never flagged. A correlation strictly above ``threshold`` flags it.

    The result has one key per idea symbol, in order, mapping to the overlapping open symbols in
    ``open_symbols`` order. An idea is never compared with itself when it is also open, and a
    symbol absent from ``close`` gets (or contributes) no flag.
    """
    ideas = list(dict.fromkeys(idea_symbols))
    opens = list(dict.fromkeys(open_symbols))
    flags: dict[str, list[str]] = {sym: [] for sym in ideas}

    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert(None)
    columns = {str(c) for c in close.columns}
    present = [s for s in dict.fromkeys([*ideas, *opens]) if s in columns]
    if not present:
        return flags

    returns = close.loc[:cutoff, present].pct_change(fill_method=None).tail(window)
    corr = returns.corr(min_periods=MIN_OVERLAP_RETURNS)
    for sym in ideas:
        if sym not in corr.index:
            continue
        flags[sym] = [
            o for o in opens if o != sym and o in corr.columns and corr.at[sym, o] > threshold
        ]
    return flags
