"""Swing engine ingest: resumable SIP backfill, split-aware incremental update, the CLI.

Everything runs against in-memory fakes of the Alpaca client and the bar store, so no
network or database is involved. The fakes mimic the real contracts that matter here:
the client clips bars to ``[start, end]`` like the real endpoint, and the store derives a
symbol's last bar from the bars it holds, like ``max(ts)`` in the database.

The incident these tests pin (Review Focus 1): ``adjustment=all`` rewrites history when a
stock splits, so appending only new days to old rows would splice two price scales and
draw a fake crash. The update therefore compares an overlap bar with what is stored and
refetches that symbol's whole history when the two disagree.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd
import pytest
import swing_engine.__main__ as cli
from swing_engine import ingest, store
from swing_engine.calendar import Session
from swing_engine.data.alpaca import BAR_COLUMNS, AlpacaError
from swing_engine.ingest import (
    IngestResult,
    backfill,
    incremental_update,
    refresh_assets,
)

FRI_EVENING = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)  # 17:00 ET: Friday's session is final
MON_EVENING = datetime(2026, 10, 19, 21, 0, tzinfo=UTC)
TUE_EVENING = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
FRIDAY = date(2026, 10, 16)
MONDAY = date(2026, 10, 19)
START = date(2016, 1, 1)


def common(
    symbol: str, exchange: str = "NASDAQ", name: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """An Alpaca asset row; ``status`` is left out (unknown) unless given."""
    row: dict[str, Any] = {
        "symbol": symbol,
        "name": name or f"{symbol} Corporation Common Stock",
        "exchange": exchange,
        "class": "us_equity",
        "tradable": True,
    }
    if status is not None:
        row["status"] = status
    return row


def etf(symbol: str) -> dict[str, Any]:
    return {**common(symbol, "ARCA"), "name": f"{symbol} Index ETF"}


def _bars_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


class FakeClient:
    """Stands in for ``AlpacaData``: bars are ``{symbol: [(iso_day, close), ...]}``."""

    def __init__(
        self,
        bars: dict[str, list[tuple[str, float]]] | None = None,
        *,
        active: list[dict[str, Any]] | None = None,
        inactive: list[dict[str, Any]] | None = None,
        fail: set[str] | None = None,
        fail_full_history: set[str] | None = None,
    ) -> None:
        self.bars = bars or {}
        self._assets = {"active": active or [], "inactive": inactive or []}
        self.fail = fail or set()
        self.fail_full_history = fail_full_history or set()
        self.calls: list[dict[str, Any]] = []
        self.requested_symbols: list[str] = []
        self.full_history_requests: list[str] = []
        # adjustment=raw requests are kept apart (see test_swing_raw_close.py)
        self.raw_calls: list[dict[str, Any]] = []
        self.asset_calls: list[str] = []
        self.calendar_calls: list[tuple[date, date]] = []

    async def daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        now: datetime | None = None,
        adjustment: str = "all",
    ) -> pd.DataFrame:
        if adjustment == "raw":  # kept apart so these tests only see the adjusted requests
            self.raw_calls.append({"symbols": list(symbols), "start": start, "end": end})
            return _bars_frame([])
        self.calls.append({"symbols": list(symbols), "start": start, "end": end, "now": now})
        self.requested_symbols.extend(symbols)
        if start <= START:
            self.full_history_requests.extend(symbols)
            if self.fail_full_history & set(symbols):
                raise AlpacaError("alpaca 500 from /v2/stocks/bars: boom")
        if self.fail & set(symbols):
            raise AlpacaError("alpaca 500 from /v2/stocks/bars: boom")
        rows = [
            {
                "ts": day,
                "symbol": symbol,
                "resolution": "1d",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000.0,
                "vwap": close,
                "trade_count": 10,
            }
            for symbol in symbols
            for day, close in self.bars.get(symbol, [])
            if start <= date.fromisoformat(day) <= end
        ]
        return _bars_frame(rows)

    async def assets(self, status: str) -> list[dict[str, Any]]:
        self.asset_calls.append(status)
        return [dict(a) for a in self._assets[status]]

    async def calendar(self, start: date, end: date) -> list[Session]:
        self.calendar_calls.append((start, end))
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [Session(d, time(9, 30), time(16)) for d in days if d.weekday() < 5]


class FakeStore:
    """In-memory ``BarStore``. A symbol's last bar is the newest bar it holds."""

    def __init__(
        self,
        *,
        last: dict[str, date] | None = None,
        closes: dict[tuple[str, date], float] | None = None,
        assets: list[dict[str, Any]] | None = None,
        fail_upsert: set[str] | None = None,
    ) -> None:
        self.closes: dict[tuple[str, date], float] = dict(closes or {})
        self.last: dict[str, date] = dict(last or {})
        for (symbol, day), _ in self.closes.items():
            if day > self.last.get(symbol, date.min):
                self.last[symbol] = day
        self.assets = [dict(a) for a in (assets or [])]
        self.fail_upsert = fail_upsert or set()
        self.upserts: list[pd.DataFrame] = []
        self.ranges_refreshed = 0

    async def upsert_assets(self, rows: Sequence[dict[str, Any]]) -> int:
        by_symbol = {a["symbol"]: a for a in self.assets}
        by_symbol.update({r["symbol"]: dict(r) for r in rows})
        self.assets = list(by_symbol.values())
        return len(rows)

    async def load_assets(self) -> pd.DataFrame:
        return pd.DataFrame(self.assets, columns=["symbol", "name", "exchange", "status", "kind"])

    async def upsert_daily_bars(self, df: pd.DataFrame) -> int:
        if self.fail_upsert & set(df["symbol"]):
            raise RuntimeError("database is down")
        self.upserts.append(df)
        for row in df.itertuples():
            day = row.ts.date()
            self.closes[(row.symbol, day)] = float(row.close)
            self.last[row.symbol] = max(day, self.last.get(row.symbol, day))
        return len(df)

    async def last_bar_dates(self, resolution: str = "1d") -> dict[str, date]:
        return dict(self.last) if resolution == "1d" else {}  # no raw bars in these tests

    async def stored_close(self, symbol: str, day: date) -> float | None:
        return self.closes.get((symbol, day))

    async def refresh_asset_bar_ranges(self) -> None:
        self.ranges_refreshed += 1

    @property
    def written_symbols(self) -> list[str]:
        return sorted({s for df in self.upserts for s in df["symbol"]})


def week(close: float = 100.0) -> list[tuple[str, float]]:
    """Mon 12 through Fri 16 Oct 2026, one flat close."""
    return [(f"2026-10-{d}", close) for d in (12, 13, 14, 15, 16)]


# --- refresh_assets --------------------------------------------------------------------


async def test_refresh_assets_classifies_and_tags_status_from_each_list():
    client = FakeClient(active=[common("AAA"), etf("QQQQ")], inactive=[common("OLD")])
    store_ = FakeStore()

    rows = await refresh_assets(client, store_)

    by_symbol = {r["symbol"]: r for r in rows}
    assert {s: (r["kind"], r["status"]) for s, r in by_symbol.items()} == {
        "AAA": ("common", "active"),
        "QQQQ": ("etf", "active"),
        "OLD": ("common", "inactive"),
    }
    assert sorted(client.asset_calls) == ["active", "inactive"]
    assert {a["symbol"] for a in store_.assets} == {"AAA", "QQQQ", "OLD"}


async def test_refresh_assets_active_wins_when_a_symbol_is_in_both_lists():
    """A ticker reused by a new company appears as inactive (old) and active (new)."""
    client = FakeClient(
        active=[common("REUSE", name="REUSE New Corporation Common Stock")],
        inactive=[common("REUSE", name="REUSE Old Corporation Common Stock")],
    )
    store_ = FakeStore()

    rows = await refresh_assets(client, store_)

    assert [(r["symbol"], r["status"]) for r in rows] == [("REUSE", "active")]
    assert "New" in rows[0]["name"]


# --- backfill --------------------------------------------------------------------------


async def test_backfill_resumes_from_stored_last_bar():
    store_ = FakeStore(last={"AAA": FRIDAY}, assets=[common("AAA"), common("BBB")])
    client = FakeClient(bars={"BBB": week(), "SPY": week()})

    res = await backfill(client, store_, start=START, now=FRI_EVENING)

    assert client.requested_symbols == ["BBB", "SPY"] and res.symbols_total == 3
    assert res.symbols_done == 3 and res.rows == 10 and res.failed == [] and res.refetched == []
    assert store_.written_symbols == ["BBB", "SPY"]
    assert store_.ranges_refreshed == 1


async def test_backfill_requests_up_to_the_last_completed_session_only():
    store_ = FakeStore(assets=[common("AAA")])
    client = FakeClient(bars={"AAA": [*week(), ("2026-10-19", 105.0)]})

    await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["AAA"])

    assert client.calendar_calls == [(date(2026, 10, 6), date(2026, 10, 17))]
    (call,) = client.calls
    assert (call["start"], call["end"], call["now"]) == (START, FRIDAY, FRI_EVENING)
    assert ("AAA", date(2026, 10, 19)) not in store_.closes


@pytest.mark.parametrize(
    ("now", "target"),
    [
        (datetime(2026, 10, 17, 15, 0, tzinfo=UTC), FRIDAY),  # Saturday: Friday is last
        (datetime(2026, 10, 16, 20, 17, tzinfo=UTC), FRIDAY),  # 16:17 ET, past close + 16 min
        (
            datetime(2026, 10, 16, 20, 10, tzinfo=UTC),
            date(2026, 10, 15),
        ),  # 16:10 ET: still settling
        (datetime(2026, 10, 16, 14, 0, tzinfo=UTC), date(2026, 10, 15)),  # Friday mid-session
    ],
)
async def test_backfill_never_requests_past_the_settled_session(now, target):
    client = FakeClient(bars={"AAA": week()})

    await backfill(client, FakeStore(), start=START, now=now, symbols=["AAA"])

    assert [c["end"] for c in client.calls] == [target]


async def test_backfill_skips_symbols_already_at_the_target_session():
    store_ = FakeStore(last={"AAA": FRIDAY, "SPY": date(2026, 10, 19)}, assets=[common("AAA")])
    client = FakeClient()
    seen: list[tuple[int, int]] = []

    res = await backfill(
        client, store_, start=START, now=FRI_EVENING, progress=lambda d, t: seen.append((d, t))
    )

    assert client.calls == [] and store_.upserts == []
    assert (res.symbols_done, res.symbols_total, res.rows) == (2, 2, 0)
    assert seen == [(2, 2)]
    assert store_.ranges_refreshed == 1


async def test_backfill_loads_a_symbol_with_no_stored_bars_whole_from_start():
    store_ = FakeStore(assets=[common("AAA")])
    client = FakeClient(bars={"AAA": week()})

    await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["AAA"])

    assert [(c["symbols"], c["start"]) for c in client.calls] == [(["AAA"], START)]


async def test_backfill_skips_a_delisted_symbol_that_already_has_bars():
    """Its history is complete, and its newest bar can never reach the target session."""
    store_ = FakeStore(
        last={"OLD": date(2024, 3, 14)}, assets=[common("OLD", status="inactive"), common("AAA")]
    )
    client = FakeClient(bars={"AAA": week(), "SPY": week()})
    seen: list[tuple[int, int]] = []

    res = await backfill(
        client, store_, start=START, now=FRI_EVENING, progress=lambda d, t: seen.append((d, t))
    )

    assert client.requested_symbols == ["AAA", "SPY"]
    assert (res.symbols_done, res.symbols_total, res.failed) == (3, 3, [])
    assert seen[0] == (1, 3) and seen[-1] == (3, 3)


async def test_backfill_still_loads_a_delisted_symbol_that_has_no_bars():
    store_ = FakeStore(assets=[common("OLD", status="inactive")])
    client = FakeClient(bars={"OLD": week()})

    await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["OLD"])

    assert [(c["symbols"], c["start"]) for c in client.calls] == [(["OLD"], START)]
    assert store_.written_symbols == ["OLD"]


async def test_backfill_catches_an_active_symbol_up_from_its_newest_bar():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 9)): 100.0}, assets=[common("AAA")])
    client = FakeClient(bars={"AAA": [("2026-10-09", 100.0), *week(100.0)]})

    res = await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["AAA"])

    (call,) = client.calls
    assert (call["symbols"], call["start"], call["end"]) == (["AAA"], date(2026, 10, 9), FRIDAY)
    assert client.full_history_requests == [] and res.refetched == []
    assert (res.symbols_done, res.symbols_total, res.failed) == (1, 1, [])
    assert store_.last["AAA"] == FRIDAY


async def test_backfill_catches_up_a_symbol_that_is_one_session_behind():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.0})
    client = FakeClient(bars={"AAA": week(100.0)})

    await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["AAA"])

    assert [c["start"] for c in client.calls] == [date(2026, 10, 15)]
    assert store_.last["AAA"] == FRIDAY


async def test_backfill_neither_refetches_nor_fails_a_symbol_with_no_print_on_the_target_day():
    """A halted or illiquid name is behind forever without being broken."""
    store_ = FakeStore(closes={("HALT", date(2026, 10, 15)): 20.0})
    client = FakeClient(bars={"HALT": [("2026-10-15", 20.0)]})  # nothing on the 16th

    res = await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["HALT"])

    assert res.failed == [] and res.refetched == [] and res.symbols_done == 1
    assert [c["start"] for c in client.calls] == [date(2026, 10, 15)]


async def test_backfill_catch_up_chunks_are_ordered_and_start_at_their_oldest_bar(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 2)
    newest = {"CCC": date(2026, 10, 14), "AAA": date(2026, 10, 2), "BBB": date(2026, 10, 9)}
    store_ = FakeStore(closes={(sym, day): 50.0 for sym, day in newest.items()})
    client = FakeClient(
        bars={sym: [(day.isoformat(), 50.0), *week(50.0)] for sym, day in newest.items()}
    )

    await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["CCC", "AAA", "BBB"])

    assert [(c["symbols"], c["start"]) for c in client.calls] == [
        (["AAA", "BBB"], date(2026, 10, 2)),
        (["CCC"], date(2026, 10, 14)),
    ]


async def test_backfill_refetches_a_behind_symbol_whose_prices_were_adjusted():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 9)): 400.0})  # pre-split stored scale
    client = FakeClient(bars={"AAA": [("2026-10-09", 100.0), *week(100.0)]})  # adjusted 4:1

    res = await backfill(client, store_, start=START, now=FRI_EVENING, symbols=["AAA"])

    assert res.refetched == ["AAA"] and client.full_history_requests == ["AAA"]
    assert res.failed == [] and res.symbols_done == 1
    assert store_.closes[("AAA", date(2026, 10, 9))] == 100.0  # one scale all the way back


async def test_backfill_leaves_an_adjusted_symbol_whole_when_its_refetch_fails():
    """Review Focus 1 on the backfill path: never a spliced series."""
    store_ = FakeStore(closes={("AAA", date(2026, 10, 9)): 400.0})
    bars = {"AAA": [("2026-10-09", 100.0), *week(100.0)]}

    first = await backfill(
        FakeClient(bars=bars, fail_full_history={"AAA"}),
        store_,
        start=START,
        now=FRI_EVENING,
        symbols=["AAA"],
    )

    assert first.failed == ["AAA"] and first.refetched == []
    assert store_.closes == {("AAA", date(2026, 10, 9)): 400.0}  # nothing new spliced on

    second = await backfill(
        FakeClient(bars=bars), store_, start=START, now=FRI_EVENING, symbols=["AAA"]
    )

    assert second.refetched == ["AAA"] and second.failed == []


async def test_backfill_resumed_after_the_next_session_settles_does_not_redownload(monkeypatch):
    """Interrupted Monday night, resumed Tuesday: only the unfinished chunk starts from zero."""
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 2)
    names = ["AAA", "BBB", "CCC", "DDD"]
    bars = {n: [*week(), ("2026-10-19", 100.0), ("2026-10-20", 100.0)] for n in names}
    store_ = FakeStore()

    first = await backfill(
        FakeClient(bars=bars, fail={"CCC"}), store_, start=START, now=MON_EVENING, symbols=names
    )

    assert first.failed == ["CCC", "DDD"]
    assert store_.last == {"AAA": MONDAY, "BBB": MONDAY}

    second_client = FakeClient(bars=bars)
    second = await backfill(second_client, store_, start=START, now=TUE_EVENING, symbols=names)

    starts = {tuple(c["symbols"]): c["start"] for c in second_client.calls}
    assert starts == {("AAA", "BBB"): MONDAY, ("CCC", "DDD"): START}
    assert second_client.full_history_requests == ["CCC", "DDD"]
    assert (second.symbols_done, second.symbols_total, second.failed) == (4, 4, [])
    assert set(store_.last.values()) == {date(2026, 10, 20)}


async def test_backfill_processes_chunks_and_stores_each_before_the_next(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 2)
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    store_ = FakeStore()
    client = FakeClient(bars={n: week() for n in names})
    seen: list[tuple[int, int]] = []

    res = await backfill(
        client,
        store_,
        start=START,
        now=FRI_EVENING,
        symbols=names,
        progress=lambda d, t: seen.append((d, t)),
    )

    assert [c["symbols"] for c in client.calls] == [["AAA", "BBB"], ["CCC", "DDD"], ["EEE"]]
    assert len(store_.upserts) == 3
    assert seen == [(0, 5), (2, 5), (4, 5), (5, 5)]
    assert (res.symbols_done, res.symbols_total, res.rows) == (5, 5, 25)


async def test_backfill_skips_a_failing_chunk_and_a_rerun_resumes_it(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 2)
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    store_ = FakeStore()
    client = FakeClient(bars={n: week() for n in names}, fail={"CCC"})

    res = await backfill(client, store_, start=START, now=FRI_EVENING, symbols=names)

    assert res.failed == ["CCC", "DDD"]
    assert (res.symbols_done, res.symbols_total) == (3, 5)
    assert store_.written_symbols == ["AAA", "BBB", "EEE"]  # stored chunks stay stored
    assert store_.ranges_refreshed == 1

    healed = FakeClient(bars={n: week() for n in names})
    again = await backfill(healed, store_, start=START, now=FRI_EVENING, symbols=names)

    assert healed.requested_symbols == ["CCC", "DDD"]
    assert again.failed == [] and again.symbols_done == 5


async def test_backfill_survives_a_store_failure_in_one_chunk(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 1)
    store_ = FakeStore(fail_upsert={"BBB"})
    client = FakeClient(bars={n: week() for n in ("AAA", "BBB", "CCC")})

    res = await backfill(
        client, store_, start=START, now=FRI_EVENING, symbols=["AAA", "BBB", "CCC"]
    )

    assert res.failed == ["BBB"] and store_.written_symbols == ["AAA", "CCC"]


class _LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        def record(event: str, **kw: Any) -> None:
            self.events.append((level, event, kw))

        return record


async def test_backfill_failure_log_names_the_error_but_not_the_credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key-id-value")
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-value")
    rec = _LogRecorder()
    monkeypatch.setattr(ingest, "log", rec)

    class Leaky(FakeClient):
        async def daily_bars(self, symbols, start, end, now=None, adjustment="all"):  # type: ignore[override]
            if adjustment == "raw":
                return await super().daily_bars(symbols, start, end, now, adjustment)
            raise AlpacaError("request failed for s3cr3t-value, key-id-value")

    res = await backfill(Leaky(), FakeStore(), start=START, now=FRI_EVENING, symbols=["AAA"])

    assert res.failed == ["AAA"]
    (warning,) = [e for e in rec.events if e[0] == "warning"]
    text = repr(warning)
    assert "request failed" in text and "AlpacaError" in text
    assert "s3cr3t-value" not in text and "key-id-value" not in text


async def test_backfill_counts_a_symbol_without_bars_as_done():
    """A delisted name can have no bars in the window; that is not a failure."""
    client = FakeClient(bars={"AAA": week()})

    res = await backfill(
        client, FakeStore(), start=START, now=FRI_EVENING, symbols=["AAA", "NOBARS"]
    )

    assert res.failed == [] and (res.symbols_done, res.symbols_total, res.rows) == (2, 2, 5)


async def test_backfill_with_explicit_symbols_normalises_and_skips_the_asset_list():
    store_ = FakeStore()
    client = FakeClient(bars={"AAA": week(), "BBB": week()})

    res = await backfill(
        client, store_, start=START, now=FRI_EVENING, symbols=[" aaa", "BBB", "AAA", ""]
    )

    assert client.requested_symbols == ["AAA", "BBB"] and res.symbols_total == 2
    assert client.asset_calls == []  # nothing fetched or stored for the asset table


async def test_backfill_refreshes_the_asset_list_when_none_is_stored():
    client = FakeClient(
        bars={"AAA": week(), "OLD": week(), "SPY": week()},
        active=[common("AAA"), etf("QQQQ")],
        inactive=[common("OLD")],
    )
    store_ = FakeStore()

    res = await backfill(client, store_, start=START, now=FRI_EVENING)

    assert sorted(client.requested_symbols) == ["AAA", "OLD", "SPY"]  # ETF out, delisted in
    assert res.symbols_total == 3
    assert {a["symbol"]: a["kind"] for a in store_.assets} == {
        "AAA": "common",
        "QQQQ": "etf",
        "OLD": "common",
    }


async def test_backfill_uses_the_stored_asset_list_without_calling_alpaca_for_it():
    store_ = FakeStore(assets=[common("AAA"), etf("QQQQ")])
    client = FakeClient(bars={"AAA": week(), "SPY": week()})

    await backfill(client, store_, start=START, now=FRI_EVENING)

    assert client.asset_calls == []
    assert sorted(client.requested_symbols) == ["AAA", "SPY"]


# --- incremental update ----------------------------------------------------------------


async def test_incremental_refetches_full_history_after_split():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 400.0})  # pre-split stored scale
    client = FakeClient(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})  # 4:1

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.refetched == ["AAA"] and client.full_history_requests == ["AAA"]
    assert res.failed == []
    assert store_.closes[("AAA", date(2026, 10, 15))] == 100.0  # history is on one scale now
    assert store_.ranges_refreshed == 1


async def test_incremental_does_not_refetch_when_overlap_matches():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.2})
    client = FakeClient(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})  # 0.2%

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.refetched == [] and client.full_history_requests == []
    assert store_.closes[("AAA", date(2026, 10, 16))] == 101.0  # the new day is appended
    assert (res.symbols_done, res.symbols_total, res.rows) == (1, 1, 2)
    assert store_.ranges_refreshed == 1


async def test_incremental_refetches_just_above_the_tolerance():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.6})  # 0.6% > 0.5%
    client = FakeClient(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.refetched == ["AAA"]


async def test_incremental_tolerance_is_adjustable():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.2})
    client = FakeClient(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})

    res = await incremental_update(client, store_, now=FRI_EVENING, adjust_tolerance=0.001)

    assert res.refetched == ["AAA"]


async def test_incremental_compares_the_earliest_overlap_bar_that_is_stored():
    """The window opens on bars the store never had (here the 12th and 13th)."""
    store_ = FakeStore(
        closes={("AAA", date(2026, 10, 14)): 400.0, ("AAA", date(2026, 10, 15)): 100.0}
    )
    client = FakeClient(bars={"AAA": week(100.0)})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    # the 14th (the earliest stored overlap bar) disagrees; the later agreeing bar is no excuse
    assert res.refetched == ["AAA"]


async def test_incremental_appends_without_comparing_when_nothing_overlaps_the_store():
    store_ = FakeStore(last={"AAA": date(2026, 10, 15)})  # a last bar but no stored closes
    client = FakeClient(bars={"AAA": week(100.0)})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.refetched == [] and res.symbols_done == 1 and res.rows == 5


async def test_incremental_fetches_the_last_five_sessions_ending_at_the_target():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.0})
    client = FakeClient(bars={"AAA": week(100.0)})

    await incremental_update(client, store_, now=FRI_EVENING)

    (call,) = client.calls
    assert (call["start"], call["end"], call["now"]) == (date(2026, 10, 12), FRIDAY, FRI_EVENING)


async def test_incremental_overlap_window_is_configurable():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 100.0})
    client = FakeClient(bars={"AAA": week(100.0)})

    await incremental_update(client, store_, now=FRI_EVENING, overlap_sessions=2)

    assert client.calls[0]["start"] == date(2026, 10, 15)


async def test_incremental_reaches_back_to_the_last_bar_of_a_lagging_symbol():
    """Ten sessions behind: a plain 5-session window would leave a hole after the last bar."""
    lagging = date(2026, 10, 2)
    store_ = FakeStore(
        closes={("AAA", date(2026, 10, 15)): 100.0, ("BBB", lagging): 50.0},
    )
    client = FakeClient(
        bars={
            "AAA": week(100.0),
            "BBB": [(f"2026-10-{d:02d}", 50.0) for d in (2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16)],
        }
    )

    res = await incremental_update(client, store_, now=FRI_EVENING)

    starts = {tuple(c["symbols"]): c["start"] for c in client.calls}
    assert starts == {("AAA",): date(2026, 10, 12), ("BBB",): lagging}
    assert res.refetched == [] and res.symbols_done == 2
    assert store_.closes[("BBB", date(2026, 10, 9))] == 50.0  # the hole is filled


def sessions_before_friday(n: int) -> date:
    """The session ``n`` sessions before Fri 16 Oct 2026 (the fake calendar has no holidays)."""
    return pd.bdate_range(end=FRIDAY, periods=n + 1)[0].date()


async def test_incremental_leaves_symbols_more_than_thirty_sessions_behind_to_backfill():
    store_ = FakeStore(
        closes={("NEAR", date(2026, 10, 15)): 100.0, ("FAR", sessions_before_friday(31)): 100.0}
    )
    client = FakeClient(bars={"NEAR": week(100.0), "FAR": week(100.0)})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert client.requested_symbols == ["NEAR"] and res.symbols_total == 1


async def test_incremental_includes_a_symbol_exactly_thirty_sessions_behind():
    edge = sessions_before_friday(30)
    store_ = FakeStore(closes={("EDGE", edge): 100.0})
    client = FakeClient(bars={"EDGE": week(100.0)})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.symbols_total == 1 and client.calls[0]["start"] == edge


async def test_incremental_does_not_leave_a_split_symbol_half_written_when_refetch_fails():
    """Review Focus 1: if the full refetch fails, the old scale must stay whole, not spliced."""
    store_ = FakeStore(
        closes={("AAA", date(2026, 10, 15)): 400.0, ("BBB", date(2026, 10, 15)): 50.0}
    )
    client = FakeClient(
        bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)], "BBB": week(50.0)},
        fail_full_history={"AAA"},
    )

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.failed == ["AAA"] and res.refetched == []
    assert (res.symbols_done, res.symbols_total) == (1, 2)
    assert store_.closes[("AAA", date(2026, 10, 15))] == 400.0  # untouched
    assert ("AAA", FRIDAY) not in store_.closes  # no new-scale bar spliced on
    assert store_.last["AAA"] == date(2026, 10, 15)
    assert store_.closes[("BBB", FRIDAY)] == 50.0  # the healthy symbol still updated
    assert store_.ranges_refreshed == 1


async def test_incremental_retries_a_failed_split_symbol_on_the_next_run():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 400.0})
    bars = {"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]}

    first = await incremental_update(
        FakeClient(bars=bars, fail_full_history={"AAA"}), store_, now=FRI_EVENING
    )
    second = await incremental_update(FakeClient(bars=bars), store_, now=FRI_EVENING)

    assert first.failed == ["AAA"] and second.refetched == ["AAA"] and second.failed == []


async def test_incremental_treats_an_empty_refetch_as_a_failure():
    store_ = FakeStore(closes={("AAA", date(2026, 10, 15)): 400.0})

    class VanishingHistory(FakeClient):
        async def daily_bars(self, symbols, start, end, now=None, adjustment="all"):  # type: ignore[override]
            if adjustment == "all" and start <= START:
                return _bars_frame([])
            return await super().daily_bars(symbols, start, end, now, adjustment)

    client = VanishingHistory(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.failed == ["AAA"] and res.refetched == []
    assert store_.closes[("AAA", date(2026, 10, 15))] == 400.0


async def test_incremental_skips_a_failing_chunk_and_continues(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 1)
    store_ = FakeStore(closes={(s, date(2026, 10, 15)): 100.0 for s in ("AAA", "BBB", "CCC")})
    client = FakeClient(bars={s: week(100.0) for s in ("AAA", "BBB", "CCC")}, fail={"BBB"})

    res = await incremental_update(client, store_, now=FRI_EVENING)

    assert res.failed == ["BBB"] and (res.symbols_done, res.symbols_total) == (2, 3)
    assert store_.written_symbols == ["AAA", "CCC"] and store_.ranges_refreshed == 1


async def test_incremental_with_nothing_stored_still_refreshes_ranges():
    store_ = FakeStore()

    res = await incremental_update(FakeClient(), store_, now=FRI_EVENING)

    assert (res.symbols_done, res.symbols_total, res.rows) == (0, 0, 0)
    assert store_.ranges_refreshed == 1


async def test_incremental_rejects_a_non_positive_overlap():
    with pytest.raises(ValueError, match="overlap_sessions"):
        await incremental_update(FakeClient(), FakeStore(), now=FRI_EVENING, overlap_sessions=0)


# --- real store satisfies the protocol -------------------------------------------------


def test_the_store_module_has_every_function_the_protocol_names():
    for name in (
        "upsert_assets",
        "load_assets",
        "upsert_daily_bars",
        "last_bar_dates",
        "stored_close",
        "refresh_asset_bar_ranges",
    ):
        assert callable(getattr(store, name))
        assert hasattr(ingest.BarStore, name)


def test_ingest_result_failed_defaults_to_empty():
    res = IngestResult(symbols_done=1, symbols_total=1, rows=3, refetched=[])
    assert res.failed == []


# --- CLI -------------------------------------------------------------------------------


def test_help_lists_backfill_and_update_only(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert exit_.value.code == 0
    assert "backfill" in out and "update" in out
    for later in ("scan", "insample", "oos"):
        assert later not in out


def test_backfill_arguments_default_and_parse():
    parser = cli.build_parser()

    default = parser.parse_args(["backfill"])
    assert (default.cmd, default.start, default.symbols) == ("backfill", START, None)

    given = parser.parse_args(["backfill", "--start", "2018-02-03", "--symbols", "aapl, msft,"])
    assert given.start == date(2018, 2, 3) and given.symbols == ["AAPL", "MSFT"]


def test_a_malformed_start_date_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["backfill", "--start", "yesterday"])

    assert exit_.value.code == 2 and "yesterday" in capsys.readouterr().err


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main([])

    assert exit_.value.code == 2


class _Recorder:
    """Swaps the CLI's collaborators for fakes and records the call order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: IngestResult) -> None:
        self.events: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.result = result
        monkeypatch.setattr(cli, "load_env", lambda: self.events.append("load_env"))
        monkeypatch.setattr(cli, "setup_logging", lambda: self.events.append("setup_logging"))
        outer = self

        class Client:
            def __init__(self) -> None:
                outer.events.append("client")

            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *_: object) -> None:
                outer.events.append("client_closed")

        async def fake_backfill(client: Any, store_: Any, **kw: Any) -> IngestResult:
            outer.events.append("backfill")
            outer.kwargs = {"client": client, "store": store_, **kw}
            return outer.result

        async def fake_update(client: Any, store_: Any, **kw: Any) -> IngestResult:
            outer.events.append("update")
            outer.kwargs = {"client": client, "store": store_, **kw}
            return outer.result

        monkeypatch.setattr(cli, "AlpacaData", Client)
        monkeypatch.setattr(cli, "backfill", fake_backfill)
        monkeypatch.setattr(cli, "incremental_update", fake_update)


def test_backfill_command_wires_env_logging_client_and_the_real_store(monkeypatch):
    rec = _Recorder(monkeypatch, IngestResult(3, 3, 42, []))

    with redirect_stdout(io.StringIO()) as out:
        cli.main(["backfill", "--start", "2020-01-02", "--symbols", "aaa,bbb"])

    assert rec.events == ["load_env", "setup_logging", "client", "backfill", "client_closed"]
    assert rec.kwargs["store"] is store
    assert rec.kwargs["start"] == date(2020, 1, 2) and rec.kwargs["symbols"] == ["AAA", "BBB"]
    assert rec.kwargs["now"].tzinfo is not None
    assert "3/3" in out.getvalue() and "42" in out.getvalue()


def test_update_command_wires_env_logging_client_and_the_real_store(monkeypatch):
    rec = _Recorder(monkeypatch, IngestResult(2, 2, 10, ["AAA"]))

    with redirect_stdout(io.StringIO()) as out:
        cli.main(["update"])

    assert rec.events == ["load_env", "setup_logging", "client", "update", "client_closed"]
    assert rec.kwargs["store"] is store and rec.kwargs["now"].tzinfo is not None
    assert "AAA" in out.getvalue()


def test_a_run_with_failed_symbols_exits_nonzero_and_names_them(monkeypatch):
    _Recorder(monkeypatch, IngestResult(1, 3, 5, [], failed=["BBB", "CCC"]))

    with redirect_stdout(io.StringIO()) as out, pytest.raises(SystemExit) as exit_:
        cli.main(["backfill"])

    assert exit_.value.code == 1
    assert "BBB" in out.getvalue() and "CCC" in out.getvalue()


def test_missing_credentials_are_reported_without_a_traceback(monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)

    with pytest.raises(SystemExit) as exit_:
        cli.main(["update"])

    assert "ALPACA_API_KEY" in str(exit_.value.code)
