"""Swing engine Alpaca client: SIP daily bars, batching, retries, throttle, assets, calendar.

Every test runs against an ``httpx.MockTransport``; nothing touches the network.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from itertools import pairwise
from urllib.parse import parse_qs, urlparse

import httpx
import pandas as pd
import pytest
from swing_engine.calendar import Session
from swing_engine.data.alpaca import AlpacaData, AlpacaError, AlpacaRequestError
from tenacity import wait_none

NOW = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
COLUMNS = [
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


def _bar(t: str, c: float = 100.0) -> dict:
    return {"t": t, "o": c - 1, "h": c + 1, "l": c - 2, "c": c, "v": 1000, "vw": c - 0.5, "n": 77}


def _client(
    handler: Callable[[httpx.Request], httpx.Response], **kw: object
) -> tuple[AlpacaData, list[httpx.Request]]:
    """A client wired to ``handler``; the returned list collects every request it saw."""
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    opts: dict[str, object] = {
        "key": "k",
        "secret": "s",
        "transport": httpx.MockTransport(wrapped),
        "min_interval_s": 0.0,
        "retry_wait": wait_none(),
    }
    opts.update(kw)
    return AlpacaData(**opts), captured  # type: ignore[arg-type]


def _qs(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


def _empty(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"bars": {}, "next_page_token": None})


async def test_daily_bars_uses_sip_all_adjustment_and_batches_of_100():
    syms = [f"S{i}" for i in range(150)]
    c, captured = _client(_empty)
    async with c:
        await c.daily_bars(syms, date(2024, 1, 1), date(2024, 1, 31), now=NOW)
    qs = [_qs(r) for r in captured]
    assert len(captured) == 2 and qs[0]["feed"] == ["sip"] and qs[0]["adjustment"] == ["all"]
    assert qs[0]["timeframe"] == ["1Day"] and len(qs[0]["symbols"][0].split(",")) == 100
    assert len(qs[1]["symbols"][0].split(",")) == 50
    assert qs[0]["symbols"][0].split(",") + qs[1]["symbols"][0].split(",") == syms
    assert qs[0]["limit"] == ["10000"] and qs[0]["sort"] == ["asc"]
    assert urlparse(str(captured[0].url)).path == "/v2/stocks/bars"
    assert captured[0].url.host == "data.alpaca.markets"


async def test_requests_carry_alpaca_auth_headers():
    c, captured = _client(_empty, key="the-key", secret="the-secret")
    async with c:
        await c.daily_bars(["AAPL"], date(2024, 1, 1), date(2024, 1, 31), now=NOW)
    assert captured[0].headers["APCA-API-KEY-ID"] == "the-key"
    assert captured[0].headers["APCA-API-SECRET-KEY"] == "the-secret"


async def test_start_and_end_are_new_york_day_bounds_in_utc():
    c, captured = _client(_empty)
    async with c:
        await c.daily_bars(["AAPL"], date(2024, 1, 2), date(2024, 1, 5), now=NOW)
    qs = _qs(captured[0])
    # Midnight EST is 05:00Z; the end of Jan 5 EST is 04:59:59Z on Jan 6.
    assert qs["start"] == ["2024-01-02T05:00:00+00:00"]
    assert qs["end"] == ["2024-01-06T04:59:59+00:00"]


async def test_end_is_clamped_to_16_minutes_before_now():
    c, captured = _client(_empty)
    async with c:
        await c.daily_bars(
            ["AAPL"],
            date(2026, 10, 16),
            date(2026, 10, 16),
            now=datetime(2026, 10, 16, 20, 10, tzinfo=UTC),
        )
    assert _qs(captured[0])["end"] == ["2026-10-16T19:54:00+00:00"]


async def test_clamp_works_for_a_now_in_another_timezone():
    from swing_engine.calendar import ET

    c, captured = _client(_empty)
    async with c:
        await c.daily_bars(
            ["AAPL"],
            date(2026, 10, 16),
            date(2026, 10, 16),
            now=datetime(2026, 10, 16, 16, 10, tzinfo=ET),
        )
    assert _qs(captured[0])["end"] == ["2026-10-16T19:54:00+00:00"]


async def test_no_request_when_the_window_is_empty_after_clamping():
    c, captured = _client(_empty)
    async with c:
        df = await c.daily_bars(
            ["AAPL"],
            date(2026, 10, 16),
            date(2026, 10, 16),
            now=datetime(2026, 10, 16, 4, 0, tzinfo=UTC),  # clamp lands before midnight ET
        )
    assert captured == []
    assert list(df.columns) == COLUMNS and df.empty


async def test_naive_now_is_rejected():
    c, _ = _client(_empty)
    async with c:
        with pytest.raises(ValueError, match="timezone-aware"):
            await c.daily_bars(
                ["AAPL"], date(2024, 1, 1), date(2024, 1, 2), now=datetime(2026, 1, 1)
            )


async def test_pagination_follows_next_page_token_and_ts_is_session_date():
    def handler(request: httpx.Request) -> httpx.Response:
        if "page_token" not in _qs(request):
            body = {"bars": {"AAPL": [_bar("2026-10-15T04:00:00Z", 100)]}, "next_page_token": "p2"}
        else:
            assert _qs(request)["page_token"] == ["p2"]
            body = {"bars": {"AAPL": [_bar("2026-10-16T04:00:00Z", 101)]}, "next_page_token": None}
        return httpx.Response(200, json=body)

    c, captured = _client(handler)
    async with c:
        df = await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 2
    assert list(df["ts"]) == [
        pd.Timestamp("2026-10-15", tz="UTC"),
        pd.Timestamp("2026-10-16", tz="UTC"),
    ]
    assert (df["resolution"] == "1d").all()
    assert list(df.columns) == COLUMNS
    assert list(df["close"]) == [100.0, 101.0]
    assert (
        df.loc[0, "open"] == 99.0 and df.loc[0, "vwap"] == 99.5 and df.loc[0, "trade_count"] == 77
    )
    assert (df["symbol"] == "AAPL").all()


async def test_ts_is_the_new_york_session_date_on_both_sides_of_the_dst_change():
    # EDT midnight is 04:00Z, EST midnight is 05:00Z. Both must map to the same session date.
    payload = {
        "bars": {
            "AAPL": [
                _bar("2026-10-30T04:00:00Z"),  # Fri, EDT
                _bar("2026-11-02T05:00:00Z"),  # Mon, EST (clocks changed on Sun Nov 1)
            ]
        },
        "next_page_token": None,
    }
    c, _ = _client(lambda _: httpx.Response(200, json=payload))
    later = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    async with c:
        df = await c.daily_bars(["AAPL"], date(2026, 10, 30), date(2026, 11, 2), now=later)
    assert list(df["ts"]) == [
        pd.Timestamp("2026-10-30", tz="UTC"),
        pd.Timestamp("2026-11-02", tz="UTC"),
    ]
    assert str(df["ts"].dt.tz) == "UTC"


async def test_multi_symbol_response_is_flattened_and_ordered_by_symbol_then_time():
    payload = {
        "bars": {
            "MSFT": [_bar("2026-10-15T04:00:00Z", 300), _bar("2026-10-16T04:00:00Z", 301)],
            "AAPL": [_bar("2026-10-15T04:00:00Z", 100)],
            "DEAD": [],
        },
        "next_page_token": None,
    }
    c, _ = _client(lambda _: httpx.Response(200, json=payload))
    async with c:
        df = await c.daily_bars(
            ["AAPL", "MSFT", "DEAD"], date(2026, 10, 15), date(2026, 10, 16), now=NOW
        )
    assert list(zip(df["symbol"], df["close"], strict=True)) == [
        ("AAPL", 100.0),
        ("MSFT", 300.0),
        ("MSFT", 301.0),
    ]


async def test_no_bars_at_all_gives_an_empty_frame_with_the_right_columns():
    c, _ = _client(lambda _: httpx.Response(200, json={"bars": None, "next_page_token": None}))
    async with c:
        df = await c.daily_bars(["GONE"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert df.empty and list(df.columns) == COLUMNS


async def test_no_symbols_makes_no_requests():
    c, captured = _client(_empty)
    async with c:
        df = await c.daily_bars([], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert captured == [] and df.empty and list(df.columns) == COLUMNS


async def test_repeated_symbols_are_requested_once():
    c, captured = _client(_empty)
    async with c:
        await c.daily_bars(
            ["AAPL", "MSFT", "AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW
        )
    assert _qs(captured[0])["symbols"] == ["AAPL,MSFT"]


async def test_429_is_retried():
    responses = iter(
        [httpx.Response(429, text="slow down"), httpx.Response(200, json={"bars": {}})]
    )
    c, captured = _client(lambda _: next(responses))
    async with c:
        df = await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 2 and df.empty


async def test_5xx_is_retried_and_gives_up_after_six_attempts():
    c, captured = _client(lambda _: httpx.Response(503, text="down"))
    async with c:
        with pytest.raises(AlpacaError, match="503"):
            await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 6


async def test_other_4xx_is_not_retried_and_names_the_status():
    c, captured = _client(lambda _: httpx.Response(403, text="not entitled"))
    async with c:
        with pytest.raises(AlpacaError, match="403"):
            await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 1


async def test_a_dropped_connection_is_retried():
    """The incident: one TCP reset or read timeout during a multi-hour backfill raised straight
    out of the client and failed its whole chunk; only 429 and 5xx answers were retried."""
    answers: list[Exception | httpx.Response] = [
        httpx.ConnectError("connection reset"),
        httpx.Response(200, json={"bars": {}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    c, captured = _client(handler)
    async with c:
        df = await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 2 and df.empty


async def test_timeouts_are_retried_and_end_as_an_alpaca_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    c, captured = _client(handler)
    async with c:
        with pytest.raises(AlpacaError, match="ReadTimeout"):
            await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert len(captured) == 6


async def test_a_4xx_carries_its_status_so_a_caller_can_tell_a_bad_symbol_from_bad_keys():
    c, _ = _client(lambda _: httpx.Response(422, text="invalid symbol: BAD!"))
    async with c:
        with pytest.raises(AlpacaRequestError) as err:
            await c.daily_bars(["BAD!"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert err.value.status == 422 and isinstance(err.value, AlpacaError)


async def test_requests_start_at_least_min_interval_apart():
    starts: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        starts.append(_time.monotonic())
        return httpx.Response(200, json={"bars": {}})

    c, _ = _client(handler, min_interval_s=0.05)
    async with c:
        await c.daily_bars(
            [f"S{i}" for i in range(250)], date(2026, 10, 15), date(2026, 10, 16), now=NOW
        )
    assert len(starts) == 3
    gaps = [b - a for a, b in pairwise(starts)]
    assert all(g >= 0.045 for g in gaps), gaps


async def test_assets_hits_the_trading_host_with_the_status_filter():
    rows = [{"symbol": "AAPL", "status": "active", "tradable": True, "exchange": "NASDAQ"}]
    c, captured = _client(lambda _: httpx.Response(200, json=rows))
    async with c:
        out = await c.assets("active")
    assert out == rows
    assert captured[0].url.host == "paper-api.alpaca.markets"
    assert urlparse(str(captured[0].url)).path == "/v2/assets"
    assert _qs(captured[0])["status"] == ["active"]
    assert captured[0].headers["APCA-API-KEY-ID"] == "k"


async def test_calendar_maps_alpaca_rows_to_sessions():
    rows = [
        {"date": "2026-11-25", "open": "09:30", "close": "16:00", "settlement_date": "2026-11-27"},
        {"date": "2026-11-27", "open": "09:30", "close": "13:00", "settlement_date": "2026-12-01"},
    ]
    c, captured = _client(lambda _: httpx.Response(200, json=rows))
    async with c:
        sessions = await c.calendar(date(2026, 11, 25), date(2026, 11, 27))
    assert sessions == [
        Session(date(2026, 11, 25), time(9, 30), time(16, 0)),
        Session(date(2026, 11, 27), time(9, 30), time(13, 0)),
    ]
    assert urlparse(str(captured[0].url)).path == "/v2/calendar"
    assert _qs(captured[0])["start"] == ["2026-11-25"] and _qs(captured[0])["end"] == ["2026-11-27"]


async def test_trading_url_drops_a_trailing_v2_from_the_base_url_env(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2/")
    c, captured = _client(lambda _: httpx.Response(200, json=[]))
    async with c:
        await c.assets("inactive")
    assert urlparse(str(captured[0].url)).path == "/v2/assets"
    assert captured[0].url.host == "paper-api.alpaca.markets"


async def test_explicit_urls_override_the_environment(monkeypatch):
    monkeypatch.setenv("ALPACA_DATA_URL", "https://env-data.example")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/calendar":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"bars": {}})

    c, captured = _client(
        handler, data_url="https://data.example/", trading_url="https://trade.example/v2"
    )
    async with c:
        await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
        await c.calendar(date(2026, 10, 15), date(2026, 10, 16))
    assert captured[0].url.host == "data.example"
    assert captured[1].url.host == "trade.example"
    assert urlparse(str(captured[1].url)).path == "/v2/calendar"


async def test_credentials_and_data_url_default_from_the_environment(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    monkeypatch.setenv("ALPACA_DATA_URL", "https://env-data.example")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"bars": {}})

    async with AlpacaData(transport=httpx.MockTransport(handler), min_interval_s=0.0) as c:
        await c.daily_bars(["AAPL"], date(2026, 10, 15), date(2026, 10, 16), now=NOW)
    assert captured[0].headers["APCA-API-KEY-ID"] == "env-key"
    assert captured[0].headers["APCA-API-SECRET-KEY"] == "env-secret"
    assert captured[0].url.host == "env-data.example"


def test_missing_credentials_fail_fast(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaError, match="ALPACA_API_KEY"):
        AlpacaData(transport=httpx.MockTransport(_empty))


async def test_context_manager_closes_the_http_client():
    c, _ = _client(_empty)
    async with c as entered:
        assert entered is c
    with pytest.raises(RuntimeError):
        await c.assets("active")
