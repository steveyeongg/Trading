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


def build_training_set(
    features: pd.DataFrame,
    horizon: int,
    pt_mult: float,
    sl_mult: float,
) -> tuple[pd.DataFrame, pd.Series, int]:
    """Label each symbol on its OWN prices, then stack in (ts, symbol) order.

    Triple-barrier labels look `horizon` bars ahead, so they must be computed on one
    symbol's contiguous series. Labelling the (ts, symbol)-stacked frame makes each
    row's "next 20 bars" other tickers' prices; the model then learns the price level
    of each ticker and scores ~0.9 AUC even on pure random walks.

    Returns `(features, labels, label_horizon_rows)`:
      - `features` is stacked sorted by (ts, symbol) when a `symbol` column exists;
        a frame without one is a single series and is returned as given.
      - `labels` is 1.0 / 0.0 (upper barrier hit first / not), aligned to `features`.
        NaN where the outcome is unknown (ATR warm-up, or the last `horizon` bars of a
        symbol), so `train_trend` drops those rows rather than teaching them as "down".
      - `label_horizon_rows = horizon * n_symbols`: in the stacked frame one symbol's
        `horizon`-bar label window spans that many rows, which is the purge gap the
        walk-forward CV needs.
    """
    if "symbol" not in features.columns:
        return features, _up_labels(features, horizon, pt_mult, sl_mult), horizon

    stacked = features.sort_values(["ts", "symbol"]).reset_index(drop=True)
    # Within a symbol the stacked order is chronological, so each group is that symbol's series.
    per_symbol = [
        _up_labels(group, horizon, pt_mult, sl_mult) for _, group in stacked.groupby("symbol")
    ]
    labels = pd.concat(per_symbol).reindex(stacked.index).rename("label")
    return stacked, labels, horizon * stacked["symbol"].nunique()


def _up_labels(frame: pd.DataFrame, horizon: int, pt_mult: float, sl_mult: float) -> pd.Series:
    tb = triple_barrier_labels(
        frame["close"], frame["atr14"], horizon_bars=horizon, pt_mult=pt_mult, sl_mult=sl_mult
    )
    return binary_up_labels(tb).astype(float).where(tb.notna())


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

    feats, labels, label_horizon = build_training_set(
        feats, horizon=args.horizon, pt_mult=args.pt_mult, sl_mult=args.sl_mult
    )

    artifact = train_trend(feats, labels, version=args.version, label_horizon=label_horizon)
    log.info("train.metrics", **artifact.metrics)

    out_path = Path(args.out) / f"{args.version}.joblib"
    save(artifact, out_path)
    log.info("train.saved", path=str(out_path))


if __name__ == "__main__":
    main()
