"""Walk-forward backtest harness: train on a rolling window, test on the
next out-of-sample slice, refit at fixed cadence. BLUEPRINT §11.2.

Distinct from `quant_engine.cv`: that splits *features+labels* for model
selection; this splits *bars* for strategy-level evaluation that includes the
risk engine and execution costs.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from feature_engine import compute_features
from quant_engine import binary_up_labels, train_trend, triple_barrier_labels
from quant_engine.trend import TrendModel

from backtest_service.simulator import run_backtest
from backtest_service.strategies.atlas import AtlasStrategy
from backtest_service.types import BacktestConfig, BacktestResult


@dataclass
class WalkForwardSpec:
    train_bars: int = 1500
    test_bars: int = 500
    refit_every: int = 500     # bars between refits
    label_horizon: int = 20


def walk_forward(
    bars: pd.DataFrame,
    spec: WalkForwardSpec | None = None,
    config: BacktestConfig | None = None,
    version_prefix: str = "wf",
) -> list[BacktestResult]:
    """Iterate over the bar series, retraining the trend model at each
    refit boundary. Returns one BacktestResult per OOS slice."""
    spec = spec or WalkForwardSpec()
    cfg = config or BacktestConfig()
    if len(bars) < spec.train_bars + spec.test_bars:
        raise ValueError(
            f"need >= {spec.train_bars + spec.test_bars} bars; got {len(bars)}"
        )

    results: list[BacktestResult] = []
    cursor = spec.train_bars
    fold = 0

    while cursor + spec.test_bars <= len(bars):
        train_slice = bars.iloc[: cursor]
        test_slice = bars.iloc[cursor : cursor + spec.test_bars]
        feats = compute_features(train_slice)
        labels = binary_up_labels(triple_barrier_labels(feats["close"], feats["atr14"], horizon_bars=spec.label_horizon))

        try:
            artifact = train_trend(
                feats, labels,
                version=f"{version_prefix}-fold{fold:03d}",
                label_horizon=spec.label_horizon,
                n_splits=4,
                test_size=max(100, spec.train_bars // 6),
            )
            model = TrendModel(artifact)
        except (ValueError, RuntimeError):
            # Degenerate fold (e.g. single-class labels) — skip rather than crash.
            cursor += spec.refit_every
            fold += 1
            continue

        strategy = AtlasStrategy(trend_model=model)
        result = run_backtest({test_slice["symbol"].iloc[0]: test_slice}, strategy, config=cfg)
        results.append(result)

        cursor += spec.refit_every
        fold += 1

    return results
