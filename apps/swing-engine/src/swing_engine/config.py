"""Frozen, versioned parameter sets for the swing engine.

Every default here is a documented rule parameter. Changing one means a new
``version`` string on the setup it belongs to, so stored results stay traceable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseFilter:
    """Which symbols are tradable on a given scan day."""

    min_price: float = 10.0
    min_median_dollar_volume: float = 20_000_000.0
    dollar_volume_window: int = 50
    min_history_sessions: int = 252
    max_gap_sessions: int = 10


@dataclass(frozen=True)
class LeaderParams:
    """Relative-strength leader gate shared by both setups."""

    rs_min_pct: float = 70.0
    within_high_pct: float = 0.25
    sma200_rising_lookback: int = 20


@dataclass(frozen=True)
class BreakoutParams:
    """Volatility-contraction breakout near the 50-session high."""

    version: str = "breakout-v1"
    near_high_pct: float = 0.02
    high_window: int = 50
    contraction_max: float = 0.85
    trigger_atr: float = 0.10
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    max_hold: int = 30


@dataclass(frozen=True)
class PullbackParams:
    """Short-term oversold pullback inside a leader's uptrend."""

    version: str = "pullback-v1"
    rsi2_max: float = 10.0
    ema_window: int = 20
    trigger_atr: float = 0.05
    low_window: int = 5
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    trail_atr: float = 3.0
    max_hold: int = 30


@dataclass(frozen=True)
class ExecutionParams:
    """Fill assumptions applied when simulating entries and exits."""

    slippage_bps: float = 10.0
    max_gap_atr: float = 1.0


@dataclass(frozen=True)
class RiskSettings:
    """Account-level sizing and exposure limits."""

    equity: float = 25_000.0
    risk_pct: float = 0.01
    max_position_pct: float = 0.20
    max_positions: int = 8
    max_total_risk_pct: float = 0.06
    overlap_corr: float = 0.80
    overlap_window: int = 60


REGIME_FACTOR: dict[str, float] = {"uptrend": 1.0, "mixed": 0.5, "downtrend": 0.0}
TOP_N_DEFAULT = 20

SetupParams = BreakoutParams | PullbackParams
