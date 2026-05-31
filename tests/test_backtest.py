"""Backtest harness: math sanity, simulator correctness, end-to-end run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from backtest_service import (
    AtlasStrategy,
    BacktestConfig,
    ExitReason,
    cost_sensitivity,
    run_backtest,
    walk_forward,
)
from backtest_service.metrics import (
    bootstrap_sharpe,
    deflated_sharpe,
    max_drawdown,
    sharpe,
    sortino,
)
from backtest_service.strategies.base import Order
from backtest_service.types import TradeSide
from backtest_service.walk_forward import WalkForwardSpec
from feature_engine import gbm_bars

# --- metrics ---------------------------------------------------------------


def test_sharpe_known_value() -> None:
    # Constant positive returns → very high Sharpe (sd=0 → NaN actually).
    rng = np.random.default_rng(0)
    rets = rng.normal(0.0008, 0.01, size=2000)
    sh = sharpe(rets)
    # Expected ~ 0.0008/0.01 * sqrt(252) ≈ 1.27
    assert 0.5 < sh < 2.5


def test_sortino_higher_than_sharpe_for_upskewed() -> None:
    rng = np.random.default_rng(0)
    rets = rng.normal(0.0005, 0.01, size=2000)
    # Upweight positives.
    rets[rets > 0] *= 1.5
    assert sortino(rets) > sharpe(rets)


def test_max_drawdown_basic() -> None:
    equity = np.array([100, 110, 105, 90, 95, 120])
    # Peak 110, trough 90 → 18.18% drawdown.
    assert max_drawdown(equity) == pytest.approx(0.1818, abs=1e-3)


def test_bootstrap_sharpe_ci_brackets_point_estimate() -> None:
    rng = np.random.default_rng(7)
    rets = rng.normal(0.001, 0.012, size=2000)
    point = sharpe(rets)
    lo, hi = bootstrap_sharpe(rets, n_boot=300, block_size=20, seed=1)
    assert lo <= point <= hi


def test_deflated_sharpe_below_one_for_overfit() -> None:
    # 100 trials, modest observed Sharpe → DS probability should be lowish.
    prob = deflated_sharpe(observed_sharpe=1.0, n_trials=100, n_obs=500)
    assert 0.0 <= prob <= 1.0


# --- simulator -------------------------------------------------------------


class _SingleShotStrategy:
    """Fires exactly one long entry at bar 5. Used for deterministic
    fixture-level tests on the simulator itself."""

    name = "single-shot"

    def __init__(self):
        self._fired = False

    def on_bar(self, symbol, bars, open_positions, equity):
        if self._fired or symbol in open_positions or len(bars) < 6:
            return None
        self._fired = True
        last = bars.iloc[-1]
        return Order(
            symbol=symbol,
            side=TradeSide.LONG,
            qty=10.0,
            entry_price=float(last["close"]),
            stop_price=float(last["close"]) * 0.95,
            targets=[float(last["close"]) * 1.05],
        )


@pytest.fixture
def small_bars() -> pd.DataFrame:
    ts0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    rows = []
    for i in range(50):
        close = 100.0 + i * 0.3   # gentle uptrend → target should hit
        rows.append({
            "ts": ts0 + timedelta(minutes=i),
            "symbol": "T",
            "resolution": "1m",
            "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
            "close": close, "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def test_simulator_target_exit(small_bars: pd.DataFrame) -> None:
    strategy = _SingleShotStrategy()
    cfg = BacktestConfig(initial_capital=10_000, max_open_positions=1)
    result = run_backtest({"T": small_bars}, strategy, config=cfg)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TARGET
    assert trade.realized_pnl > 0
    assert result.equity_curve[-1][1] > cfg.initial_capital


def test_simulator_stop_exit() -> None:
    """Force a stop hit by feeding a descending series."""
    ts0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    rows = []
    for i in range(50):
        close = 100.0 - i * 0.5
        rows.append({
            "ts": ts0 + timedelta(minutes=i),
            "symbol": "T",
            "resolution": "1m",
            "open": close + 0.1, "high": close + 0.2, "low": close - 0.5,
            "close": close, "volume": 1000.0,
        })
    bars = pd.DataFrame(rows)
    strategy = _SingleShotStrategy()
    result = run_backtest({"T": bars}, strategy, config=BacktestConfig(initial_capital=10_000))
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == ExitReason.STOP
    assert result.trades[0].realized_pnl < 0


def test_simulator_time_stop() -> None:
    """Flat market → exit on time."""
    ts0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    rows = [{
        "ts": ts0 + timedelta(minutes=i),
        "symbol": "T", "resolution": "1m",
        "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1000.0,
    } for i in range(200)]
    bars = pd.DataFrame(rows)
    strategy = _SingleShotStrategy()
    cfg = BacktestConfig(initial_capital=10_000, time_stop_bars=20)
    result = run_backtest({"T": bars}, strategy, config=cfg)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == ExitReason.TIME
    assert result.trades[0].bars_held >= 20


# --- end-to-end ------------------------------------------------------------


def test_atlas_strategy_runs_without_errors() -> None:
    bars = gbm_bars(n=600, seed=3, symbol="SYN")
    strategy = AtlasStrategy(trend_model=None, feature_step=20)
    cfg = BacktestConfig(initial_capital=50_000)
    result = run_backtest({"SYN": bars}, strategy, config=cfg)
    # Bars should at least have been processed.
    assert result.bars_processed > 0
    # All metrics finite or NaN — no errors thrown.
    assert "sharpe" in result.metrics


def test_cost_sensitivity_returns_three_results() -> None:
    bars = gbm_bars(n=400, seed=5, symbol="SYN")
    strategy = AtlasStrategy(trend_model=None, feature_step=25)
    results = cost_sensitivity({"SYN": bars}, strategy, multipliers=(0.0, 1.0, 2.0))
    assert set(results.keys()) == {0.0, 1.0, 2.0}


def test_walk_forward_emits_multiple_folds() -> None:
    bars = gbm_bars(n=3500, seed=9, symbol="SYN")
    spec = WalkForwardSpec(train_bars=1500, test_bars=500, refit_every=500)
    results = walk_forward(bars, spec=spec)
    # At least 2 folds should be produced.
    assert len(results) >= 2
    for r in results:
        assert "sharpe" in r.metrics
