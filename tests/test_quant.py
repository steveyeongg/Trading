"""Quant-engine smoke tests on synthetic data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from feature_engine import compute_features, gbm_bars
from quant_engine import (
    binary_up_labels,
    train_trend,
    triple_barrier_labels,
    walk_forward_purged,
)
from quant_engine.trend import TrendModel


@pytest.fixture(scope="module")
def features() -> pd.DataFrame:
    return compute_features(gbm_bars(n=2500, seed=123))


def test_triple_barrier_label_shape(features: pd.DataFrame) -> None:
    labels = triple_barrier_labels(features["close"], features["atr14"], horizon_bars=20)
    # last `horizon_bars` should be NaN.
    assert labels.iloc[-20:].isna().all()
    # mid-series values must be in {-1, 0, 1}
    mid = labels.dropna()
    assert set(mid.unique()).issubset({-1.0, 0.0, 1.0})


def test_binary_up_labels_balance(features: pd.DataFrame) -> None:
    labels = binary_up_labels(triple_barrier_labels(features["close"], features["atr14"]))
    # On a GBM with positive drift, we should see *some* positives.
    assert labels.sum() > 0


def test_walk_forward_purge_no_overlap() -> None:
    splits = list(walk_forward_purged(n=2000, n_splits=4, test_size=200, embargo=10, label_horizon=20))
    assert len(splits) == 4
    for s in splits:
        assert s.train.max() < s.test.min(), "train must precede test"
        # Purge gap = label_horizon.
        assert s.test.min() - s.train.max() >= 20


def test_train_trend_runs(features: pd.DataFrame, tmp_path) -> None:
    labels = binary_up_labels(triple_barrier_labels(features["close"], features["atr14"]))
    art = train_trend(features, labels, version="test-v0", n_splits=4, test_size=250)
    assert art.metrics["n_folds"] >= 2
    # AUC should be a number even if not great on random walk.
    assert np.isfinite(art.metrics["oof_brier"])

    # Round-trip serialization.
    from quant_engine.trend import save
    path = save(art, tmp_path / "model.joblib")
    loaded = TrendModel.load(path)
    p = loaded.predict_proba(features.iloc[[-1]])
    assert 0.0 <= p <= 1.0
