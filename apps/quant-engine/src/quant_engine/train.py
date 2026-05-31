"""CLI: train the trend model.

Phase 1 entrypoint. Sources of training data, in priority order:
  1. Postgres `bars` table — when populated.
  2. Synthetic GBM bars — fallback for offline dev.

Usage:
    uv run python -m quant_engine.train --symbols AAPL,MSFT --version v1
    uv run python -m quant_engine.train --synthetic --n-bars 3000 --version v0
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from atlas_shared.logging import get_logger, setup_logging
from feature_engine import compute_features, gbm_bars

from quant_engine.data import load_features_for_training
from quant_engine.labels import binary_up_labels, triple_barrier_labels
from quant_engine.trend import save, train_trend

setup_logging()
log = get_logger("quant.train")

DEFAULT_MODEL_DIR = Path("ml/registry/trend")


def _synthetic_dataset(n_bars: int, seed: int) -> pd.DataFrame:
    bars = gbm_bars(n=n_bars, seed=seed)
    feats = compute_features(bars)
    return feats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--synthetic", action="store_true", help="Use synthetic GBM bars.")
    p.add_argument("--n-bars", type=int, default=3000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--symbols", type=str, default="", help="Postgres path: comma-separated symbols.")
    p.add_argument("--resolution", type=str, default="1m")
    p.add_argument("--days", type=int, default=180, help="Lookback window for Postgres path.")
    p.add_argument("--horizon", type=int, default=20)
    p.add_argument("--pt-mult", type=float, default=2.0)
    p.add_argument("--sl-mult", type=float, default=1.0)
    p.add_argument("--version", type=str, default="v0")
    p.add_argument("--out", type=str, default=str(DEFAULT_MODEL_DIR))
    args = p.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        end = datetime.now(UTC)
        start = end - timedelta(days=args.days)
        log.info("train.dataset", source="postgres", symbols=symbols, resolution=args.resolution, days=args.days)
        feats = load_features_for_training(symbols, resolution=args.resolution, start=start, end=end)
        if feats.empty:
            raise SystemExit(
                "No bars returned from Postgres. Ingest bars first:\n"
                f"  uv run python -m ingest_equities backfill --symbols {args.symbols} --days {args.days}"
            )
    else:
        log.info("train.dataset", source="synthetic", n_bars=args.n_bars)
        feats = _synthetic_dataset(args.n_bars, args.seed)

    labels_tb = triple_barrier_labels(
        feats["close"],
        feats["atr14"],
        horizon_bars=args.horizon,
        pt_mult=args.pt_mult,
        sl_mult=args.sl_mult,
    )
    labels = binary_up_labels(labels_tb)

    artifact = train_trend(feats, labels, version=args.version, label_horizon=args.horizon)
    log.info("train.metrics", **artifact.metrics)

    out_path = Path(args.out) / f"{args.version}.joblib"
    save(artifact, out_path)
    log.info("train.saved", path=str(out_path))


if __name__ == "__main__":
    main()
