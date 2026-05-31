"""CLI for ad-hoc backtests.

Examples:
    # Synthetic single-symbol replay with the trained registry model.
    uv run python -m backtest_service.run --synthetic --n-bars 3000

    # Cost sensitivity sweep.
    uv run python -m backtest_service.run --synthetic --n-bars 3000 --cost-sensitivity

    # Walk-forward on synthetic.
    uv run python -m backtest_service.run --synthetic --n-bars 5000 --walk-forward
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from atlas_shared.logging import get_logger, setup_logging
from feature_engine import gbm_bars
from quant_engine.trend import TrendModel

from backtest_service.simulator import cost_sensitivity, run_backtest
from backtest_service.strategies import AtlasStrategy, TrendFollower
from backtest_service.walk_forward import WalkForwardSpec, walk_forward

setup_logging()
log = get_logger("backtest.cli")

DEFAULT_MODEL = Path("ml/registry/trend/v0.joblib")


def _load_model() -> TrendModel | None:
    if not DEFAULT_MODEL.exists():
        log.warning("backtest.no_model", path=str(DEFAULT_MODEL))
        return None
    return TrendModel.load(DEFAULT_MODEL)


def _pretty(metrics: dict) -> str:
    keys = (
        "n_trades", "hit_rate", "expectancy_per_trade",
        "total_return", "cagr", "max_drawdown",
        "sharpe", "sharpe_ci_lo", "sharpe_ci_hi",
        "sortino", "calmar", "profit_factor", "avg_bars_held",
    )
    lines = []
    for k in keys:
        v = metrics.get(k)
        if v is None:
            continue
        lines.append(f"  {k:<24s} {v:>10.4f}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--synthetic", action="store_true", default=True)
    p.add_argument("--n-bars", type=int, default=3000)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--cost-sensitivity", action="store_true")
    p.add_argument("--walk-forward", action="store_true")
    p.add_argument("--strategy", choices=["atlas", "trend-follower"], default="atlas")
    p.add_argument("--out", type=str, default="ml/backtests/latest.json")
    args = p.parse_args()

    bars = gbm_bars(n=args.n_bars, seed=args.seed, symbol="SYN")
    if args.strategy == "trend-follower":
        strategy = TrendFollower()
    else:
        model = _load_model()
        strategy = AtlasStrategy(trend_model=model)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    if args.walk_forward:
        results = walk_forward(bars, spec=WalkForwardSpec(train_bars=1500, test_bars=500, refit_every=500))
        log.info("walk_forward.folds", n=len(results))
        for i, r in enumerate(results):
            print(f"\n=== fold {i} ===")
            print(_pretty(r.metrics))
        Path(args.out).write_text(json.dumps([r.metrics for r in results], indent=2, default=str))
        return

    if args.cost_sensitivity:
        results = cost_sensitivity({"SYN": bars}, strategy)
        for mult, r in results.items():
            print(f"\n=== cost multiplier {mult}x ===")
            print(_pretty(r.metrics))
        Path(args.out).write_text(
            json.dumps({str(k): v.metrics for k, v in results.items()}, indent=2, default=str)
        )
        return

    result = run_backtest({"SYN": bars}, strategy)
    print(_pretty(result.metrics))
    Path(args.out).write_text(json.dumps(result.metrics, indent=2, default=str))
    log.info("backtest.saved", path=args.out)


if __name__ == "__main__":
    main()
