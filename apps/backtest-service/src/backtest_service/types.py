"""Backtest dataclasses. Kept dependency-free so other services can import."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TradeSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"
    EOD = "eod"
    MANUAL = "manual"


@dataclass
class Trade:
    symbol: str
    side: TradeSide
    entry_ts: datetime
    entry_price: float
    qty: float
    stop_price: float
    targets: list[float]
    target_allocations: list[float]
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    realized_pnl: float = 0.0
    realized_return: float = 0.0
    max_run_up: float = 0.0
    max_drawdown: float = 0.0
    bars_held: int = 0
    fees_paid: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.entry_price


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_per_unit: float = 0.0005   # 5 bps of notional
    slippage_bps: float = 1.0             # 1 bp on entry & exit
    impact_coef: float = 0.0              # 0 = no market impact; >0 = coef * (qty/ADV)
    max_open_positions: int = 5
    time_stop_bars: int = 60              # cancel if not at +0.5R in this many bars
    target_allocations: tuple[float, ...] = (0.40, 0.40, 0.20)
    cost_multiplier: float = 1.0          # sensitivity: rerun at 0/1/2x


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade]
    equity_curve: list[tuple[datetime, float]]   # [(ts, equity), ...]
    metrics: dict[str, float]
    bars_processed: int = 0
