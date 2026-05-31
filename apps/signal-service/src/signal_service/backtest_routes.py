"""Backtest endpoint.

Runs synchronously inside FastAPI's threadpool (the route is a plain `def`,
so Starlette offloads it — the event loop stays responsive). Synthetic data
runs sub-second; real multi-symbol/multi-year backtests should move to the
job-queue model (Phase 3) rather than block an HTTP request.
"""

from __future__ import annotations

from typing import Literal

from backtest_service import (
    AtlasStrategy,
    BacktestConfig,
    TrendFollower,
    cost_sensitivity,
    run_backtest,
)
from fastapi import APIRouter
from feature_engine import gbm_bars
from pydantic import BaseModel, Field

from signal_service.state import get_trend_model

router = APIRouter(prefix="/v1")


class BacktestRequest(BaseModel):
    symbol: str = "SYN"
    strategy: Literal["trend-follower", "atlas"] = "trend-follower"
    synthetic: bool = True
    n_bars: int = Field(default=3000, ge=300, le=20000)
    seed: int = 11
    initial_capital: float = 100_000.0
    cost_multiplier: float = Field(default=1.0, ge=0.0, le=5.0)
    cost_sweep: bool = False


class BacktestResponse(BaseModel):
    symbol: str
    strategy: str
    metrics: dict[str, float]
    equity_curve: list[list[float]]          # [[epoch_seconds, equity], ...]
    n_trades: int
    cost_sweep: dict[str, dict[str, float]] | None = None


def _equity_to_pairs(equity_curve: list[tuple]) -> list[list[float]]:
    out: list[list[float]] = []
    for ts, eq in equity_curve:
        out.append([float(ts.timestamp()), float(eq)])
    return out


@router.post("/backtests", response_model=BacktestResponse)
def run_backtest_endpoint(req: BacktestRequest) -> BacktestResponse:
    # Phase 2.5: synthetic only via the API. Real-bar backtests run through
    # the CLI against Postgres until the job-queue model lands.
    bars = gbm_bars(n=req.n_bars, seed=req.seed, symbol=req.symbol.upper())

    if req.strategy == "atlas":
        strategy = AtlasStrategy(trend_model=get_trend_model())
    else:
        strategy = TrendFollower()

    cfg = BacktestConfig(initial_capital=req.initial_capital, cost_multiplier=req.cost_multiplier)
    result = run_backtest({req.symbol.upper(): bars}, strategy, config=cfg)

    sweep: dict[str, dict[str, float]] | None = None
    if req.cost_sweep:
        sweep_results = cost_sensitivity({req.symbol.upper(): bars}, strategy, multipliers=(0.0, 1.0, 2.0))
        sweep = {str(k): v.metrics for k, v in sweep_results.items()}

    return BacktestResponse(
        symbol=req.symbol.upper(),
        strategy=req.strategy,
        metrics=result.metrics,
        equity_curve=_equity_to_pairs(result.equity_curve),
        n_trades=len(result.trades),
        cost_sweep=sweep,
    )
