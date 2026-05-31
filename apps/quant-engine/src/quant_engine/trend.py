"""Trend model: XGBoost binary classifier + isotonic calibration.

The calibrated probability is what becomes user-facing confidence; see
BLUEPRINT §7.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from xgboost import XGBClassifier

from quant_engine.cv import walk_forward_purged

# Features the trend model consumes. Must be present in the feature frame.
FEATURE_COLS: list[str] = [
    "rsi14",
    "macd",
    "macd_hist",
    "bb_pctb",
    "ema9",
    "ema21",
    "ema50",
    "ema200",
    "atr14",
    "adx",
    "di_plus",
    "di_minus",
    "obv_slope_z",
    "stoch_k",
    "stoch_d",
    "vol_realized_20",
    "smc_bos",
    "divergence_bull",
    "divergence_bear",
]


@dataclass
class TrendArtifact:
    """Serializable bundle. One file = one model version."""

    model: XGBClassifier
    calibrator: IsotonicRegression
    feature_cols: list[str]
    version: str
    metrics: dict[str, float] = field(default_factory=dict)


class TrendModel:
    """Wrapper around the artifact for inference."""

    def __init__(self, artifact: TrendArtifact):
        self.artifact = artifact

    @property
    def version(self) -> str:
        return self.artifact.version

    def predict_proba(self, features: dict[str, float] | pd.DataFrame) -> float:
        """Return calibrated P(up) for a single feature vector or the last row
        of a feature frame."""
        row = pd.DataFrame([features]) if isinstance(features, dict) else features.tail(1)
        x = row[self.artifact.feature_cols].to_numpy(dtype=float)
        # Replace NaN with column medians to be robust on the live path.
        col_med = np.nanmedian(x, axis=0)
        x = np.where(np.isnan(x), col_med, x)
        raw = self.artifact.model.predict_proba(x)[:, 1]
        cal = self.artifact.calibrator.predict(raw)
        return float(np.clip(cal[0], 0.0, 1.0))

    @classmethod
    def load(cls, path: str | Path) -> TrendModel:
        artifact: TrendArtifact = joblib.load(path)
        return cls(artifact)


def _xy(features: pd.DataFrame, labels: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    df = features[FEATURE_COLS].copy()
    df["__y"] = labels.values
    df = df.dropna()
    return df[FEATURE_COLS], df["__y"].astype(int)


def train_trend(
    features: pd.DataFrame,
    labels: pd.Series,
    version: str = "v0",
    n_splits: int = 5,
    test_size: int = 200,
    embargo: int = 10,
    label_horizon: int = 20,
    xgb_params: dict[str, Any] | None = None,
) -> TrendArtifact:
    """Walk-forward train, then refit on the full set and fit the calibrator
    on out-of-fold predictions (best-practice — avoids the calibrator seeing
    in-fold targets).
    """
    X, y = _xy(features, labels)
    if len(X) < 5 * test_size:
        raise ValueError(
            f"need at least {5 * test_size} clean rows; got {len(X)}. "
            "Synthesize more bars or relax the horizon."
        )

    params = {
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "tree_method": "hist",
        "random_state": 7,
        **(xgb_params or {}),
    }

    oof_p = np.full(len(X), np.nan)
    oof_y = np.full(len(X), np.nan)
    fold_metrics: list[dict[str, float]] = []

    for split in walk_forward_purged(
        n=len(X),
        n_splits=n_splits,
        test_size=test_size,
        embargo=embargo,
        label_horizon=label_horizon,
    ):
        Xtr = X.iloc[split.train].to_numpy(dtype=float)
        ytr = y.iloc[split.train].to_numpy(dtype=int)
        Xte = X.iloc[split.test].to_numpy(dtype=float)
        yte = y.iloc[split.test].to_numpy(dtype=int)
        # If a fold's training set is degenerate (only one class), skip it.
        if len(np.unique(ytr)) < 2:
            continue
        model = XGBClassifier(**params)
        model.fit(Xtr, ytr)
        p = model.predict_proba(Xte)[:, 1]
        oof_p[split.test] = p
        oof_y[split.test] = yte
        try:
            auc = float(roc_auc_score(yte, p))
        except ValueError:
            auc = float("nan")
        fold_metrics.append({"auc": auc, "n_test": float(len(yte))})

    mask = ~np.isnan(oof_p)
    if mask.sum() < 50:
        raise RuntimeError("too few OOF predictions to calibrate")

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(oof_p[mask], oof_y[mask])

    # Refit on everything for the deployed weights.
    final = XGBClassifier(**params)
    final.fit(X.to_numpy(dtype=float), y.to_numpy(dtype=int))

    metrics = {
        "oof_auc": float(roc_auc_score(oof_y[mask], oof_p[mask])) if mask.sum() > 1 else float("nan"),
        "oof_brier": float(brier_score_loss(oof_y[mask], calibrator.predict(oof_p[mask]))),
        "n_folds": float(len(fold_metrics)),
        "n_train_total": float(len(X)),
    }

    return TrendArtifact(
        model=final,
        calibrator=calibrator,
        feature_cols=FEATURE_COLS,
        version=version,
        metrics=metrics,
    )


def save(artifact: TrendArtifact, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    return path
