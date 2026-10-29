"""Regression guards for the Alpaca ingest adapter.

Pins the exact bug that broke every backfill silently: the `end` param was
combined with `datetime.min.time()` (00:00) instead of `datetime.max.time()`
(23:59:59.999), so requests for "end = today" actually meant "end = midnight
at the START of today", which excluded every bar from the requested end day.

MAX(ts) after a backfill was stuck one full day behind current — which, after
a weekend + US holiday, made bars stale enough to trip the §9.6 stale-data
veto and stop signals from publishing across the board.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from ingest_equities.alpaca import AlpacaClient


@pytest.mark.asyncio
async def test_end_param_reaches_end_of_day_not_midnight() -> None:
    """The `end` query param must be `YYYY-MM-DDT23:59:59...`, NOT
    `YYYY-MM-DDT00:00:00...`. Otherwise we exclude the entire end day."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"bars": {"AAPL": []}, "next_page_token": None})

    client = AlpacaClient(api_key="k", api_secret="s")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://data.alpaca.markets",
        headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
    )

    end_day = date(2026, 7, 6)
    start_day = end_day - timedelta(days=7)
    async for _ in client.aggs_1m("AAPL", start_day, end_day):
        pass

    q = parse_qs(urlparse(str(captured[0].url)).query)
    end_iso = q["end"][0]
    start_iso = q["start"][0]

    # End must cover the whole end day.
    assert end_iso.startswith("2026-07-06T23:59:59"), (
        f"end must reach end-of-day for {end_day} inclusive, got {end_iso}. "
        "This is the exact bug that made every backfill exclude 'today' and "
        "left signals gated by the §9.6 stale-data veto."
    )
    # Start stays at 00:00 (a full-window request).
    assert start_iso.startswith("2026-06-29T00:00:00")


@pytest.mark.asyncio
async def test_end_bug_regression_end_of_range_is_reachable() -> None:
    """Direct regression: with `end = today`, a bar timestamped mid-day today
    must fall INSIDE the requested window. The old code sent
    end=today-T00:00 which excluded every mid-day bar."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # Alpaca-shaped bar from mid-morning today.
        bar = {
            "t": datetime.combine(date(2026, 7, 6), datetime.min.time(), UTC)
            .replace(hour=13, minute=45)
            .isoformat()
            .replace("+00:00", "Z"),
            "o": 100.0, "h": 100.5, "l": 99.9, "c": 100.2, "v": 12000, "n": 42, "vw": 100.1,
        }
        return httpx.Response(200, json={"bars": {"AAPL": [bar]}, "next_page_token": None})

    client = AlpacaClient(api_key="k", api_secret="s")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://data.alpaca.markets",
        headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
    )

    end_day = date(2026, 7, 6)
    start_day = end_day - timedelta(days=7)

    yielded: list = []
    async for chunk in client.aggs_1m("AAPL", start_day, end_day):
        yielded.append(chunk)

    assert yielded, "mid-day bar on end_day must be inside the window"
    df = yielded[0]
    assert df["ts"].iloc[0].date() == end_day
    assert df["ts"].iloc[0].hour == 13
