"""Alpaca Market Data v2 adapter for 1-minute aggregate bars.

Reference: https://docs.alpaca.markets/reference/stockbars

Endpoint:
    GET https://data.alpaca.markets/v2/stocks/bars
        ?symbols=AAPL[,MSFT,…]
        &timeframe=1Min
        &start=<RFC3339|YYYY-MM-DD>
        &end=<RFC3339|YYYY-MM-DD>
        &limit=10000                # max 10000
        &adjustment=all              # raw|split|dividend|spin-off|all (comma-sep)
        &feed=iex                    # iex|sip|boats|otc (sip = paid)
        &sort=asc                    # asc|desc
        &page_token=…                # echo of `next_page_token`

Headers: `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`.

Response shape (multi-symbol):
    {
      "bars": { "AAPL": [ {t,o,h,l,c,v,n,vw}, … ], "MSFT": [ … ] },
      "next_page_token": "…" | null,
      "currency": "USD"
    }

We expose a per-symbol iterator (`aggs_1m(symbol, …)`) to mirror
`PolygonClient`'s public surface so `main.py` can swap them at call sites with
no other code change. Batching multiple symbols in a single request is a
future optimisation — single-symbol scanning is fine for current volumes.

Feeds:
  - `iex` (free tier default for our code) — IEX-only, ~3% of consolidated
    volume. Fine for indicator-based signals + backtests; VWAP will diverge
    from true NBBO on fast moves.
  - `sip` (paid, $99/mo) — full consolidated tape; equivalent to Polygon
    Starter. Set `ALPACA_FEED=sip` once you have a paid plan.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
from atlas_shared.config import get_settings
from atlas_shared.logging import get_logger
from tenacity import retry, stop_after_attempt, wait_exponential

log = get_logger("ingest.alpaca")

DEFAULT_DATA_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    """Thin async client around the bars endpoint. Mirrors the public surface
    of `PolygonClient` so the CLI can swap them at call sites."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        feed: str | None = None,
        timeout: float = 30.0,
    ):
        s = get_settings()
        self.api_key = api_key or s.alpaca_api_key
        self.api_secret = api_secret or s.alpaca_api_secret
        if not (self.api_key and self.api_secret):
            raise AlpacaError("ALPACA_API_KEY + ALPACA_API_SECRET are not configured")
        self.base_url = (
            base_url
            or os.environ.get("ALPACA_DATA_URL")
            or DEFAULT_DATA_URL
        )
        # Free-tier accounts can only access `iex`. Paid plans can flip to `sip`.
        self.feed = feed or os.environ.get("ALPACA_FEED", "iex")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
            },
        )

    async def __aenter__(self) -> AlpacaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        r = await self._client.get(path, params=params)
        if r.status_code == 429:
            raise AlpacaError("rate limited")
        if r.status_code == 403:
            # Most common cause: requesting `sip` on a free-tier account.
            raise AlpacaError(
                f"alpaca 403 — feed='{params.get('feed')}' may not be entitled. "
                f"Free tier is iex-only. body={r.text[:200]}"
            )
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

        Each yielded frame has the same columns as `PolygonClient.aggs_1m`:
            ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count
        """
        # End must reach the *end* of the requested day, not the start. Using
        # `datetime.min.time()` (00:00) here would exclude every intraday bar
        # from that day — the whole reason MAX(ts) got stuck at
        # `end_date - 1 day 23:59` on every backfill.
        params: dict[str, str | int] = {
            "symbols": symbol,
            "timeframe": "1Min",
            "start": datetime.combine(start, datetime.min.time(), UTC).isoformat(),
            "end": datetime.combine(end, datetime.max.time(), UTC).isoformat(),
            "limit": 10_000,
            "feed": self.feed,
            "adjustment": "all" if adjusted else "raw",
            "sort": "asc",
        }
        while True:
            payload = await self._get(BARS_PATH, params)
            # Multi-symbol response: bars is a dict keyed by symbol.
            bars_map = payload.get("bars") or {}
            bars = bars_map.get(symbol) or []
            if bars:
                df = pd.DataFrame(bars)
                # Alpaca keys: t, o, h, l, c, v, vw, n.
                df = df.rename(
                    columns={
                        "t": "ts_iso",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "c": "close",
                        "v": "volume",
                        "vw": "vwap",
                        "n": "trade_count",
                    }
                )
                df["ts"] = pd.to_datetime(df["ts_iso"], utc=True)
                df["symbol"] = symbol
                df["resolution"] = "1m"
                cols = ["ts", "symbol", "resolution", "open", "high", "low",
                        "close", "volume", "vwap", "trade_count"]
                yield df[cols]

            next_token = payload.get("next_page_token")
            if not next_token:
                break
            params = {**params, "page_token": next_token}


async def backfill_to_frame(symbol: str, days: int = 30) -> pd.DataFrame:
    """Convenience helper for tests + small backfills."""
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    out: list[pd.DataFrame] = []
    async with AlpacaClient() as c:
        async for chunk in c.aggs_1m(symbol, start, end):
            out.append(chunk)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Latest-bar helper for "near-live" polling.
#
# For genuine streaming the Alpaca Market Data WebSocket
# (`wss://stream.data.alpaca.markets/v2/{feed}`) is the right tool — that's a
# Phase 2 add (broadcaster gets a new `live_bar_listener` task).  Until then,
# polling this REST endpoint on a short interval from the broadcaster gives
# a "live enough" experience for indicator-based signals.
# ─────────────────────────────────────────────────────────────────────────────


async def latest_bar(symbol: str) -> dict | None:
    """One-shot fetch of the most recent 1-minute bar.

    Endpoint: GET /v2/stocks/bars/latest?symbols=SYM&feed=iex|sip
    Response: {"bars": {"AAPL": {t,o,h,l,c,v,n,vw}}}  (single object, not list)
    """
    async with AlpacaClient() as c:
        payload = await c._get(
            "/v2/stocks/bars/latest",
            {"symbols": symbol, "feed": c.feed},
        )
    bar = (payload.get("bars") or {}).get(symbol)
    return bar  # caller normalises shape if persisting
