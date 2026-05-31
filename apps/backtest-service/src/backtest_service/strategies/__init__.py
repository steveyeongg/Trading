"""Pluggable backtest strategies."""

from backtest_service.strategies.atlas import AtlasStrategy
from backtest_service.strategies.base import Order, Strategy
from backtest_service.strategies.trend_follower import TrendFollower

__all__ = ["AtlasStrategy", "Order", "Strategy", "TrendFollower"]
