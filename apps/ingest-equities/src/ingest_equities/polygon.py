"""Minimal Polygon.io REST client for 1-minute aggregate bars.

Only the endpoint we actually need:
  GET /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}

Phase 1.5 will add: WebSocket trades, options aggs, news, snapshots.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
from atlas_shared.config import get_settings
from atlas_shared.logging import get_logger
from tenacity import retry, stop_after_attempt, wait_exponential

log = get_logger("ingest.polygon")

BASE = "https://api.polygon.io"


class PolygonError(RuntimeError):
    pass


class PolygonClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or get_settings().polygon_api_key
        if not self.api_key:
            raise PolygonError("POLYGON_API_KEY is not configured")
        self._client = httpx.AsyncClient(timeout=timeout, base_url=BASE)

    async def __aenter__(self) -> PolygonClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        params = {**params, "apiKey": self.api_key}
        r = await self._client.get(path, params=params)
        if r.status_code == 429:
            raise PolygonError("rate limited")
        r.raise_for_status()
        return r.json()

    async def aggs_1m(
        self,
        symbol: str,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> AsyncIterator[pd.DataFrame]:
        """Yield batches of 1-minute bars between `start` and `end`.

        Polygon paginates via `next_url`; we follow it transparently. Each
        yielded frame has columns:
            ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count
        """
        path = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start.isoformat()}/{end.isoformat()}"
        params: dict[str, str | int] = {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": 50_000,
        }
        while True:
            payload = await self._get(path, params)
            results = payload.get("results") or []
            if results:
                df = pd.DataFrame(results)
                df = df.rename(
                    columns={
                        "t": "ts_ms",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "c": "close",
                        "v": "volume",
                        "vw": "vwap",
                        "n": "trade_count",
                    }
                )
                df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
                df["symbol"] = symbol
                df["resolution"] = "1m"
                cols = ["ts", "symbol", "resolution", "open", "high", "low", "close", "volume", "vwap", "trade_count"]
                yield df[cols]

            next_url = payload.get("next_url")
            if not next_url:
                break
            # Polygon's next_url is fully qualified; httpx will route off base.
            path = next_url
            params = {}


async def backfill_to_frame(symbol: str, days: int = 30) -> pd.DataFrame:
    """Convenience helper for tests + small backfills."""
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    out: list[pd.DataFrame] = []
    async with PolygonClient() as c:
        async for chunk in c.aggs_1m(symbol, start, end):
            out.append(chunk)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()
