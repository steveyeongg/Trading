"""Synthetic-data helpers shared by the swing engine tests."""

from __future__ import annotations

import socket
from collections.abc import Sequence

import numpy as np
import pandas as pd

from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel, build_panel

_PANEL_FIELDS = ("open", "high", "low", "close", "volume")


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


def synthetic_universe_panel(seed: int, n_symbols: int = 40, n_sessions: int = 600) -> Panel:
    """SPY plus ``n_symbols`` trending stocks (``S00``, ``S01``, ...) on consecutive business days.

    Built to pass the default universe filter once 252 sessions of history exist (prices stay
    far above $10 for any realistic path, and dollar volume sits near $60M to $180M a day) and to
    leave a scan day around session 450 with relative-strength leaders and a mix of setups. Each
    stock has its own drift and volatility, and its volatility switches between calm and wild
    stretches, which gives the breakout setup contractions to find. SPY drifts up gently, so its
    regime is an uptrend by then. ``raw_close`` equals the adjusted close (no splits happened).
    """
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-02", periods=n_sessions)
    names = ["SPY", *(f"S{i:02d}" for i in range(n_symbols))]
    frames = {f: np.empty((n_sessions, len(names))) for f in _PANEL_FIELDS}

    for j, name in enumerate(names):
        if name == "SPY":
            drift, base_vol, start = 0.0006, 0.006, 300.0
        else:
            drift = rng.uniform(-0.0003, 0.0030)
            base_vol = rng.uniform(0.012, 0.025)
            start = rng.uniform(40.0, 200.0)
        # Volatility moves between calm and wild stretches of 30 to 70 sessions.
        scale = np.empty(n_sessions)
        lo = 0
        while lo < n_sessions:
            hi = lo + int(rng.integers(30, 71))
            scale[lo:hi] = rng.choice([0.5, 1.0, 1.6])
            lo = hi
        rets = drift + base_vol * scale * rng.standard_normal(n_sessions)
        close = start * np.cumprod(1.0 + rets)
        open_ = np.concatenate([[start], close[:-1]]) * (
            1.0 + 0.003 * rng.standard_normal(n_sessions)
        )
        spread = base_vol * scale * np.abs(rng.standard_normal(n_sessions))
        frames["open"][:, j] = open_
        frames["close"][:, j] = close
        frames["high"][:, j] = np.maximum(open_, close) * (1.0 + 0.5 * spread)
        frames["low"][:, j] = np.minimum(open_, close) * (1.0 - 0.5 * spread)
        frames["volume"][:, j] = (
            rng.uniform(60e6, 180e6) / close * rng.lognormal(0.0, 0.2, n_sessions)
        )

    built = {f: pd.DataFrame(a, index=index, columns=names) for f, a in frames.items()}
    return Panel(**built, raw_close=built["close"].copy())


def synthetic_kinds(panel: Panel) -> dict[str, AssetKind]:
    """Every symbol is common stock except SPY, the regime reference, which is an ETF."""
    return {s: AssetKind.ETF if s == "SPY" else AssetKind.COMMON for s in panel.symbols()}


def scramble_after(panel: Panel, as_of: pd.Timestamp | str, seed: int) -> Panel:
    """A copy of ``panel`` with every row after ``as_of`` replaced by random positive numbers.

    All of open, high, low, close, volume and (when present) ``raw_close`` change, NaN cells
    included. Rows up to and including ``as_of`` are untouched, so a computation that answers
    differently on the copy has looked at the future.
    """
    rng = np.random.default_rng(seed)
    cutoff = pd.Timestamp(as_of)
    out: dict[str, pd.DataFrame | None] = {}
    for name in (*_PANEL_FIELDS, "raw_close"):
        frame = getattr(panel, name)
        if frame is None:
            out[name] = None
            continue
        copy = frame.copy()
        future = copy.index > cutoff
        copy.loc[future, :] = rng.uniform(1.0, 1000.0, size=(int(future.sum()), copy.shape[1]))
        out[name] = copy
    return Panel(**out)


def postgres_reachable() -> bool:
    """True when something accepts TCP on 127.0.0.1:5432 (used to skip DB tests)."""
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=0.25):
            return True
    except OSError:
        return False
