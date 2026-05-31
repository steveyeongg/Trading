"""Indicator pipeline smoke + sanity tests."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from feature_engine import INDICATORS, compute_features, gbm_bars


@pytest.fixture
def bars() -> pd.DataFrame:
    return gbm_bars(n=400, seed=42)


def test_compute_features_returns_all_columns(bars: pd.DataFrame) -> None:
    out = compute_features(bars)
    for col in INDICATORS:
        assert col in out.columns, f"missing {col}"
    assert len(out) == len(bars)


def test_compute_features_tail_is_finite(bars: pd.DataFrame) -> None:
    """After enough lookback, every indicator should produce a real number."""
    out = compute_features(bars).tail(50)
    numeric_cols = [c for c in INDICATORS if c not in {"divergence_bull", "divergence_bear", "smc_bos"}]
    bad = {c: out[c].isna().sum() for c in numeric_cols if out[c].isna().any()}
    # ichimoku_span_b has the longest lookback (52). After 400 bars, last 50 must be clean.
    assert not bad, f"unexpected NaNs in tail: {bad}"


def test_rsi_bounds(bars: pd.DataFrame) -> None:
    rsi = compute_features(bars)["rsi14"].dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()


def test_bb_ordering(bars: pd.DataFrame) -> None:
    out = compute_features(bars).dropna(subset=["bb_lower", "bb_middle", "bb_upper"])
    assert (out["bb_lower"] <= out["bb_middle"]).all()
    assert (out["bb_middle"] <= out["bb_upper"]).all()


def test_ema_recovers_constant_series() -> None:
    """An EMA of a flat price = the same flat price (within fp tolerance)."""
    df = pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=300, freq="1min", tz="UTC"),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1000.0,
        }
    )
    out = compute_features(df)
    assert math.isclose(out["ema9"].iloc[-1], 100.0, abs_tol=1e-6)
    assert math.isclose(out["ema200"].iloc[-1], 100.0, abs_tol=1e-6)


def test_missing_column_raises() -> None:
    bad = pd.DataFrame({"close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="missing required columns"):
        compute_features(bad)


def test_empty_passthrough() -> None:
    out = compute_features(pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]))
    assert out.empty


def test_atr_positive_on_volatile_series(bars: pd.DataFrame) -> None:
    atr = compute_features(bars)["atr14"].dropna()
    assert (atr > 0).all()


def test_smc_bos_in_set(bars: pd.DataFrame) -> None:
    bos = compute_features(bars)["smc_bos"]
    assert set(bos.unique()).issubset({-1, 0, 1, np.int8(-1), np.int8(0), np.int8(1)})
