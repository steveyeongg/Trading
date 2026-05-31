"""Strategy interface — minimal contract every backtestable strategy honours."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from backtest_service.types import TradeSide


@dataclass(frozen=True)
class Order:
    """A proposed new trade. The simulator turns this into a `Trade` with
    fills applied."""

    symbol: str
    side: TradeSide
    qty: float                   # in units (shares / coins / contracts)
    entry_price: float           # signal limit; simulator clips by slippage
    stop_price: float
    targets: list[float]
    target_allocations: list[float] = field(default_factory=lambda: [0.40, 0.40, 0.20])
    notes: dict = field(default_factory=dict)


class Strategy(Protocol):
    """Strategies are pure: they see the bar history, return zero/one orders."""

    name: str

    def on_bar(
        self,
        symbol: str,
        bars: pd.DataFrame,
        open_positions: dict[str, object],
        equity: float,
    ) -> Order | None:
        """Called once per bar per symbol.

        - `bars` is the full history up to and including the current bar.
        - `open_positions` keyed by symbol — strategy can skip if already in.
        - Return None to do nothing.
        """
        ...
