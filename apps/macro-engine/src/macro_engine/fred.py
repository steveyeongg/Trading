"""FRED (Federal Reserve Economic Data) client.

Free with API key registration. Falls back to a synthetic stub generator
when no key is configured so offline dev / CI keeps working.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
from atlas_shared.logging import get_logger
from tenacity import retry, stop_after_attempt, wait_exponential

log = get_logger("macro.fred")
BASE = "https://api.stlouisfed.org/fred"


# Core macro series (FRED IDs) consumed by the regime classifier.
SERIES = {
    "T10Y2Y": "T10Y2Y",        # 10Y-2Y term spread (recession leading indicator)
    "T10Y3M": "T10Y3M",        # 10Y-3M term spread
    "DGS10": "DGS10",          # 10Y treasury yield
    "DGS2": "DGS2",            # 2Y treasury yield
    "DGS3MO": "DGS3MO",        # 3M treasury yield
    "DTWEXBGS": "DTWEXBGS",    # broad USD index
    "VIXCLS": "VIXCLS",        # VIX
    "UNRATE": "UNRATE",        # unemployment
    "CPIAUCSL": "CPIAUCSL",    # CPI
}


class FredError(RuntimeError):
    pass


class FredClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        self._client = httpx.AsyncClient(timeout=timeout, base_url=BASE)

    async def __aenter__(self) -> FredClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @property
    def available(self) -> bool:
        return self.api_key is not None

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _series_raw(self, series_id: str, start: datetime, end: datetime) -> dict:
        if not self.available:
            raise FredError("FRED_API_KEY not configured")
        r = await self._client.get(
            "/series/observations",
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "observation_start": start.date().isoformat(),
                "observation_end": end.date().isoformat(),
            },
        )
        r.raise_for_status()
        return r.json()

    async def series(self, series_id: str, days: int = 365) -> pd.Series:
        """Return a date-indexed pd.Series for the requested FRED series."""
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        if not self.available:
            return _synthetic_series(series_id, start, end)
        payload = await self._series_raw(series_id, start, end)
        obs = payload.get("observations", [])
        rows = [(o["date"], o["value"]) for o in obs if o["value"] not in (".", "")]
        if not rows:
            return pd.Series(dtype=float, name=series_id)
        idx = pd.to_datetime([r[0] for r in rows], utc=True)
        vals = pd.to_numeric([r[1] for r in rows], errors="coerce")
        s = pd.Series(vals, index=idx, name=series_id).dropna()
        return s


def _synthetic_series(series_id: str, start: datetime, end: datetime) -> pd.Series:
    """Deterministic stub series so the pipeline runs without a FRED key.

    Each known series gets a plausible distribution (level + small noise);
    unknown ids return a 0-mean random walk. Sufficient for code paths +
    tests; never use for actual macro inference.
    """
    # Snap to whole UTC days so cross-series concat(join='inner') aligns.
    # Without this, each call to .now() inside .series() drifts by milliseconds
    # and the joined frame collapses to empty.
    start_day = pd.Timestamp(start.date(), tz="UTC")
    end_day = pd.Timestamp(end.date(), tz="UTC")
    days = max(1, (end_day - start_day).days)
    seed = abs(hash(series_id)) & 0xFFFF
    rng = np.random.default_rng(seed)
    levels = {
        "T10Y2Y": (0.5, 0.3),
        "T10Y3M": (0.3, 0.3),
        "DGS10":  (4.0, 0.4),
        "DGS2":   (4.6, 0.4),
        "DGS3MO": (5.0, 0.3),
        "DTWEXBGS": (105.0, 1.5),
        "VIXCLS": (16.0, 4.0),
        "UNRATE": (4.0, 0.3),
        "CPIAUCSL": (310.0, 5.0),
    }
    base, vol = levels.get(series_id, (0.0, 1.0))
    walk = np.cumsum(rng.normal(0, vol / np.sqrt(days), size=days)) + base
    idx = pd.date_range(start_day, periods=days, freq="D", tz="UTC")
    return pd.Series(walk, index=idx, name=series_id)
