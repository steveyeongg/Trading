"""Phase 1 fix: pipeline must surface *why* a candidate didn't publish —
no silent `None` returns. Covers the scoring gate path, the stale-data
veto, and the insufficient-bars path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from feature_engine import gbm_bars
from scoring_engine.signal import GateResult, generate_signal
from signal_service.pipeline import run_pipeline
from atlas_shared.schemas import AssetClass, Horizon


def test_insufficient_bars_carries_reason() -> None:
    short = gbm_bars(n=50, seed=1)
    result = run_pipeline(bars=short, symbol="X", generate_explanation=False)
    assert result.signal is None
    assert result.veto is None
    assert result.no_signal_reason is not None
    assert "insufficient bars" in result.no_signal_reason


def test_empty_bars_carries_reason() -> None:
    result = run_pipeline(bars=pd.DataFrame(), symbol="X", generate_explanation=False)
    assert result.no_signal_reason is not None
    assert "insufficient bars" in result.no_signal_reason


def test_stale_data_veto() -> None:
    """A 240-bar frame whose latest bar is 48h old must trip the swing-horizon
    stale-data veto (30h threshold)."""
    bars = gbm_bars(n=240, seed=3)
    # Shift the timestamp column 48h into the past.
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True) - pd.Timedelta(hours=48)
    result = run_pipeline(
        bars=bars,
        symbol="STALE",
        horizon=Horizon.SWING,
        generate_explanation=False,
    )
    assert result.signal is None
    assert result.no_signal_reason is not None
    assert "stale data" in result.no_signal_reason


def test_gate_failure_returns_reason_not_none() -> None:
    """generate_signal must return a GateResult (with a reason) when gates
    fail, never a bare None."""
    flat_features = {
        "rsi14": 50.0, "macd_hist": 0.0, "atr14": 1.0,
        "ema9": 100.0, "ema21": 100.0, "ema50": 100.0, "ema200": 100.0,
        "bb_pctb": 0.5, "adx": 10.0, "obv_slope_z": 0.0,
        "smc_bos": 0.0, "divergence_bull": 0.0, "divergence_bear": 0.0,
        "close": 100.0,
    }
    outcome = generate_signal(
        symbol="FLAT",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=flat_features,
        p_up=0.50,
    )
    assert isinstance(outcome, GateResult)
    assert outcome.reason  # non-empty
    assert "composite" in outcome.reason.lower() or "confirm" in outcome.reason.lower()
