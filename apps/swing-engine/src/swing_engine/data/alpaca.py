"""Alpaca client for the swing engine: SIP daily bars, the asset list, the trading calendar.

Free-plan rules baked in here (Alpaca Market Data v2):

* ``feed=sip`` is allowed on the free plan only for data at least 16 minutes old, so a
  request's ``end`` is clamped to ``now - 16 minutes``.
* ``adjustment=all`` rewrites history on a split or dividend. Callers that append
  incrementally must notice that (see the backfill job), because this client only
  returns what Alpaca says today. ``adjustment=raw`` never changes: it is what printed on
  the day, which is what a price floor has to look at (stored as ``resolution='1d_raw'``).
* About 200 requests/minute, so requests are spaced ``min_interval_s`` apart (default
  0.3 s) and 429/5xx responses are retried with exponential backoff, as are network failures
  (a dropped connection, a timeout): a multi-hour backfill meets a few of those. Any other 4xx
  is final and raises :class:`AlpacaRequestError` with its status, so a caller can tell one
  rejected symbol (400/404/422) from bad keys (401/403).

Bars endpoint: ``GET /v2/stocks/bars?symbols=A,B&timeframe=1Day&feed=sip&adjustment=all``
answering ``{"bars": {SYM: [{t,o,h,l,c,v,vw,n}]}, "next_page_token": ...}``.
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal, Self

import httpx
import pandas as pd
from atlas_shared.logging import get_logger
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

from swing_engine.calendar import ET, Session

log = get_logger("swing.alpaca")

DEFAULT_DATA_URL = "https://data.alpaca.markets"
DEFAULT_TRADING_URL = "https://paper-api.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
ASSETS_PATH = "/v2/assets"
CALENDAR_PATH = "/v2/calendar"
BATCH_SIZE = 100
PAGE_LIMIT = 10_000
MAX_ATTEMPTS = 6
SIP_DELAY = timedelta(minutes=16)

BAR_COLUMNS = [
    "ts",
    "symbol",
    "resolution",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "trade_count",
]
_FLOAT_COLUMNS = ["open", "high", "low", "close", "volume", "vwap"]
# Alpaca's ``adjustment`` parameter and the ``resolution`` its bars are stored under
# (``DAILY`` and ``RAW_DAILY`` in :mod:`swing_engine.store`, which imports from here).
RESOLUTIONS = {"all": "1d", "raw": "1d_raw"}
_ALPACA_FIELDS = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vw": "vwap",
    "n": "trade_count",
}


class AlpacaError(RuntimeError):
    """A non-retryable Alpaca failure, or a retryable one that ran out of attempts."""


class AlpacaRequestError(AlpacaError):
    """A 4xx other than 429: Alpaca refused the request itself. ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _RetryableError(AlpacaError):
    """429, 5xx or a network failure: worth another attempt."""


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome is not None else None
    sleep = state.next_action.sleep if state.next_action is not None else 0.0
    log.warning("alpaca.retry", attempt=state.attempt_number, sleep_s=sleep, error=str(exc))


def _strip_v2(url: str) -> str:
    """``https://host/v2/`` and ``https://host`` both become ``https://host``."""
    url = url.rstrip("/")
    return url.removesuffix("/v2").rstrip("/")


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": pd.Series(dtype="datetime64[ns, UTC]"),
            "symbol": pd.Series(dtype="object"),
            "resolution": pd.Series(dtype="object"),
            **{c: pd.Series(dtype="float64") for c in _FLOAT_COLUMNS},
            "trade_count": pd.Series(dtype="int64"),
        }
    )[BAR_COLUMNS]


def _bars_frame(rows: list[dict[str, Any]], resolution: str) -> pd.DataFrame:
    """Flat Alpaca bar dicts (each with its ``symbol``) to the storage-shaped frame."""
    if not rows:
        return _empty_bars()
    df = pd.DataFrame(rows).rename(columns=_ALPACA_FIELDS)
    # Alpaca stamps a daily bar at midnight New York. The storage convention is the
    # session date at 00:00 UTC, so take the New York calendar date and re-label it UTC.
    session_date = (
        pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.tz_localize(None).dt.normalize()
    )
    df["ts"] = session_date.dt.tz_localize("UTC")
    df["resolution"] = resolution
    df = df.reindex(columns=BAR_COLUMNS)
    df[_FLOAT_COLUMNS] = df[_FLOAT_COLUMNS].astype("float64")
    return df.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)


class AlpacaData:
    """Async Alpaca client for daily bars, assets and the exchange calendar."""

    def __init__(
        self,
        key: str | None = None,
        secret: str | None = None,
        data_url: str | None = None,
        trading_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval_s: float = 0.3,
        retry_wait: wait_base | None = None,
    ) -> None:
        key = key or os.environ.get("ALPACA_API_KEY")
        secret = secret or os.environ.get("ALPACA_API_SECRET")
        if not (key and secret):
            raise AlpacaError("ALPACA_API_KEY and ALPACA_API_SECRET are not configured")
        self._data_url = (data_url or os.environ.get("ALPACA_DATA_URL") or DEFAULT_DATA_URL).rstrip(
            "/"
        )
        self._trading_url = _strip_v2(
            trading_url or os.environ.get("ALPACA_BASE_URL") or DEFAULT_TRADING_URL
        )
        self._min_interval_s = min_interval_s
        self._retry_wait = retry_wait or wait_exponential(multiplier=1, min=1, max=30)
        self._gate = asyncio.Lock()
        self._next_start = 0.0
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=30.0,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        """Hold the caller until ``min_interval_s`` after the previous request started."""
        async with self._gate:
            delay = self._next_start - _time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_start = _time.monotonic() + self._min_interval_s

    async def _get_once(self, url: str, params: dict[str, str | int]) -> Any:
        await self._throttle()
        try:
            r = await self._client.get(url, params=params)
        except httpx.TransportError as exc:  # connect, read, write, pool and timeout errors
            path = httpx.URL(url).path
            raise _RetryableError(f"network error on {path}: {type(exc).__name__}: {exc}") from exc
        status = r.status_code
        if status == 429 or status >= 500:
            raise _RetryableError(f"alpaca {status} from {r.url.path}: {r.text[:200]}")
        if status >= 400:
            raise AlpacaRequestError(status, f"alpaca {status} from {r.url.path}: {r.text[:200]}")
        return r.json()

    async def _get(self, url: str, params: dict[str, str | int]) -> Any:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=self._retry_wait,
            retry=retry_if_exception_type(_RetryableError),
            before_sleep=_log_retry,
            reraise=True,
        )
        return await retrying(self._get_once, url, params)

    async def daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        now: datetime | None = None,
        adjustment: Literal["all", "raw"] = "all",
    ) -> pd.DataFrame:
        """SIP daily bars for ``symbols`` from ``start`` to ``end``.

        ``adjustment="all"`` (the default) gives split/dividend-adjusted bars, labelled
        ``resolution="1d"``. ``adjustment="raw"`` gives the unadjusted prices as they
        printed, labelled ``resolution="1d_raw"``; they are only for questions about the
        price on the day (the price floor), never for indicators or returns.

        Both dates are New York session dates, inclusive. The request ``end`` never goes
        past ``now - 16 minutes`` (the free-plan SIP delay), so the newest returned bar
        may be earlier than ``end``. Symbols without bars are simply absent; no bars at
        all gives an empty frame with the same columns.

        Columns: ``ts`` (session date at 00:00 UTC), ``symbol``, ``resolution``,
        ``open``, ``high``, ``low``, ``close``, ``volume``, ``vwap``, ``trade_count``.
        """
        if adjustment not in RESOLUTIONS:
            raise ValueError(f"adjustment must be one of {sorted(RESOLUTIONS)}, got {adjustment!r}")
        now = now if now is not None else datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        start_dt = datetime.combine(start, dtime.min, ET).astimezone(UTC)
        day_end = datetime.combine(end, dtime(23, 59, 59), ET).astimezone(UTC)
        end_dt = min(day_end, (now.astimezone(UTC) - SIP_DELAY).replace(microsecond=0))
        unique = list(dict.fromkeys(symbols))
        if not unique or start_dt >= end_dt:
            return _empty_bars()

        base: dict[str, str | int] = {
            "timeframe": "1Day",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "limit": PAGE_LIMIT,
            "adjustment": adjustment,
            "feed": "sip",
            "sort": "asc",
        }
        rows: list[dict[str, Any]] = []
        for i in range(0, len(unique), BATCH_SIZE):
            params = {**base, "symbols": ",".join(unique[i : i + BATCH_SIZE])}
            while True:
                payload = await self._get(self._data_url + BARS_PATH, params)
                for symbol, bars in (payload.get("bars") or {}).items():
                    rows.extend({"symbol": symbol, **bar} for bar in bars)
                token = payload.get("next_page_token")
                if not token:
                    break
                params = {**params, "page_token": token}
        return _bars_frame(rows, RESOLUTIONS[adjustment])

    async def assets(self, status: Literal["active", "inactive"]) -> list[dict[str, Any]]:
        """Alpaca's US-equity asset list (symbol, exchange, tradable flags, ...)."""
        out = await self._get(
            self._trading_url + ASSETS_PATH, {"status": status, "asset_class": "us_equity"}
        )
        return list(out)

    async def calendar(self, start: date, end: date) -> list[Session]:
        """Exchange sessions from ``start`` to ``end`` inclusive, with early closes."""
        rows = await self._get(
            self._trading_url + CALENDAR_PATH,
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        return [
            Session(
                day=date.fromisoformat(r["date"]),
                open=dtime.fromisoformat(r["open"]),
                close=dtime.fromisoformat(r["close"]),
            )
            for r in rows
        ]
