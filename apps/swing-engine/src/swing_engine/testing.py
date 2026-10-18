"""Synthetic-data helpers shared by the swing engine tests."""

from __future__ import annotations

import socket
from collections.abc import Sequence

import numpy as np
import pandas as pd

from swing_engine.panel import Panel, build_panel


def make_bars(
    symbol: str,
    closes: Sequence[float],
    start: str = "2020-01-02",
    range_pct: float = 0.01,
    volume: float = 2_000_000,
) -> pd.DataFrame:
    """Long daily bars on consecutive business days.

    ``ts`` is the session date at 00:00 UTC (the storage convention). ``open`` is the
    prior close (the first bar opens at its own close); ``high``/``low`` extend the
    open-close range by ``range_pct``.
    """
    close = np.asarray(closes, dtype="float64")
    open_ = np.concatenate([close[:1], close[:-1]])
    ts = pd.bdate_range(start, periods=len(close)).tz_localize("UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "symbol": symbol,
            "open": open_,
            "high": np.maximum(open_, close) * (1.0 + range_pct),
            "low": np.minimum(open_, close) * (1.0 - range_pct),
            "close": close,
            "volume": float(volume),
        }
    )


def make_panel(series: dict[str, Sequence[float]], **kw: float | str) -> Panel:
    """A Panel built from per-symbol close lists (``kw`` goes to ``make_bars``)."""
    return build_panel(pd.concat([make_bars(sym, closes, **kw) for sym, closes in series.items()]))


def trend_series(
    n: int, start: float = 100.0, drift: float = 0.001, vol: float = 0.01, seed: int = 0
) -> list[float]:
    """A reproducible drifting random-walk close series of length ``n``."""
    rets = drift + vol * np.random.default_rng(seed).standard_normal(n)
    return (start * np.cumprod(1.0 + rets)).tolist()


def postgres_reachable() -> bool:
    """True when something accepts TCP on 127.0.0.1:5432 (used to skip DB tests)."""
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=0.25):
            return True
    except OSError:
        return False
