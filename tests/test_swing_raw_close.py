"""Swing engine: raw (unadjusted) daily closes for the point-in-time price floor.

The incident these tests pin: the universe's $10 price floor was applied to
``adjustment=all`` closes, and that adjustment rewrites history on every split. A stock
that later split 10:1 (NVDA, 2016-2020) then reads below $10 in years it traded at $30-$250,
so it was wrongly dropped from backtests, while a stock that later reverse-split wrongly
passed. The floor must see what a trader saw on the day, so the engine also ingests
``adjustment=raw`` bars (``resolution='1d_raw'``) and applies *only* the price floor to them.

Everything runs against ``httpx.MockTransport`` or in-memory fakes: no network, no database.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pandas as pd
import pytest
from swing_engine import ingest, store
from swing_engine.calendar import Session
from swing_engine.config import UniverseFilter
from swing_engine.data.alpaca import BAR_COLUMNS, RESOLUTIONS, AlpacaData, AlpacaError
from swing_engine.data.assets import AssetKind
from swing_engine.ingest import HISTORY_START, IngestResult, backfill, incremental_update, sync_raw
from swing_engine.panel import build_panel, select, truncate
from swing_engine.testing import make_bars
from swing_engine.universe import eligibility

NOW = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)  # 17:00 ET Friday: the 16th is final
FRIDAY = date(2026, 10, 16)
START = date(2016, 1, 1)
COMMON = AssetKind.COMMON


# --- the Alpaca client ------------------------------------------------------------------


def _one_bar_client() -> tuple[AlpacaData, list[httpx.Request]]:
    captured: list[httpx.Request] = []
    bar = {"t": "2024-01-02T05:00:00Z", "o": 99, "h": 101, "l": 98, "c": 100, "v": 1000, "n": 5}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"bars": {"AAPL": [bar]}, "next_page_token": None})

    client = AlpacaData(
        key="k", secret="s", transport=httpx.MockTransport(handler), min_interval_s=0.0
    )
    return client, captured


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


async def test_daily_bars_raw_sends_adjustment_raw_and_labels_1d_raw():
    c, captured = _one_bar_client()
    async with c:
        df = await c.daily_bars(
            ["AAPL"], date(2024, 1, 1), date(2024, 1, 31), now=NOW, adjustment="raw"
        )
    qs = _query(captured[0])
    assert qs["adjustment"] == ["raw"] and (df["resolution"] == "1d_raw").all()
    assert qs["feed"] == ["sip"] and qs["timeframe"] == ["1Day"]
    assert list(df.columns) == BAR_COLUMNS and df["close"].tolist() == [100.0]
    assert df["ts"].iloc[0] == pd.Timestamp("2024-01-02", tz="UTC")  # the same session-date rule


async def test_the_store_accepts_what_the_client_returns_for_raw_bars():
    c, _ = _one_bar_client()
    async with c:
        df = await c.daily_bars(
            ["AAPL"], date(2024, 1, 1), date(2024, 1, 31), now=NOW, adjustment="raw"
        )
    records = store._copy_records(store._prepare_bars(df))
    assert [(r[0], r[1], r[2]) for r in records] == [
        (datetime(2024, 1, 2, tzinfo=UTC), "AAPL", "1d_raw")
    ]


async def test_daily_bars_default_is_still_adjusted_and_labelled_1d():
    c, captured = _one_bar_client()
    async with c:
        df = await c.daily_bars(["AAPL"], date(2024, 1, 1), date(2024, 1, 31), now=NOW)
    assert _query(captured[0])["adjustment"] == ["all"] and (df["resolution"] == "1d").all()


async def test_daily_bars_rejects_an_unknown_adjustment_before_any_request():
    c, captured = _one_bar_client()
    unknown: Any = "split"
    async with c:
        with pytest.raises(ValueError, match="adjustment"):
            await c.daily_bars(
                ["AAPL"], date(2024, 1, 1), date(2024, 1, 31), now=NOW, adjustment=unknown
            )
    assert captured == []


# --- the store (no database) ------------------------------------------------------------


def _raw_bars(symbol: str = "ZZTEST", closes: Sequence[float] = (10, 11, 12)) -> pd.DataFrame:
    return make_bars(symbol, list(closes)).assign(resolution="1d_raw")


def test_store_accepts_1d_raw_and_rejects_other_resolutions():
    out = store._prepare_bars(_raw_bars())
    assert set(out["resolution"]) == {"1d_raw"}
    assert {r[2] for r in store._copy_records(out)} == {"1d_raw"}
    assert store.RAW_DAILY == "1d_raw" and store.DAILY == "1d"
    # the client labels its bars with the same two strings the store accepts
    assert (RESOLUTIONS["all"], RESOLUTIONS["raw"]) == (store.DAILY, store.RAW_DAILY)
    with pytest.raises(ValueError, match="1m"):
        store._prepare_bars(_raw_bars().assign(resolution="1m"))
    with pytest.raises(ValueError, match="1d_rawx"):
        store._prepare_bars(_raw_bars().assign(resolution="1d_rawx"))


def test_store_still_treats_a_missing_resolution_as_adjusted_daily():
    out = store._prepare_bars(make_bars("ZZTEST", [10, 11, 12]))
    assert set(out["resolution"]) == {"1d"}


def test_one_symbol_day_keeps_both_its_adjusted_and_its_raw_bar():
    """The primary key is (symbol, resolution, ts): the two series must not dedupe each other."""
    adjusted = make_bars("ZZTEST", [5, 5.5, 6]).assign(resolution="1d")
    out = store._prepare_bars(pd.concat([adjusted, _raw_bars(closes=(50, 55, 60))]))
    assert len(out) == 6
    assert out.groupby("resolution")["close"].apply(list).to_dict() == {
        "1d": [5.0, 5.5, 6.0],
        "1d_raw": [50.0, 55.0, 60.0],
    }


def test_duplicate_raw_bars_collapse_to_the_last_one():
    out = store._prepare_bars(pd.concat([_raw_bars(closes=(10, 11, 12)), _raw_bars().iloc[:1]]))
    assert len(out) == 3


class _Db:
    """Stands in for ``session_scope`` and records the statements that are executed."""

    def __init__(self) -> None:
        self.executed: list[tuple[Any, Any]] = []

    @asynccontextmanager
    async def scope(self):
        yield self

    async def execute(self, statement: Any, params: Any = None) -> _Db:
        self.executed.append((statement, params))
        return self

    def all(self) -> list[Any]:
        return []


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    fake = _Db()
    monkeypatch.setattr(store, "session_scope", fake.scope)
    return fake


async def test_reads_default_to_adjusted_and_accept_raw(db):
    await store.last_bar_dates()
    await store.last_bar_dates("1d_raw")
    await store.load_daily_bars(["AAA"], date(2020, 1, 1), date(2020, 2, 1))
    await store.load_daily_bars(["AAA"], date(2020, 1, 1), date(2020, 2, 1), resolution="1d_raw")
    await store.load_daily_bars(None, date(2020, 1, 1), date(2020, 2, 1), resolution="1d_raw")

    literals = [("'1d_raw'" in s.text, "'1d'" in s.text) for s, _ in db.executed]
    assert literals == [(False, True), (True, False), (False, True), (True, False), (True, False)]
    # the parameters are the ones the 1d reads always had
    assert db.executed[3][1] == db.executed[2][1]


async def test_reads_reject_any_other_resolution_before_querying(db):
    for resolution in ("1m", "1D", ""):
        with pytest.raises(ValueError, match="resolution"):
            await store.last_bar_dates(resolution)
        with pytest.raises(ValueError, match="resolution"):
            await store.load_daily_bars(["AAA"], date(2020, 1, 1), date(2020, 2, 1), resolution)
        with pytest.raises(ValueError, match="resolution"):
            await store.load_daily_bars([], date(2020, 1, 1), date(2020, 2, 1), resolution)
    assert db.executed == []


@pytest.mark.parametrize(
    "statement", [store._STORED_CLOSE_SQL, store._LIQUID_SQL, store._REFRESH_RANGES_SQL]
)
def test_close_lookup_liquidity_and_asset_ranges_stay_adjusted_only(statement):
    assert "resolution = '1d'" in statement.text and "1d_raw" not in statement.text


# --- the panel and the price floor ------------------------------------------------------


def _adjusted_and_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    adjusted = pd.concat([make_bars("AAA", [6.0] * 10), make_bars("BBB", [20.0] * 10)])
    raw = pd.concat([make_bars("AAA", [12.0] * 10), make_bars("BBB", [2.0] * 10)])
    return adjusted, raw


def test_build_panel_carries_raw_close_through_truncate_and_select():
    adj_bars, raw_bars = _adjusted_and_raw()
    p = build_panel(adj_bars, raw_bars=raw_bars)

    assert p.raw_close is not None and p.raw_close.shape == p.close.shape
    assert p.raw_close["AAA"].tolist() == [12.0] * 10 and p.close["AAA"].tolist() == [6.0] * 10

    t = select(truncate(p, p.dates()[5]), ["AAA"])
    assert t.raw_close is not None
    assert t.raw_close.index[-1] == p.dates()[5] and list(t.raw_close.columns) == ["AAA"]
    assert t.raw_close.shape == t.close.shape
    assert build_panel(adj_bars).raw_close is None


def test_a_panel_without_raw_stays_without_raw_through_truncate_and_select():
    adj_bars, _ = _adjusted_and_raw()
    p = build_panel(adj_bars)
    assert truncate(p, p.dates()[5]).raw_close is None
    assert select(p, ["BBB"]).raw_close is None


def test_raw_close_is_reindexed_onto_the_adjusted_panel():
    adj_bars, raw_bars = _adjusted_and_raw()
    raw_bars = pd.concat(
        [
            raw_bars[~((raw_bars["symbol"] == "AAA") & (raw_bars["ts"] == raw_bars["ts"].iloc[3]))],
            make_bars("ZZZ", [1.0] * 10),  # a symbol the adjusted panel does not have
            make_bars("BBB", [9.0] * 3, start="2019-06-03"),  # sessions it does not have
        ]
    )
    p = build_panel(adj_bars, raw_bars=raw_bars)

    assert p.raw_close is not None
    assert list(p.raw_close.columns) == ["AAA", "BBB"] and p.raw_close.index.equals(p.close.index)
    assert p.raw_close.index.tz is None and p.raw_close.index.name is None
    assert p.raw_close.columns.name is None
    assert p.raw_close["AAA"].isna().sum() == 1 and p.raw_close["AAA"].isna().iloc[3]  # not filled
    assert p.raw_close["BBB"].tolist() == [2.0] * 10


def test_a_symbol_with_no_raw_bars_at_all_is_an_all_missing_column():
    adj_bars, raw_bars = _adjusted_and_raw()
    p = build_panel(adj_bars, raw_bars=raw_bars[raw_bars["symbol"] == "AAA"])
    assert p.raw_close is not None and p.raw_close["BBB"].isna().all()


def test_empty_raw_bars_give_an_all_missing_raw_close():
    adj_bars, _ = _adjusted_and_raw()
    p = build_panel(adj_bars, raw_bars=pd.DataFrame(columns=BAR_COLUMNS))
    assert p.raw_close is not None and p.raw_close.shape == p.close.shape
    assert p.raw_close.isna().all().all()


def _floor_panel(n: int = 300, volume: float = 10_000_000):
    adjusted = pd.concat(
        [
            make_bars("AAA", [6.0] * n, volume=volume),  # split 2:1 later: adjusted 6, traded 12
            make_bars("BBB", [20.0] * n, volume=volume),  # reverse split 1:10 later: traded 2
            make_bars("CCC", [15.0] * n, volume=volume),  # no raw bar on the last day
        ]
    )
    raw = pd.concat(
        [
            make_bars("AAA", [12.0] * n),
            make_bars("BBB", [2.0] * n),
            make_bars("CCC", [15.0] * n).iloc[:-1],
        ]
    )
    return build_panel(adjusted, raw_bars=raw)


def test_price_floor_uses_raw_close_point_in_time():
    # AAA: adjusted 6.0 but traded 12.0 raw -> eligible; BBB: adjusted 20.0 but traded 2.0 raw
    # -> not eligible; CCC: raw missing on the last day, adjusted 15.0 -> falls back -> eligible.
    e = eligibility(_floor_panel(), {s: COMMON for s in ("AAA", "BBB", "CCC")}, UniverseFilter())

    assert e["AAA"].iloc[-1] and not e["BBB"].iloc[-1] and e["CCC"].iloc[-1]
    assert e["AAA"].iloc[-2] and not e["BBB"].iloc[-2]


def test_a_missing_raw_cell_falls_back_to_the_adjusted_close_for_that_cell_only():
    n = 300
    adjusted = make_bars("AAA", [15.0] * n, volume=10_000_000)
    raw = make_bars("AAA", [3.0] * n).iloc[:-1]  # traded under $10 until the raw bar is missing
    e = eligibility(build_panel(adjusted, raw_bars=raw), {"AAA": COMMON}, UniverseFilter())["AAA"]
    assert not e.iloc[-2] and e.iloc[-1]


def test_price_floor_on_raw_is_inclusive_and_does_not_depend_on_the_adjusted_close():
    n = 300
    adjusted = pd.concat([make_bars(s, [500.0] * n, volume=10_000_000) for s in ("EXACT", "UNDER")])
    raw = pd.concat([make_bars("EXACT", [10.0] * n), make_bars("UNDER", [9.99] * n)])
    kinds = {"EXACT": COMMON, "UNDER": COMMON}
    e = eligibility(build_panel(adjusted, raw_bars=raw), kinds, UniverseFilter())
    assert e["EXACT"].iloc[-1] and not e["UNDER"].iloc[-1]


def test_dollar_volume_is_still_measured_on_the_adjusted_close():
    """Only the price floor moves to raw: raw $100 x 1M would clear $20M, adjusted $6 does not."""
    n = 300
    adjusted = make_bars("AAA", [6.0] * n, volume=1_000_000)
    raw = make_bars("AAA", [100.0] * n)
    e = eligibility(build_panel(adjusted, raw_bars=raw), {"AAA": COMMON}, UniverseFilter())
    assert not e["AAA"].any()


def test_a_missing_adjusted_bar_is_still_not_eligible_even_with_a_raw_bar():
    n = 300
    adjusted = make_bars("AAA", [50.0] * n, volume=10_000_000).iloc[:-1]
    adjusted = pd.concat([adjusted, make_bars("SPY", [400.0] * n)])  # keeps the full index
    raw = make_bars("AAA", [50.0] * n)
    e = eligibility(build_panel(adjusted, raw_bars=raw), {"AAA": COMMON}, UniverseFilter())["AAA"]
    assert e.iloc[-2] and not e.iloc[-1]


# --- backfill and incremental update: fakes ---------------------------------------------

Series = dict[str, list[tuple[str, float]]]


def week(close: float = 100.0) -> list[tuple[str, float]]:
    """Mon 12 through Fri 16 Oct 2026, one flat close."""
    return [(f"2026-10-{d}", close) for d in (12, 13, 14, 15, 16)]


class Client:
    """Stands in for ``AlpacaData``: adjusted and raw closes per symbol, clipped to the window."""

    def __init__(
        self,
        adjusted: Series | None = None,
        raw: Series | None = None,
        *,
        fail_adjusted: set[str] | None = None,
        fail_raw: set[str] | None = None,
    ) -> None:
        self.series = {"all": adjusted or {}, "raw": raw or {}}
        self.fail = {"all": fail_adjusted or set(), "raw": fail_raw or set()}
        self.calls: list[tuple[str, list[str], date, date]] = []

    async def daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        now: datetime | None = None,
        adjustment: str = "all",
    ) -> pd.DataFrame:
        self.calls.append((adjustment, list(symbols), start, end))
        if self.fail[adjustment] & set(symbols):
            raise AlpacaError("alpaca 500 from /v2/stocks/bars: boom")
        label = "1d_raw" if adjustment == "raw" else "1d"
        rows = [
            [day, sym, label, close, close, close, close, 1_000.0, close, 10]
            for sym in symbols
            for day, close in self.series[adjustment].get(sym, [])
            if start <= date.fromisoformat(day) <= end
        ]
        df = pd.DataFrame(rows, columns=BAR_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df

    async def assets(self, status: str) -> list[dict[str, Any]]:
        return []

    async def calendar(self, start: date, end: date) -> list[Session]:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [Session(d, time(9, 30), time(16)) for d in days if d.weekday() < 5]

    def starts(self, adjustment: str) -> dict[str, date]:
        """The fetch start of every symbol requested with ``adjustment``."""
        return {s: start for adj, syms, start, _ in self.calls for s in syms if adj == adjustment}

    def adjustments(self) -> list[str]:
        return [adj for adj, *_ in self.calls]


class Store:
    """In-memory ``BarStore`` holding adjusted (``1d``) and raw (``1d_raw``) closes."""

    def __init__(
        self,
        adjusted: dict[tuple[str, date], float] | None = None,
        raw: dict[tuple[str, date], float] | None = None,
        inactive: Sequence[str] = (),
        active: Sequence[str] = (),
    ) -> None:
        self.closes = {"1d": dict(adjusted or {}), "1d_raw": dict(raw or {})}
        self.assets = [
            {"symbol": s, "name": s, "exchange": "NASDAQ", "status": status, "kind": "common"}
            for status, names in (("active", active), ("inactive", inactive))
            for s in names
        ]
        self.upserts: list[pd.DataFrame] = []

    async def upsert_assets(self, rows: Sequence[dict[str, Any]]) -> int:
        return len(rows)

    async def load_assets(self) -> pd.DataFrame:
        return pd.DataFrame(self.assets, columns=["symbol", "name", "exchange", "status", "kind"])

    async def upsert_daily_bars(self, df: pd.DataFrame) -> int:
        self.upserts.append(df)
        for row in df.itertuples():
            self.closes[row.resolution][(row.symbol, row.ts.date())] = float(row.close)
        return len(df)

    async def last_bar_dates(self, resolution: str = "1d") -> dict[str, date]:
        out: dict[str, date] = {}
        for symbol, day in self.closes[resolution]:
            out[symbol] = max(day, out.get(symbol, day))
        return out

    async def stored_close(self, symbol: str, day: date) -> float | None:
        return self.closes["1d"].get((symbol, day))

    async def refresh_asset_bar_ranges(self) -> None:
        return None


def stored(symbol: str, day: date, close: float = 100.0) -> dict[tuple[str, date], float]:
    return {(symbol, day): close}


# --- backfill ----------------------------------------------------------------------------


async def test_backfill_syncs_raw_full_history_for_new_and_appends_for_behind():
    thu = date(2026, 10, 15)
    store_ = Store(
        adjusted={
            **stored("BEHIND", thu),
            **stored("DONE", FRIDAY),
            **stored("OLD", date(2024, 3, 14)),
            **stored("UPGRADE", FRIDAY),
        },
        raw={
            **stored("BEHIND", thu),
            **stored("DONE", FRIDAY),
            **stored("OLD", date(2024, 3, 14)),
        },
        active=["NEW", "BEHIND", "DONE", "UPGRADE"],
        inactive=["OLD"],
    )
    both = {s: week() for s in ("NEW", "BEHIND", "DONE", "UPGRADE", "OLD")}
    client = Client(adjusted=both, raw=both)

    res = await backfill(
        client,
        store_,
        start=START,
        now=NOW,
        symbols=["NEW", "BEHIND", "DONE", "OLD", "UPGRADE"],
    )

    assert client.starts("raw") == {"NEW": START, "BEHIND": thu, "UPGRADE": START}
    # NEW and BEHIND are fetched as adjusted bars; the raw-only gap (UPGRADE) is not.
    assert client.starts("all") == {"NEW": START, "BEHIND": thu}
    assert (res.symbols_done, res.symbols_total, res.failed) == (5, 5, [])
    # NEW: 5 bars; BEHIND: the overlap day plus Friday. `rows` never counts raw bars.
    assert res.rows == 5 + 2 and res.raw_rows == 5 + 2 + 5  # raw: NEW, BEHIND, UPGRADE
    assert store_.closes["1d_raw"][("NEW", FRIDAY)] == 100.0
    assert store_.closes["1d_raw"][("UPGRADE", date(2026, 10, 12))] == 100.0
    assert ("OLD", FRIDAY) not in store_.closes["1d_raw"]  # delisted with raw bars: skipped
    assert all(df["resolution"].nunique() == 1 for df in store_.upserts)


async def test_backfill_raw_pass_follows_the_adjusted_pass_and_ends_at_the_same_session():
    client = Client(adjusted={"AAA": week()}, raw={"AAA": week()})

    await backfill(client, Store(), start=START, now=NOW, symbols=["AAA"])

    assert client.adjustments() == ["all", "raw"]
    assert [end for *_, end in client.calls] == [FRIDAY, FRIDAY]


async def test_backfill_skips_raw_for_a_symbol_already_at_the_target_and_delisted_with_raw():
    store_ = Store(
        adjusted={**stored("DONE", FRIDAY), **stored("OLD", date(2024, 3, 14))},
        raw={**stored("DONE", FRIDAY), **stored("OLD", date(2024, 3, 14))},
        active=["DONE"],
        inactive=["OLD"],
    )
    client = Client()

    res = await backfill(client, store_, start=START, now=NOW, symbols=["DONE", "OLD"])

    assert client.calls == [] and store_.upserts == []
    assert (res.symbols_done, res.symbols_total, res.rows, res.raw_rows) == (2, 2, 0, 0)


async def test_backfill_still_loads_raw_for_a_delisted_symbol_that_has_none():
    store_ = Store(adjusted=stored("OLD", date(2024, 3, 14)), inactive=["OLD"])
    client = Client(raw={"OLD": [("2024-03-13", 30.0), ("2024-03-14", 31.0)]})

    res = await backfill(client, store_, start=START, now=NOW, symbols=["OLD"])

    assert client.starts("raw") == {"OLD": START} and res.raw_rows == 2
    assert store_.closes["1d_raw"][("OLD", date(2024, 3, 14))] == 31.0


async def test_a_split_refetches_adjusted_history_but_never_touches_raw_history():
    """Raw closes are what printed, so a 4:1 split changes nothing before the split date."""
    nine = date(2026, 10, 9)
    store_ = Store(adjusted=stored("AAA", nine, 400.0), raw=stored("AAA", nine, 400.0))
    client = Client(
        adjusted={"AAA": [("2026-10-09", 100.0), *week(100.0)]},  # adjusted 4:1
        raw={"AAA": [("2026-10-09", 400.0), *week(100.0)]},  # split took effect on the 12th
    )

    res = await backfill(client, store_, start=START, now=NOW, symbols=["AAA"])

    assert res.refetched == ["AAA"] and res.failed == []
    assert [(adj, start) for adj, _, start, _ in client.calls if adj == "raw"] == [("raw", nine)]
    assert store_.closes["1d_raw"][("AAA", nine)] == 400.0  # pre-split print is kept
    assert store_.closes["1d_raw"][("AAA", FRIDAY)] == 100.0
    assert store_.closes["1d"][("AAA", nine)] == 100.0  # adjusted history is on the new scale


async def test_a_raw_chunk_failure_is_isolated_reported_and_resumed(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 1)
    names = ["AAA", "BBB", "CCC"]
    store_ = Store()
    series = {n: week() for n in names}

    first = await backfill(
        Client(adjusted=series, raw=series, fail_raw={"BBB"}),
        store_,
        start=START,
        now=NOW,
        symbols=names,
    )

    assert first.failed == ["BBB"] and (first.symbols_done, first.symbols_total) == (2, 3)
    assert first.rows == 15 and first.raw_rows == 10  # BBB's adjusted bars are stored regardless
    assert {s for s, _ in store_.closes["1d_raw"]} == {"AAA", "CCC"}

    healed = Client(adjusted=series, raw=series)
    second = await backfill(healed, store_, start=START, now=NOW, symbols=names)

    assert healed.starts("raw") == {"BBB": START} and healed.starts("all") == {}
    assert second.failed == [] and (second.symbols_done, second.raw_rows) == (3, 5)


async def test_a_symbol_that_fails_in_both_passes_is_listed_and_counted_once(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 1)
    series = {"AAA": week(), "BBB": week()}
    client = Client(adjusted=series, raw=series, fail_adjusted={"BBB"}, fail_raw={"BBB"})

    res = await backfill(client, Store(), start=START, now=NOW, symbols=["AAA", "BBB"])

    assert res.failed == ["BBB"] and (res.symbols_done, res.symbols_total) == (1, 2)


async def test_a_raw_store_failure_does_not_stop_the_other_chunks(monkeypatch):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 1)

    class FailingRawStore(Store):
        async def upsert_daily_bars(self, df: pd.DataFrame) -> int:
            if (df["resolution"] == "1d_raw").any() and "BBB" in set(df["symbol"]):
                raise RuntimeError("database is down")
            return await super().upsert_daily_bars(df)

    series = {n: week() for n in ("AAA", "BBB", "CCC")}
    store_ = FailingRawStore()

    res = await backfill(
        Client(adjusted=series, raw=series), store_, start=START, now=NOW, symbols=list(series)
    )

    assert res.failed == ["BBB"] and res.raw_rows == 10


# --- incremental update ------------------------------------------------------------------


async def test_incremental_update_appends_raw_without_scale_check():
    """Raw history is never rewritten, so a raw overlap that disagrees is not a split."""
    thu = date(2026, 10, 15)
    store_ = Store(adjusted=stored("AAA", thu, 100.0), raw=stored("AAA", thu, 400.0))
    client = Client(
        adjusted={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]},
        raw={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]},
    )

    res = await incremental_update(client, store_, now=NOW)

    assert res.refetched == [] and res.failed == []
    assert client.starts("raw") == {"AAA": thu}  # from its newest raw bar, never from scratch
    assert all(start > HISTORY_START for _, _, start, _ in client.calls)
    assert store_.closes["1d_raw"][("AAA", FRIDAY)] == 101.0
    assert (res.rows, res.raw_rows) == (2, 2)


async def test_incremental_update_loads_the_whole_raw_history_of_a_symbol_without_any():
    """The first update after raw bars were introduced catches every stored symbol up."""
    store_ = Store(adjusted=stored("AAA", date(2026, 10, 15), 100.0))
    client = Client(adjusted={"AAA": week()}, raw={"AAA": week()})

    res = await incremental_update(client, store_, now=NOW)

    assert client.starts("raw") == {"AAA": HISTORY_START} and res.raw_rows == 5
    assert res.refetched == []  # nothing was refetched: raw simply had no history yet


async def test_incremental_update_only_syncs_raw_for_the_symbols_it_maintains():
    stale = pd.bdate_range(end=FRIDAY, periods=32)[0].date()  # 31 sessions behind the target
    store_ = Store(adjusted={**stored("NEAR", date(2026, 10, 15)), **stored("FAR", stale)})
    series = {"NEAR": week(), "FAR": week()}
    client = Client(adjusted=series, raw=series)

    res = await incremental_update(client, store_, now=NOW)

    assert list(client.starts("raw")) == ["NEAR"] and res.symbols_total == 1


async def test_incremental_update_reports_raw_failures_and_keeps_the_adjusted_bars():
    store_ = Store(adjusted=stored("AAA", date(2026, 10, 15)))
    client = Client(adjusted={"AAA": week()}, raw={"AAA": week()}, fail_raw={"AAA"})

    res = await incremental_update(client, store_, now=NOW)

    assert res.failed == ["AAA"] and (res.symbols_done, res.symbols_total) == (0, 1)
    assert res.rows == 5 and res.raw_rows == 0
    assert store_.closes["1d"][("AAA", FRIDAY)] == 100.0


# --- sync_raw directly -------------------------------------------------------------------


async def test_sync_raw_returns_an_ingest_result_that_counts_raw_rows_only():
    client = Client(raw={"AAA": week(), "BBB": week()})
    store_ = Store()

    res = await sync_raw(
        client, store_, symbols=["AAA", "BBB"], status={}, start=START, target=FRIDAY, now=NOW
    )

    assert res == IngestResult(2, 2, 0, [], [], raw_rows=10)
    assert client.starts("raw") == {"AAA": START, "BBB": START}


async def test_sync_raw_treats_an_unknown_status_as_active():
    store_ = Store(raw=stored("AAA", date(2026, 10, 15)))
    client = Client(raw={"AAA": week()})

    res = await sync_raw(client, store_, symbols=["AAA"], status={}, start=START, target=FRIDAY)

    assert client.starts("raw") == {"AAA": date(2026, 10, 15)} and res.raw_rows == 2


async def test_sync_raw_sorts_behind_symbols_into_chunks_that_start_at_their_oldest_bar(
    monkeypatch,
):
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 2)
    newest = {"CCC": date(2026, 10, 14), "AAA": date(2026, 10, 2), "BBB": date(2026, 10, 9)}
    store_ = Store(raw={(s, d): 50.0 for s, d in newest.items()})
    client = Client(raw={s: week(50.0) for s in newest})

    await sync_raw(
        client, store_, symbols=["CCC", "AAA", "BBB"], status={}, start=START, target=FRIDAY
    )

    assert [(symbols, start) for _, symbols, start, _ in client.calls] == [
        (["AAA", "BBB"], date(2026, 10, 2)),
        (["CCC"], date(2026, 10, 14)),
    ]


def test_ingest_result_raw_rows_defaults_to_zero():
    assert IngestResult(1, 1, 3, []).raw_rows == 0
