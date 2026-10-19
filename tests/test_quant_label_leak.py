"""Regression: triple-barrier labels must be computed per symbol.

`load_features_for_training` stacks symbols sorted by (ts, symbol). Labelling that
interleaved series made each row's "next 20 bars" other tickers' prices, so the
model learned the price LEVEL of each ticker instead of anything about the future
(deployed v1 reported an out-of-fold AUC of 0.89). On pure random walks, which
contain nothing to learn, the leaky method scored about 0.93.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from feature_engine import compute_features, gbm_bars
from quant_engine import binary_up_labels, train_trend, triple_barrier_labels
from quant_engine.train import build_training_set

# Start prices span two orders of magnitude, like a real watchlist (penny stock to NVDA-ish).
START_PRICES = [5, 40, 100, 230, 450, 900]
HORIZON = 20


def _stacked_random_walk_features(prices: list[float], n: int) -> pd.DataFrame:
    """One driftless random walk per price, all sharing timestamps, stacked (ts, symbol)."""
    frames = []
    for i, s0 in enumerate(prices):
        bars = gbm_bars(n=n, seed=100 + i, s0=float(s0), mu=0.0, symbol=f"RW{i}")
        frames.append(compute_features(bars))
    return pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"]).reset_index(drop=True)


@pytest.fixture(scope="module")
def random_walk_features() -> pd.DataFrame:
    return _stacked_random_walk_features(START_PRICES, n=4000)


def test_random_walks_score_near_chance_with_per_symbol_labels(
    random_walk_features: pd.DataFrame,
) -> None:
    x, y, h = build_training_set(random_walk_features, horizon=HORIZON, pt_mult=2.0, sl_mult=1.0)
    assert h == HORIZON * len(START_PRICES)

    art = train_trend(x, y, label_horizon=h)

    assert art.metrics["oof_auc"] < 0.65
    assert art.label_scheme == "per_symbol_v2"


def test_old_interleaved_labelling_scored_high_on_the_same_random_walks(
    random_walk_features: pd.DataFrame,
) -> None:
    """Proves the leak existed: the old method 'learns' random walks, the new one does not."""
    feats = random_walk_features
    old_labels = binary_up_labels(
        triple_barrier_labels(feats["close"], feats["atr14"], horizon_bars=HORIZON)
    )
    old = train_trend(feats, old_labels, label_horizon=HORIZON)

    assert old.metrics["oof_auc"] > 0.80


def _two_symbol_frame(n: int = 300) -> pd.DataFrame:
    frames = [
        compute_features(gbm_bars(n=n, seed=11, s0=10.0, symbol="LOW")),
        compute_features(gbm_bars(n=n, seed=12, s0=800.0, symbol="HIGH")),
    ]
    # Deliberately NOT pre-sorted so build_training_set has to do the stacking.
    return pd.concat(frames[::-1], ignore_index=True)


def test_each_symbols_labels_equal_labelling_that_symbol_alone() -> None:
    frame = _two_symbol_frame()

    x, y, _ = build_training_set(frame, horizon=HORIZON, pt_mult=2.0, sl_mult=1.0)

    for sym in ("LOW", "HIGH"):
        own = frame[frame["symbol"] == sym].sort_values("ts").reset_index(drop=True)
        expected = triple_barrier_labels(own["close"], own["atr14"], horizon_bars=HORIZON)
        expected_up = (expected == 1).astype(float).where(expected.notna())
        got = y[x["symbol"] == sym]
        np.testing.assert_array_equal(got.to_numpy(), expected_up.to_numpy())


def test_training_set_is_stacked_ts_then_symbol_with_aligned_labels() -> None:
    frame = _two_symbol_frame()

    x, y, h = build_training_set(frame, horizon=HORIZON, pt_mult=2.0, sl_mult=1.0)

    assert h == HORIZON * 2
    assert len(x) == len(y) == len(frame)
    assert x.index.equals(y.index)
    keys = list(zip(x["ts"], x["symbol"], strict=True))
    assert keys == sorted(keys)


def test_unlabelled_tail_is_nan_not_zero() -> None:
    """The last `horizon` rows of each symbol have no outcome yet; they must be dropped
    from training, not silently taught as 'did not go up'."""
    frame = _two_symbol_frame()

    x, y, _ = build_training_set(frame, horizon=HORIZON, pt_mult=2.0, sl_mult=1.0)

    for sym in ("LOW", "HIGH"):
        tail = y[x["symbol"] == sym].iloc[-HORIZON:]
        assert tail.isna().all()


def test_single_series_without_symbol_column_is_labelled_directly() -> None:
    feats = compute_features(gbm_bars(n=400, seed=5)).drop(columns=["symbol"])

    x, y, h = build_training_set(feats, horizon=HORIZON, pt_mult=2.0, sl_mult=1.0)

    assert h == HORIZON
    expected = triple_barrier_labels(feats["close"], feats["atr14"], horizon_bars=HORIZON)
    expected_up = (expected == 1).astype(float).where(expected.notna())
    np.testing.assert_array_equal(y.to_numpy(), expected_up.to_numpy())
    assert len(x) == len(feats)
