"""ATLAS backtest service."""

from backtest_service.simulator import cost_sensitivity, run_backtest
from backtest_service.strategies import AtlasStrategy, Order, Strategy, TrendFollower
from backtest_service.types import (
    BacktestConfig,
    BacktestResult,
    ExitReason,
    Trade,
    TradeSide,
)
from backtest_service.walk_forward import WalkForwardSpec, walk_forward

__all__ = [
    "AtlasStrategy",
    "BacktestConfig",
    "BacktestResult",
    "ExitReason",
    "Order",
    "Strategy",
    "Trade",
    "TradeSide",
    "TrendFollower",
    "WalkForwardSpec",
    "cost_sensitivity",
    "run_backtest",
    "walk_forward",
]
