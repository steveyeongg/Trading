"""Daily-bar ingest: a resumable full backfill and a split-aware incremental update.

Two jobs share one rule: a daily bar is only requested for sessions that have settled. The
target session is :meth:`TradingCalendar.last_completed_session` (close plus the 16 minute
SIP delay), and every request ends there, so a partial in-progress bar never lands.

**Backfill** walks the universe in chunks, and each chunk is fetched and then stored in one
transaction before the next starts. A chunk that raises is logged and skipped, and stored
chunks stay stored. A re-run does only the work that is left, whatever session it runs in:

* no stored bars: the full history from ``start``;
* a delisted (inactive) symbol that has bars: skipped, its history is complete;
* an active symbol behind the target: caught up from its newest stored bar, and refetched in
  full only if a split or dividend moved its prices (the check below);
* a symbol already at the target: skipped.

**The adjustment trap.** Bars are requested with ``adjustment=all``, so when a stock splits
or pays a dividend Alpaca rewrites that stock's whole history. Append only the new days to
the old rows and two price scales meet at the join, which reads as a crash (or a spike) that
never happened. So whenever new bars are added to a stored symbol, the job refetches an
overlap and compares the earliest bar that is also stored with its stored close. If they
differ by more than the tolerance the symbol's full history is refetched instead. Until that
succeeds none of the symbol's new bars are written, so a failed refetch leaves it whole on
its old scale, and it is retried on the next run. The incremental update runs this check on
the last few sessions of every stored symbol.

**Raw closes.** The universe's price floor has to see the price a stock traded at on the day,
which ``adjustment=all`` rewrites after every split. So after the adjusted pass both jobs also
run :func:`sync_raw`, which keeps ``adjustment=raw`` bars (``resolution='1d_raw'``) for the same
symbols. Raw history is never rewritten by a corporate action, so it is only ever appended and
never needs the scale check or a refetch. Its failures are reported with the adjusted ones.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import pandas as pd
from atlas_shared.logging import get_logger

from swing_engine.calendar import ET, Session, TradingCalendar
from swing_engine.data.assets import candidate_symbols, classify_asset
from swing_engine.store import RAW_DAILY

log = get_logger("swing.ingest")

# The oldest session the engine keeps, and where a split-triggered refetch starts.
HISTORY_START = date(2016, 1, 1)
# Symbols per fetch-and-store round. The Alpaca client holds a whole call in memory and does
# not retry transport errors, so a bounded chunk caps both the memory and the loss.
CHUNK_SIZE = 100
# The update only maintains symbols whose newest bar is at most this many sessions behind.
MAX_STALE_SESSIONS = 30
CALENDAR_LOOKBACK_DAYS = 10
# A fetched overlap bar that differs from the stored close by more than this (relative)
# means the adjustment moved. Dividend yields below it are accepted as noise.
ADJUST_TOLERANCE = 0.005

_SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_API_SECRET")


class BarSource(Protocol):
    """The part of ``AlpacaData`` the ingest jobs use."""

    async def daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        now: datetime | None = None,
        adjustment: Literal["all", "raw"] = "all",
    ) -> pd.DataFrame: ...

    async def assets(self, status: Literal["active", "inactive"]) -> list[dict[str, Any]]: ...

    async def calendar(self, start: date, end: date) -> list[Session]: ...


class BarStore(Protocol):
    """The part of :mod:`swing_engine.store` the ingest jobs use (the module satisfies it)."""

    async def upsert_assets(self, rows: Sequence[dict[str, Any]]) -> int: ...

    async def load_assets(self) -> pd.DataFrame: ...

    async def upsert_daily_bars(self, df: pd.DataFrame) -> int: ...

    async def last_bar_dates(self, resolution: str = "1d") -> dict[str, date]: ...

    async def stored_close(self, symbol: str, day: date) -> float | None: ...

    async def refresh_asset_bar_ranges(self) -> None: ...


@dataclass(frozen=True)
class IngestResult:
    """What a run did.

    ``symbols_done`` is the symbols that ended up current (already up to date, complete and
    delisted, or fetched cleanly), so ``symbols_total - symbols_done == len(failed)``. ``rows``
    counts adjusted bars written and ``raw_rows`` the raw (unadjusted) ones. ``refetched`` lists
    symbols that already had history and got all of it replaced because their price adjustment
    moved; ``failed`` lists symbols a re-run still has to do, whether the adjusted pass, the raw
    pass or both failed for them.
    """

    symbols_done: int
    symbols_total: int
    rows: int
    refetched: list[str]
    failed: list[str] = field(default_factory=list)
    raw_rows: int = 0


def _describe(exc: BaseException) -> str:
    """An exception as one log-safe line: the type and message with credentials masked."""
    text = f"{type(exc).__name__}: {exc}"
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def _chunks[T](items: Sequence[T]) -> Iterator[list[T]]:
    for i in range(0, len(items), CHUNK_SIZE):
        yield list(items[i : i + CHUNK_SIZE])


async def _calendar(client: BarSource, now: datetime, lookback_days: int) -> TradingCalendar:
    """The sessions around ``now``: from ``lookback_days`` back to tomorrow (New York dates)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(ET).date()
    sessions = await client.calendar(today - timedelta(days=lookback_days), today + timedelta(1))
    return TradingCalendar(sessions)


async def refresh_assets(client: BarSource, store: BarStore) -> list[dict[str, Any]]:
    """Fetch Alpaca's active and inactive assets, classify them, store them, return the rows.

    Delisted names live in the inactive list and are needed for a survivorship-free history.
    Each row keeps Alpaca's fields and gains ``kind`` (an :class:`AssetKind` value) and the
    ``status`` of the list it came from. A ticker reused by a new company can be in both
    lists; the active entry wins because ``assets`` is keyed by symbol.
    """
    rows: dict[str, dict[str, Any]] = {}
    for status in ("active", "inactive"):
        for raw in await client.assets(status):
            symbol = str(raw.get("symbol") or "").strip().upper()
            if symbol and symbol not in rows:
                rows[symbol] = {
                    **raw,
                    "symbol": symbol,
                    "status": status,
                    "kind": classify_asset(raw).value,
                }
    out = list(rows.values())
    await store.upsert_assets(out)
    log.info("ingest.assets_refreshed", n=len(out))
    return out


def _tickers(symbols: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))


async def _universe(
    client: BarSource, store: BarStore, symbols: Sequence[str] | None, *, refresh: bool = False
) -> tuple[list[str], dict[str, str]]:
    """The symbols to backfill, and the Alpaca status of each one the asset table knows.

    The caller's list wins; otherwise the candidates come from the asset table. The table is
    refreshed from Alpaca first when it is empty (and no explicit list was given) or when
    ``refresh`` is true, which is how new listings and delistings are picked up: a stored
    table is otherwise reused as it is. A symbol the table does not know has no status and
    is treated as active.
    """
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of tickers, not a single string")
    stored = await store.load_assets()
    if stored.empty and not refresh:
        if symbols is not None:
            return _tickers(symbols), {}
        rows = await refresh_assets(client, store)
        return candidate_symbols(rows), {r["symbol"]: r["status"] for r in rows}
    if refresh:
        await refresh_assets(client, store)
        stored = await store.load_assets()
    status = {str(a): str(b) for a, b in zip(stored["symbol"], stored["status"], strict=True)}
    if symbols is not None:
        return _tickers(symbols), status
    return candidate_symbols(stored.to_dict("records")), status


@dataclass
class _Pass:
    """What one pass over a list of chunks produced."""

    rows: int = 0
    failed: list[str] = field(default_factory=list)
    returned: set[str] = field(default_factory=set)  # symbols that came back with bars
    moved: list[str] = field(default_factory=list)  # symbols whose price adjustment moved


def _log_chunk_failure(phase: str, chunk: Sequence[str], exc: Exception) -> None:
    log.warning(
        "ingest.chunk_failed", phase=phase, first=chunk[0], symbols=len(chunk), error=_describe(exc)
    )


async def _fetch_history(
    client: BarSource,
    store: BarStore,
    symbols: Sequence[str],
    *,
    start: date,
    target: date,
    now: datetime | None,
    phase: str,
    on_chunk: Callable[[int], None] | None = None,
    adjustment: Literal["all", "raw"] = "all",
) -> _Pass:
    """Fetch ``start`` through ``target`` for ``symbols``, storing each chunk before the next.

    A chunk that raises is logged and skipped. Its symbols land in ``failed`` and nothing of
    them is stored, because a chunk is written in one transaction.
    """
    out = _Pass()
    for chunk in _chunks(symbols):
        try:
            bars = await client.daily_bars(chunk, start, target, now=now, adjustment=adjustment)
            if not bars.empty:
                out.rows += await store.upsert_daily_bars(bars)
                out.returned.update(str(sym) for sym in bars["symbol"].unique())
        except Exception as exc:  # one bad chunk must not sink the run; a re-run resumes it
            out.failed.extend(chunk)
            _log_chunk_failure(phase, chunk, exc)
        if on_chunk is not None:
            on_chunk(len(chunk))
    return out


async def _price_scale_moved(
    store: BarStore, symbol: str, bars: pd.DataFrame, tolerance: float
) -> bool:
    """True when the earliest fetched bar that is also stored closed differently.

    ``adjustment=all`` shifts every bar before a split or dividend, so the earliest overlap
    bar is the one that shows it. Bars the store never had cannot be compared and are
    skipped; with nothing to compare there is nothing to notice.
    """
    for ts, close in zip(bars["ts"], bars["close"], strict=True):
        if pd.isna(close):
            continue
        stored = await store.stored_close(symbol, ts.date())
        if stored is None:
            continue
        moved = bool(abs(close - stored) > tolerance * abs(stored))
        if moved:
            log.info(
                "ingest.price_scale_moved",
                symbol=symbol,
                day=ts.date().isoformat(),
                stored=stored,
                fetched=float(close),
            )
        return moved
    return False


async def _moved_symbols(store: BarStore, bars: pd.DataFrame, tolerance: float) -> set[str]:
    moved: set[str] = set()
    for symbol, frame in bars.groupby("symbol", sort=False):
        if await _price_scale_moved(store, str(symbol), frame.sort_values("ts"), tolerance):
            moved.add(str(symbol))
    return moved


async def _catch_up(
    client: BarSource,
    store: BarStore,
    jobs: Sequence[tuple[date, list[str]]],
    *,
    target: date,
    now: datetime,
    tolerance: float,
    on_chunk: Callable[[int], None] | None = None,
) -> _Pass:
    """Append recent bars to symbols that already have history; flag those whose scale moved.

    Each job is a chunk of stored symbols and the date to fetch it from (an overlap with what
    is stored). A flagged symbol's fetched bars are withheld and it is returned in ``moved``
    for :func:`_refetch_moved`, so nothing of it is written until its whole history is.
    """
    out = _Pass()
    for fetch_from, chunk in jobs:
        changed: set[str] = set()
        try:
            bars = await client.daily_bars(chunk, fetch_from, target, now=now)
            changed = await _moved_symbols(store, bars, tolerance)
            keep = bars[~bars["symbol"].isin(changed)]
            if not keep.empty:
                out.rows += await store.upsert_daily_bars(keep)
        except Exception as exc:
            out.failed.extend(chunk)
            changed = set()
            _log_chunk_failure("catch_up", chunk, exc)
        out.moved.extend(sorted(changed))
        if on_chunk is not None:
            on_chunk(len(chunk))
    return out


async def _refetch_moved(
    client: BarSource,
    store: BarStore,
    moved: Sequence[str],
    *,
    start: date,
    target: date,
    now: datetime,
) -> tuple[int, list[str], list[str]]:
    """Replace the history of symbols whose adjustment moved: ``(rows, refetched, failed)``.

    Their catch-up bars were withheld, so a failed (or empty) refetch leaves them whole on the
    old scale and they are flagged again next run.
    """
    symbols = sorted(moved)
    redo = await _fetch_history(
        client, store, symbols, start=start, target=target, now=now, phase="refetch"
    )
    refetched = [s for s in symbols if s in redo.returned]
    failed = set(redo.failed)
    empty = [s for s in symbols if s not in redo.returned and s not in failed]
    if empty:
        log.warning("ingest.refetch_empty", symbols=empty[:20], count=len(empty))
    return redo.rows, refetched, [*redo.failed, *empty]


async def sync_raw(
    client: BarSource,
    store: BarStore,
    *,
    symbols: Sequence[str],
    status: Mapping[str, str],
    start: date,
    target: date,
    now: datetime | None = None,
) -> IngestResult:
    """Bring the raw (``adjustment=raw``) daily bars of ``symbols`` up to ``target``.

    Raw prices are what printed on the day, so a split or dividend never rewrites them: this
    only ever appends, with no scale check and no refetch. Each symbol is handled by what raw
    history is stored for it:

    * none: the full history from ``start``;
    * some, but ``status`` says the asset is inactive (delisted): skipped, it is complete;
    * some, active, newest before ``target``: fetched from its newest raw bar to ``target``;
    * some, through ``target`` or later: skipped.

    A symbol missing from ``status`` is treated as active. Chunking and failure handling are
    those of the adjusted pass: a failing chunk is logged and its symbols go to ``failed``.
    ``now`` is passed to the client for its SIP delay clamp. The result carries the raw bars
    written in ``raw_rows`` (``rows`` is 0, since it counts adjusted bars).
    """
    last = await store.last_bar_dates(RAW_DAILY)
    new: list[str] = []
    behind: list[tuple[date, str]] = []
    for symbol in symbols:
        newest = last.get(symbol)
        if newest is None:
            new.append(symbol)
        elif status.get(symbol) != "inactive" and newest < target:
            behind.append((newest, symbol))
    behind.sort()

    fresh = await _fetch_history(
        client,
        store,
        new,
        start=start,
        target=target,
        now=now,
        phase="raw_backfill",
        adjustment="raw",
    )
    rows = fresh.rows
    failed = list(fresh.failed)
    for group in _chunks(behind):  # sorted by newest bar, so the first one is the oldest
        caught = await _fetch_history(
            client,
            store,
            [symbol for _, symbol in group],
            start=group[0][0],
            target=target,
            now=now,
            phase="raw_catch_up",
            adjustment="raw",
        )
        rows += caught.rows
        failed.extend(caught.failed)
    log.info(
        "ingest.raw_synced",
        target=target.isoformat(),
        symbols=len(symbols),
        new=len(new),
        caught_up=len(behind),
        failed=len(failed),
        rows=rows,
    )
    return IngestResult(len(symbols) - len(failed), len(symbols), 0, [], sorted(failed), rows)


async def backfill(
    client: BarSource,
    store: BarStore,
    *,
    start: date,
    now: datetime,
    symbols: Sequence[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
    refresh_assets: bool = False,
) -> IngestResult:
    """Load daily bars from ``start`` through the last completed session; safe to re-run.

    ``symbols`` defaults to every candidate in the stored asset table (listed common stock,
    active and delisted, plus the reference ETF), refreshing that table first when it is
    empty. A re-run otherwise reuses the stored table, so it neither discovers new listings
    nor notices delistings: ``refresh_assets=True`` re-fetches Alpaca's active and inactive
    lists first (also when ``symbols`` is given, so their status is current). Each symbol
    is handled by what is already stored for it:

    * no bars: fetched whole from ``start``;
    * bars, but the asset is inactive (delisted): skipped, its history is complete;
    * bars, active, newest before the target session: caught up from its newest stored bar.
      If the overlap shows the price adjustment moved, the full history is refetched from
      ``start`` instead (and the symbol is listed in ``refetched``);
    * bars through the target session or later: skipped.

    The adjusted pass is followed by :func:`sync_raw` for the same symbols (raw bars for the
    price floor), and its failures are merged into ``failed``.

    ``progress(handled, total)`` is called once up front (with the symbols that needed no
    work) and after every chunk of the adjusted pass, ending at ``(total, total)`` even when
    some chunks failed. The raw pass does not report progress.
    """
    cal = await _calendar(client, now, CALENDAR_LOOKBACK_DAYS)
    target = cal.last_completed_session(now)
    wanted, status = await _universe(client, store, symbols, refresh=refresh_assets)
    last = await store.last_bar_dates()

    new: list[str] = []
    behind: list[tuple[date, str]] = []
    for symbol in wanted:
        newest = last.get(symbol)
        if newest is None:
            new.append(symbol)
        elif status.get(symbol) != "inactive" and newest < target:
            behind.append((newest, symbol))
    # Sorted by newest bar so a chunk's fetch start (its oldest bar) wastes little on the rest.
    behind.sort()
    jobs = [(group[0][0], [sym for _, sym in group]) for group in _chunks(behind)]

    total = len(wanted)
    handled = total - len(new) - len(behind)
    if progress is not None:
        progress(handled, total)

    def advance(n: int) -> None:
        nonlocal handled
        handled += n
        if progress is not None:
            progress(handled, total)

    fresh = await _fetch_history(
        client, store, new, start=start, target=target, now=now, phase="backfill", on_chunk=advance
    )
    caught = await _catch_up(
        client, store, jobs, target=target, now=now, tolerance=ADJUST_TOLERANCE, on_chunk=advance
    )
    redo_rows, refetched, redo_failed = await _refetch_moved(
        client, store, caught.moved, start=start, target=target, now=now
    )
    raw = await sync_raw(
        client, store, symbols=wanted, status=status, start=start, target=target, now=now
    )

    failed = {*fresh.failed, *caught.failed, *redo_failed, *raw.failed}
    rows = fresh.rows + caught.rows + redo_rows
    await store.refresh_asset_bar_ranges()
    log.info(
        "ingest.backfill_done",
        target=target.isoformat(),
        symbols=total,
        new=len(new),
        caught_up=len(behind),
        refetched=len(refetched),
        failed=len(failed),
        rows=rows,
        raw_rows=raw.raw_rows,
    )
    return IngestResult(total - len(failed), total, rows, refetched, sorted(failed), raw.raw_rows)


async def incremental_update(
    client: BarSource,
    store: BarStore,
    *,
    now: datetime,
    overlap_sessions: int = 5,
    adjust_tolerance: float = ADJUST_TOLERANCE,
) -> IngestResult:
    """Append the newest sessions to every stored symbol, replacing history after a split.

    Each symbol whose newest bar is at most :data:`MAX_STALE_SESSIONS` sessions behind the
    target is fetched from the earlier of the last ``overlap_sessions`` sessions and its own
    newest bar, so the stored series never gets a hole. Symbols further behind are left to
    :func:`backfill`. The earliest overlap bar is compared with the stored close: a relative
    difference above ``adjust_tolerance`` means the adjustment moved, and that symbol's
    history is refetched from :data:`HISTORY_START`. Its overlap bars are withheld until
    then, so a failed refetch (the symbol lands in ``failed``) changes nothing.

    Dividends adjust history by their yield, so a difference under the tolerance is accepted
    as the stored scale and the older rows drift from Alpaca's current figures by less than
    that. New listings are not discovered here; they enter through :func:`backfill` with
    ``refresh_assets=True`` (CLI: ``backfill --refresh-assets``).

    The symbols maintained here also get :func:`sync_raw` (from :data:`HISTORY_START` for a
    symbol with no raw bars yet), and its failures are merged into ``failed``.
    """
    if overlap_sessions < 1:
        raise ValueError("overlap_sessions must be at least 1")
    lookback = max(CALENDAR_LOOKBACK_DAYS, 2 * max(overlap_sessions, MAX_STALE_SESSIONS))
    cal = await _calendar(client, now, lookback)
    target = cal.last_completed_session(now)
    window_start = target
    for _ in range(overlap_sessions - 1):
        window_start = cal.previous_session(window_start)

    by_start: dict[date, list[str]] = defaultdict(list)
    for symbol, newest in sorted((await store.last_bar_dates()).items()):
        if len(cal.sessions_between(newest, target)) <= MAX_STALE_SESSIONS:
            by_start[min(window_start, newest)].append(symbol)
    maintained = sorted(symbol for group in by_start.values() for symbol in group)
    total = len(maintained)
    jobs = [(day, chunk) for day in sorted(by_start) for chunk in _chunks(by_start[day])]

    caught = await _catch_up(
        client, store, jobs, target=target, now=now, tolerance=adjust_tolerance
    )
    redo_rows, refetched, redo_failed = await _refetch_moved(
        client, store, caught.moved, start=HISTORY_START, target=target, now=now
    )
    raw = await sync_raw(
        client,
        store,
        symbols=maintained,
        status={},
        start=HISTORY_START,
        target=target,
        now=now,
    )

    failed = {*caught.failed, *redo_failed, *raw.failed}
    rows = caught.rows + redo_rows
    await store.refresh_asset_bar_ranges()
    log.info(
        "ingest.update_done",
        target=target.isoformat(),
        symbols=total,
        refetched=len(refetched),
        failed=len(failed),
        rows=rows,
        raw_rows=raw.raw_rows,
    )
    return IngestResult(total - len(failed), total, rows, refetched, sorted(failed), raw.raw_rows)
