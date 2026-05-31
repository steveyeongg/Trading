"""Synthetic OHLCV generator. Used by tests + offline dev so the pipeline
runs without external data providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd


def gbm_bars(
    n: int = 500,
    start: datetime | None = None,
    seed: int = 0,
    s0: float = 100.0,
    mu: float = 0.10,
    sigma: float = 0.25,
    bar_seconds: int = 60,
    symbol: str = "SYN",
) -> pd.DataFrame:
    """Generate `n` OHLCV bars from a geometric Brownian motion.

    Deterministic per (seed, n). Returns columns:
        ts, symbol, resolution, open, high, low, close, volume.
    """
    rng = np.random.default_rng(seed)
    dt = bar_seconds / (252 * 6.5 * 3600)  # bar fraction of trading-year
    # Generate close path.
    shocks = rng.standard_normal(n)
    drift = (mu - 0.5 * sigma**2) * dt
    diffusion = sigma * np.sqrt(dt) * shocks
    log_close = np.log(s0) + np.cumsum(drift + diffusion)
    close = np.exp(log_close)

    # Build OHLC around close with plausible intra-bar excursion.
    rng2 = np.random.default_rng(seed + 1)
    bar_range = np.abs(rng2.standard_normal(n)) * sigma * np.sqrt(dt) * close
    open_ = np.empty(n)
    open_[0] = s0
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + bar_range / 2
    low = np.minimum(open_, close) - bar_range / 2
    volume = rng2.integers(low=1_000, high=10_000, size=n).astype(float)

    if start is None:
        start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    ts = [start + timedelta(seconds=bar_seconds * i) for i in range(n)]

    return pd.DataFrame(
        {
            "ts": ts,
            "symbol": symbol,
            "resolution": f"{bar_seconds}s" if bar_seconds < 60 else f"{bar_seconds // 60}m",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
