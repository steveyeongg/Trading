"""Daily-bar ingest: a resumable full backfill and a split-aware incremental update.

Two jobs share one rule: a daily bar is only requested for sessions that have settled. The
target session is :meth:`TradingCalendar.last_completed_session` (close plus the 16 minute
SIP delay), and every request ends there, so a partial in-progress bar never lands.

**Backfill** walks the universe in chunks. Each chunk is fetched and then stored in one
transaction before the next starts, and a chunk that raises is logged and skipped. Stored
chunks stay stored, so a re-run resumes with whatever is still behind the target session.

**Incremental update** has to defeat one trap. Bars are requested with ``adjustment=all``,
so when a stock splits or pays a dividend Alpaca rewrites that stock's whole history. Append
only the new days to the old rows and two price scales meet at the join, which reads as a
crash (or a spike) that never happened. So the update refetches the last few sessions and
compares the earliest one that is also stored with its stored close. If they differ by more
than the tolerance the symbol's full history is refetched instead. Until that succeeds none
of the symbol's new bars are written, so a failed refetch leaves it whole on its old scale,
and it is retried on the next run.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import pandas as pd
from atlas_shared.logging import get_logger

from swing_engine.calendar import ET, Session, TradingCalendar
from swing_engine.data.assets import candidate_symbols, classify_asset

log = get_logger("swing.ingest")

# The oldest session the engine keeps, and where a split-triggered refetch starts.
HISTORY_START = date(2016, 1, 1)
# Symbols per fetch-and-store round. The Alpaca client holds a whole call in memory and does
# not retry transport errors, so a bounded chunk caps both the memory and the loss.
CHUNK_SIZE = 100
# The update only maintains symbols whose newest bar is at most this many sessions behind.
MAX_STALE_SESSIONS = 30
CALENDAR_LOOKBACK_DAYS = 10

_SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_API_SECRET")


class BarSource(Protocol):
    """The part of ``AlpacaData`` the ingest jobs use."""

    async def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, now: datetime | None = None
    ) -> pd.DataFrame: ...

    async def assets(self, status: Literal["active", "inactive"]) -> list[dict[str, Any]]: ...

    async def calendar(self, start: date, end: date) -> list[Session]: ...


class BarStore(Protocol):
    """The part of :mod:`swing_engine.store` the ingest jobs use (the module satisfies it)."""

    async def upsert_assets(self, rows: Sequence[dict[str, Any]]) -> int: ...

    async def load_assets(self) -> pd.DataFrame: ...

    async def upsert_daily_bars(self, df: pd.DataFrame) -> int: ...

    async def last_bar_dates(self) -> dict[str, date]: ...

    async def stored_close(self, symbol: str, day: date) -> float | None: ...

    async def refresh_asset_bar_ranges(self) -> None: ...


@dataclass(frozen=True)
class IngestResult:
    """What a run did.

    ``symbols_done`` is the symbols that ended up current (up to date already, or fetched
    cleanly), so ``symbols_total - symbols_done == len(failed)``. ``rows`` counts bars
    written. ``refetched`` lists symbols whose full history was replaced because their
    price adjustment moved; ``failed`` lists symbols a re-run still has to do.
    """

    symbols_done: int
    symbols_total: int
    rows: int
    refetched: list[str]
    failed: list[str] = field(default_factory=list)


def _describe(exc: BaseException) -> str:
    """An exception as one log-safe line: the type and message with credentials masked."""
    text = f"{type(exc).__name__}: {exc}"
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def _chunks(symbols: Sequence[str]) -> Iterator[list[str]]:
    for i in range(0, len(symbols), CHUNK_SIZE):
        yield list(symbols[i : i + CHUNK_SIZE])


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


async def _universe(client: BarSource, store: BarStore, symbols: Sequence[str] | None) -> list[str]:
    """The symbols to backfill: the caller's list, else the candidates from the asset table."""
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of tickers, not a single string")
    if symbols is not None:
        return list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    stored = await store.load_assets()
    if stored.empty:
        return candidate_symbols(await refresh_assets(client, store))
    return candidate_symbols(stored.to_dict("records"))


async def backfill(
    client: BarSource,
    store: BarStore,
    *,
    start: date,
    now: datetime,
    symbols: Sequence[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> IngestResult:
    """Load daily bars from ``start`` through the last completed session; safe to re-run.

    ``symbols`` defaults to every candidate in the stored asset table (listed common stock,
    active and delisted, plus the reference ETF), refreshing that table first when it is
    empty. A symbol whose newest stored bar is already at the target session is skipped; any
    other symbol is fetched whole from ``start``, because ``adjustment=all`` may have
    rewritten its older rows.

    ``progress(handled, total)`` is called once up front (with the symbols that needed no
    work) and after every chunk, ending at ``(total, total)`` even when some chunks failed.
    """
    cal = await _calendar(client, now, CALENDAR_LOOKBACK_DAYS)
    target = cal.last_completed_session(now)
    wanted = await _universe(client, store, symbols)
    last = await store.last_bar_dates()
    todo = [s for s in wanted if (newest := last.get(s)) is None or newest < target]
    total = len(wanted)
    handled = total - len(todo)
    if progress is not None:
        progress(handled, total)

    rows = 0
    failed: list[str] = []
    for chunk in _chunks(todo):
        try:
            bars = await client.daily_bars(chunk, start, target, now=now)
            if not bars.empty:
                rows += await store.upsert_daily_bars(bars)
        except Exception as exc:  # one bad chunk must not sink the run; a re-run resumes it
            failed.extend(chunk)
            log.warning(
                "ingest.backfill_chunk_failed",
                first=chunk[0],
                symbols=len(chunk),
                error=_describe(exc),
            )
        handled += len(chunk)
        if progress is not None:
            progress(handled, total)

    await store.refresh_asset_bar_ranges()
    log.info(
        "ingest.backfill_done",
        target=target.isoformat(),
        symbols=total,
        fetched=len(todo) - len(failed),
        failed=len(failed),
        rows=rows,
    )
    return IngestResult(total - len(failed), total, rows, [], sorted(failed))


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


async def incremental_update(
    client: BarSource,
    store: BarStore,
    *,
    now: datetime,
    overlap_sessions: int = 5,
    adjust_tolerance: float = 0.005,
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
    that. New listings are not discovered here; they enter through :func:`backfill`.
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
    total = sum(len(group) for group in by_start.values())

    rows = 0
    failed: list[str] = []
    moved: list[str] = []
    for fetch_from in sorted(by_start):
        for chunk in _chunks(by_start[fetch_from]):
            try:
                bars = await client.daily_bars(chunk, fetch_from, target, now=now)
                changed = await _moved_symbols(store, bars, adjust_tolerance)
                keep = bars[~bars["symbol"].isin(changed)]
                if not keep.empty:
                    rows += await store.upsert_daily_bars(keep)
            except Exception as exc:
                failed.extend(chunk)
                log.warning(
                    "ingest.update_chunk_failed",
                    first=chunk[0],
                    symbols=len(chunk),
                    error=_describe(exc),
                )
                continue
            moved.extend(changed)

    refetched: list[str] = []
    for chunk in _chunks(sorted(moved)):
        try:
            bars = await client.daily_bars(chunk, HISTORY_START, target, now=now)
            if not bars.empty:
                rows += await store.upsert_daily_bars(bars)
        except Exception as exc:
            # Nothing of these symbols was written, so they keep their old scale whole.
            failed.extend(chunk)
            log.warning(
                "ingest.refetch_failed",
                first=chunk[0],
                symbols=len(chunk),
                error=_describe(exc),
            )
            continue
        returned = set(bars["symbol"])
        refetched.extend(s for s in chunk if s in returned)
        empty = [s for s in chunk if s not in returned]
        if empty:
            failed.extend(empty)
            log.warning("ingest.refetch_empty", symbols=empty[:20], count=len(empty))

    await store.refresh_asset_bar_ranges()
    log.info(
        "ingest.update_done",
        target=target.isoformat(),
        symbols=total,
        refetched=len(refetched),
        failed=len(failed),
        rows=rows,
    )
    return IngestResult(total - len(failed), total, rows, refetched, sorted(failed))
