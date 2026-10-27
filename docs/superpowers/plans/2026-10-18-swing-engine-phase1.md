# Swing Engine Phase 1 (Data, Engine, Validation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A rules-based daily swing engine for US stocks.
- Ingests full-market (SIP) daily bars and builds a point-in-time universe.
- Classifies the market regime and finds breakout and pullback setups.
- Sizes them and simulates fills and exits.
- Runs in-sample validation that ends at a user checkpoint before the one-shot out-of-sample test.

**Architecture:** A new workspace package `apps/swing-engine` (`swing_engine`). It's built from pure functions over a wide "panel" (one DataFrame per OHLCV field, rows = session dates, columns = symbols), with a thin async DB store and an Alpaca client at the edges. Everything that decides a trade (universe, regime, leaders, setups, fills/exits, sizing, scan) is a pure function of the panel truncated at the scan date, which makes look-ahead testable. Validation reuses `backtest_service.metrics`.

**Tech Stack:** Python 3.12, pandas/numpy, httpx (+ `MockTransport` in tests), SQLAlchemy async + asyncpg (COPY), TimescaleDB, tenacity, pytest.

**Spec:** `docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md` (read §7.1–§7.8, §7.15, §7.16 before starting). Phase 2 lives in `docs/superpowers/plans/2026-10-18-swing-engine-phase2.md`.

## Global Constraints

- Work on branch `swing-rebuild` (`git switch -c swing-rebuild` before Task 1). `git add` only the files your task names. The working tree has unrelated user changes (`apps/alert-service/.../rules.py`, untracked `tests/test_*.py`, `AGENTS.md`); never stage them.
- End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Free data only: Alpaca `/v2/stocks/bars`, `timeframe=1Day`, `feed=sip`, `adjustment=all`, request `end` ≥ 16 minutes before now; free plan ≈ 200 requests/min.
- Long-only. No order placement anywhere. `ATLAS_ENABLE_AUTO_EXECUTION` stays off by default.
- Never label a number "confidence" or "probability" unless it is a measured historical frequency. No profit-promising language.
- The test suite must pass with **no Postgres/Redis running**. DB tests use `pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="needs local Postgres")` with `postgres_reachable` from `swing_engine.testing`.
- Tests live in `tests/test_swing_<area>.py` (flat, matching repo convention).
- Ruff: line length 100, py312. Run `uv run ruff check <your files>` before each commit.
- Daily bar `ts` convention: the session date at 00:00 UTC, `resolution='1d'`. Alpaca's `t` (midnight America/New_York) converts via `tz_convert("America/New_York").date()`.
- Indicators are computed per symbol, per contiguous segment. A gap of > 10 sessions starts a new segment (it handles ticker reuse and long halts). A missing day is never filled.
- Every documented `python -m X` needs `X/__main__.py` (guarded by `tests/test_cli_entrypoints.py`). Every `apps/<pkg>` needs a row in `docs/architecture/SYSTEM.md` (guarded by `tests/test_docs.py`).

## Review Focus

1. **A split or dividend after backfill.** `adjustment=all` rewrites history, so an incremental fetch that appends only new days would splice two price scales into a fake crash. Expected: incremental update notices that the overlap day moved and refetches that symbol's full history (test in Task 5).
2. **A stock that delists while a trade is open.** Bars just stop. Expected: the trade closes at the last close with reason `delisted`. It must not stay open forever or vanish from the stats (test in Task 8).
3. **A symbol missing the scan day** (halt, late print, newly listed). Expected: not eligible that day, with no stale-price idea from the prior day (test in Task 6).
4. **The same symbol reused by a new company** (> 10-session gap in its bars). Expected: indicators and history restart at the new segment, and the old company's SMA doesn't leak into the new one (test in Task 1).
5. **Gap-up open far above the trigger, or a gap-down through the stop.** Expected: no chase above trigger + 1 ATR, and stop exits fill at the worse open price, not the stop price (test in Task 8).

---

### Task 1: Package scaffold, settings defaults, panel, indicators

**Files:**
- Create: `apps/swing-engine/pyproject.toml` (deps: `atlas-shared`, `backtest-service`, `httpx>=0.27`, `pandas>=2.2`, `numpy>=1.26`, `sqlalchemy>=2.0`, `asyncpg>=0.29`, `tenacity>=8.0`; hatch wheel `src/swing_engine`, same layout as `apps/ingest-equities/pyproject.toml`)
- Create: `apps/swing-engine/src/swing_engine/__init__.py`, `config.py`, `panel.py`, `indicators.py`, `testing.py`
- Modify: root `pyproject.toml`: add `swing-engine` to `dependencies`, `[tool.uv.workspace].members` (`apps/swing-engine`) and `[tool.uv.sources]`
- Test: `tests/test_swing_indicators.py`

**Interfaces:**
- Produces, in `config.py` (frozen dataclasses with exactly these defaults):
  - `UniverseFilter(min_price=10.0, min_median_dollar_volume=20_000_000.0, dollar_volume_window=50, min_history_sessions=252, max_gap_sessions=10)`
  - `LeaderParams(rs_min_pct=70.0, within_high_pct=0.25, sma200_rising_lookback=20)`
  - `BreakoutParams(version="breakout-v1", near_high_pct=0.02, high_window=50, contraction_max=0.85, trigger_atr=0.10, stop_atr=2.0, trail_atr=3.0, max_hold=30)`
  - `PullbackParams(version="pullback-v1", rsi2_max=10.0, ema_window=20, trigger_atr=0.05, low_window=5, stop_buffer_atr=0.25, min_stop_atr=1.0, max_stop_atr=3.0, trail_atr=3.0, max_hold=30)`
  - `ExecutionParams(slippage_bps=10.0, max_gap_atr=1.0)`
  - `RiskSettings(equity=25_000.0, risk_pct=0.01, max_position_pct=0.20, max_positions=8, max_total_risk_pct=0.06, overlap_corr=0.80, overlap_window=60)`
  - `REGIME_FACTOR = {"uptrend": 1.0, "mixed": 0.5, "downtrend": 0.0}`, `TOP_N_DEFAULT = 20`, `SetupParams = BreakoutParams | PullbackParams`
- Produces, in `panel.py`:
  - `@dataclass(frozen=True) class Panel: open, high, low, close, volume: pd.DataFrame`. Index is a tz-naive `DatetimeIndex` of session dates; columns are symbols.
  - Methods: `.symbols() -> list[str]`, `.dates() -> pd.DatetimeIndex`.
  - `build_panel(bars: pd.DataFrame) -> Panel` (long frame with `ts, symbol, open, high, low, close, volume`).
  - `truncate(panel: Panel, as_of: pd.Timestamp) -> Panel`, `select(panel: Panel, symbols: Sequence[str]) -> Panel`.
  - `segments(series: pd.Series, max_gap: int) -> list[pd.Series]`: splits the non-NaN values where the gap between consecutive bars exceeds `max_gap` panel sessions.
- Produces, in `indicators.py`:
  - `sma(x: pd.DataFrame, n: int)`, `ema(x, n)`, `wilder(x, n)`, `true_range(p: Panel)`, `atr(p: Panel, n: int)`, `rsi(close: pd.DataFrame, n: int)`, `rolling_max(x, n)`, `rolling_min(x, n)`. All return `pd.DataFrame` with strict `min_periods=n`, computed per symbol per segment via `segments(..., max_gap=10)`.
  - `@dataclass(frozen=True) class Indicators: sma50, sma200, ema20, atr10, atr14, atr50, rsi2, high_max50, close_max50, close_max252, low_min5: pd.DataFrame`
  - `compute_indicators(p: Panel) -> Indicators`
- Produces, in `testing.py`:
  - `make_bars(symbol: str, closes: Sequence[float], start: str = "2020-01-02", range_pct: float = 0.01, volume: float = 2_000_000) -> pd.DataFrame` (long; business days; `open` = prior close; high/low = max/min(open, close) ± `range_pct`).
  - `make_panel(series: dict[str, Sequence[float]], **kw) -> Panel`.
  - `trend_series(n: int, start: float = 100.0, drift: float = 0.001, vol: float = 0.01, seed: int = 0) -> list[float]`.
  - `postgres_reachable() -> bool` (TCP 127.0.0.1:5432, 0.25s timeout).

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_match_spec():
    assert UniverseFilter().min_median_dollar_volume == 20_000_000.0
    assert BreakoutParams().stop_atr == 2.0 and BreakoutParams().trail_atr == 3.0
    assert PullbackParams().rsi2_max == 10.0 and PullbackParams().max_hold == 30
    assert RiskSettings().risk_pct == 0.01 and RiskSettings().max_positions == 8
    assert REGIME_FACTOR == {"uptrend": 1.0, "mixed": 0.5, "downtrend": 0.0}

def test_sma_is_strict_about_warmup():
    p = make_panel({"AAA": list(range(1, 61))})
    s = sma(p.close, 50)["AAA"]
    assert s.iloc[:49].isna().all() and s.iloc[49] == pytest.approx(25.5)

def test_atr_and_rsi_match_pandas_ta_after_warmup():
    p = make_panel({"AAA": trend_series(400, seed=3)})
    ours = atr(p, 14)["AAA"].iloc[-100:]
    ref = ta.atr(p.high["AAA"], p.low["AAA"], p.close["AAA"], length=14).iloc[-100:]
    assert np.allclose(ours, ref, rtol=1e-4)
    assert np.allclose(rsi(p.close, 14)["AAA"].iloc[-100:], ta.rsi(p.close["AAA"], 14).iloc[-100:], rtol=1e-4)

def test_gap_over_ten_sessions_restarts_indicators():
    a = make_bars("AAA", [100.0] * 60)                  # old company
    b = make_bars("AAA", [10.0] * 60, start="2020-06-01")  # reused ticker, > 10-session gap
    spy = make_bars("SPY", trend_series(200))
    p = build_panel(pd.concat([a, b, spy]))
    s = sma(p.close, 50)["AAA"]
    first_new = b["ts"].iloc[0]
    assert s.loc[first_new:].dropna().iloc[0] == pytest.approx(10.0)  # no blend of 100s and 10s
    assert s.loc[first_new:].iloc[:49].isna().all()

def test_truncate_drops_future_rows():
    p = make_panel({"AAA": trend_series(30)})
    t = truncate(p, p.dates()[9])
    assert len(t.close) == 10 and t.close.index[-1] == p.dates()[9]
```

- [ ] **Step 2:** Run `uv sync && uv run pytest tests/test_swing_indicators.py -q`. Expected: FAIL (`ModuleNotFoundError: swing_engine`).
- [ ] **Step 3: Implement** `config.py`, `panel.py`, `indicators.py`, `testing.py` per the Interfaces. `ema` = `ewm(span=n, adjust=False, min_periods=n)`, `wilder` = `ewm(alpha=1/n, adjust=False, min_periods=n)`, RSI and ATR use `wilder`. Apply each indicator per column over `segments(col, 10)`, then reindex onto the panel index.
- [ ] **Step 4:** Run `uv run pytest tests/test_swing_indicators.py -q`. Expected: PASS. Then run `uv run pytest -q`. Expected: no new failures.
- [ ] **Step 5: Commit** with message `feat(swing): package scaffold, panel and strict daily indicators`.

---

### Task 2: Alpaca daily-data client and trading calendar

**Files:**
- Create: `apps/swing-engine/src/swing_engine/data/__init__.py`, `data/alpaca.py`, `calendar.py`
- Test: `tests/test_swing_alpaca.py`, `tests/test_swing_calendar.py`

**Interfaces:**
- Produces, in `calendar.py`:
  - `ET = ZoneInfo("America/New_York")`
  - `@dataclass(frozen=True) class Session: day: date; open: time; close: time`
  - `class TradingCalendar(sessions: Sequence[Session])` with methods:
    - `is_session(d) -> bool`
    - `next_session(d) -> date`
    - `previous_session(d) -> date`
    - `last_completed_session(now: datetime, settle_minutes: int = 16) -> date`: the latest session whose close (ET) plus `settle_minutes` is ≤ `now`.
    - `sessions_between(after: date, through: date) -> list[date]`: `after` exclusive, `through` inclusive.
    - `scan_time(d: date, delay_minutes: int = 45) -> datetime`: tz-aware ET; session close + 45 min.
- Produces, in `data/alpaca.py`:
  - Constructor: `class AlpacaData(key: str | None = None, secret: str | None = None, data_url: str | None = None, trading_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None, min_interval_s: float = 0.3)`. Defaults come from `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_DATA_URL` (default `https://data.alpaca.markets`) and `ALPACA_BASE_URL` (default `https://paper-api.alpaca.markets`, strip trailing `/v2`).
  - `async daily_bars(symbols: Sequence[str], start: date, end: date, now: datetime | None = None) -> pd.DataFrame`. Columns are `ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count`; `resolution="1d"`; `ts` follows the Global Constraints convention.
  - `async assets(status: Literal["active", "inactive"]) -> list[dict]`
  - `async calendar(start: date, end: date) -> list[Session]`
  - `async aclose()`, plus async context manager support.

- [ ] **Step 1: Write the failing tests** (an httpx `MockTransport` handler captures requests and serves canned JSON):

```python
async def test_daily_bars_uses_sip_all_adjustment_and_batches_of_100():
    syms = [f"S{i}" for i in range(150)]
    ...  # handler returns {"bars": {}, "next_page_token": None}
    await c.daily_bars(syms, date(2024, 1, 1), date(2024, 1, 31), now=NOW)
    qs = [parse_qs(urlparse(str(r.url)).query) for r in captured]
    assert len(captured) == 2 and qs[0]["feed"] == ["sip"] and qs[0]["adjustment"] == ["all"]
    assert qs[0]["timeframe"] == ["1Day"] and len(qs[0]["symbols"][0].split(",")) == 100

async def test_end_is_clamped_to_16_minutes_before_now():
    await c.daily_bars(["AAPL"], date(2026, 10, 16), date(2026, 10, 16), now=datetime(2026, 10, 16, 20, 10, tzinfo=UTC))
    assert qs["end"] == ["2026-10-16T19:54:00+00:00"]

async def test_pagination_follows_next_page_token_and_ts_is_session_date():
    ...  # page 1 returns token "p2" with bar t="2026-10-15T04:00:00Z"; page 2 returns t="2026-10-16T04:00:00Z"
    df = await c.daily_bars(["AAPL"], ...)
    assert list(df["ts"]) == [pd.Timestamp("2026-10-15", tz="UTC"), pd.Timestamp("2026-10-16", tz="UTC")]
    assert (df["resolution"] == "1d").all()

async def test_429_is_retried():
    ...  # first response 429, second 200
    assert len(captured) == 2

def test_last_completed_session_skips_weekend_and_waits_16_minutes():
    cal = TradingCalendar([Session(date(2026, 10, 15), time(9, 30), time(16)), Session(date(2026, 10, 16), time(9, 30), time(16)), Session(date(2026, 10, 19), time(9, 30), time(16))])
    assert cal.last_completed_session(datetime(2026, 10, 18, 12, tzinfo=ET)) == date(2026, 10, 16)
    assert cal.last_completed_session(datetime(2026, 10, 16, 16, 10, tzinfo=ET)) == date(2026, 10, 15)

def test_scan_time_on_half_day_is_close_plus_45():
    cal = TradingCalendar([Session(date(2026, 11, 27), time(9, 30), time(13))])
    assert cal.scan_time(date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 45, tzinfo=ET)
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_alpaca.py tests/test_swing_calendar.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement.**
  - Requests to `/v2/stocks/bars` use `symbols` (comma-joined, ≤ 100), `limit=10000` and `sort=asc`.
  - Retry 429 and 5xx with tenacity: exponential backoff, 6 attempts.
  - Throttle so requests start ≥ `min_interval_s` apart.
  - `calendar()` maps Alpaca `{"date","open","close"}` to `Session`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): Alpaca SIP daily client and trading calendar`.

---

### Task 3: Asset classification

**Files:**
- Create: `apps/swing-engine/src/swing_engine/data/assets.py`
- Test: `tests/test_swing_assets.py`

**Interfaces:**
- Produces:
  - `class AssetKind(StrEnum)` with members `COMMON="common"`, `ETF="etf"`, `FUND="fund"`, `WARRANT="warrant"`, `UNIT="unit"`, `RIGHT="right"`, `PREFERRED="preferred"`, `SPAC="spac"`, `OTC="otc"`, `OTHER="other"`
  - `classify_asset(asset: dict) -> AssetKind` (uses `symbol`, `name`, `exchange`, `class`)
  - `TRADEABLE_KINDS = frozenset({AssetKind.COMMON})`
  - `REFERENCE_SYMBOLS = ("SPY",)`
  - `candidate_symbols(assets: Iterable[dict]) -> list[str]`: COMMON on NYSE/NASDAQ/AMEX/ARCA/BATS, plus `REFERENCE_SYMBOLS`; sorted and unique.

- [ ] **Step 1: Write the failing test.** A parametrized table:

```python
@pytest.mark.parametrize("sym,name,exch,kind", [
    ("AAPL", "Apple Inc. Common Stock", "NASDAQ", "common"),
    ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.", "NYSE", "common"),
    ("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE", "common"),
    ("BABA", "Alibaba Group Holding Limited American Depositary Shares", "NYSE", "common"),
    ("SPY", "SPDR S&P 500 ETF Trust", "ARCA", "etf"),
    ("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ", "etf"),
    ("PDI", "PIMCO Dynamic Income Fund", "NYSE", "fund"),
    ("ABCDW", "Abcd Corp - Warrant", "NASDAQ", "warrant"),
    ("XYZU", "XYZ Acquisition Corp - Units", "NASDAQ", "unit"),
    ("XYZ", "XYZ Acquisition Corp - Class A Ordinary Shares", "NASDAQ", "spac"),
    ("XYZR", "XYZ Acquisition Corp - Rights", "NASDAQ", "right"),
    ("JPM.PRC", "JPMorgan Chase & Co. Depositary Shares Series C Preferred", "NYSE", "preferred"),
    ("GBTC", "Grayscale Bitcoin Trust (BTC)", "ARCA", "etf"),
    ("OTCX", "Some Co", "OTC", "otc"),
])
def test_classify(sym, name, exch, kind):
    assert classify_asset({"symbol": sym, "name": name, "exchange": exch, "class": "us_equity"}) == kind

def test_candidate_symbols_includes_spy_reference_once():
    assert candidate_symbols([{"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "class": "us_equity"}]) == ["SPY"]
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_assets.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with ordered rules:
  1. OTC exchange.
  2. Name keywords for ETF/ETN/fund issuers and "Trust" products.
  3. "Acquisition Corp/Co" → SPAC, but units, rights and warrants take precedence by name.
  4. Name contains warrant/unit/right.
  5. Preferred or depositary-preferred.
  6. Dot-suffix rules: `.WS`/`.W` → warrant, `.U`/`.UN` → unit, `.R`/`.RT` → right, `.PR*` → preferred. Other dot classes (`BRK.B`) are common.
  7. Default COMMON.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): asset classification heuristics`.

---

### Task 4: Migration 0009 and swing data store

**Files:**
- Create: `packages/shared-py/migrations/0009_swing_assets.sql`
- Create: `apps/swing-engine/src/swing_engine/store.py`
- Test: `tests/test_swing_store.py`

**Interfaces:**
- Migration:
  - `assets(symbol TEXT PRIMARY KEY, name TEXT, exchange TEXT, status TEXT NOT NULL, kind TEXT NOT NULL, tradable BOOLEAN, first_bar DATE, last_bar DATE, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`
  - `CREATE INDEX IF NOT EXISTS bars_resolution_ts_idx ON bars (resolution, ts);`
  - Use `DOUBLE PRECISION`/`DATE` only, no `NUMERIC`.
- Produces (all `async`):
  - `upsert_assets(rows: Sequence[dict]) -> int`: rows carry `symbol, name, exchange, status, kind, tradable`.
  - `load_assets() -> pd.DataFrame`
  - `refresh_asset_bar_ranges() -> None`: sets `first_bar`/`last_bar` from `bars` where `resolution='1d'`.
  - `upsert_daily_bars(df: pd.DataFrame) -> int`
  - `last_bar_dates() -> dict[str, date]`
  - `stored_close(symbol: str, day: date) -> float | None`
  - `load_daily_bars(symbols: Sequence[str] | None, start: date, end: date) -> pd.DataFrame`
  - `liquid_prefilter(min_dollar_volume: float) -> list[str]`: symbols with `max(close*volume) >= X`. This is a necessary condition for a 50-day median ≥ X.
- Module constants `_UPSERT_ASSETS_SQL`, `_MERGE_BARS_SQL` (for contract tests).

- [ ] **Step 1: Write the failing tests:**

```python
def test_merge_sql_upserts_on_natural_key():
    assert "ON CONFLICT (symbol, resolution, ts) DO UPDATE" in _MERGE_BARS_SQL.text

def test_migration_0009_has_no_numeric_columns():
    sql = (MIGRATIONS / "0009_swing_assets.sql").read_text()
    assert "NUMERIC" not in sql.upper() and "CREATE TABLE IF NOT EXISTS assets" in sql

pytestmark_db = pytest.mark.skipif(not postgres_reachable(), reason="needs local Postgres")

@pytestmark_db
async def test_daily_bars_round_trip_and_idempotent():
    df = make_bars("ZZTEST", [10, 11, 12]).assign(resolution="1d")
    assert await upsert_daily_bars(df) == 3 and await upsert_daily_bars(df) == 3
    out = await load_daily_bars(["ZZTEST"], date(2020, 1, 1), date(2020, 2, 1))
    assert list(out["close"]) == [10, 11, 12]
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_store.py -q`. Expected: FAIL (import error; DB test skipped if no Postgres).
- [ ] **Step 3: Implement.** `upsert_daily_bars` batches 50,000 rows. It gets the asyncpg connection via `(await session.connection()).get_raw_connection()` → `.driver_connection`, runs `CREATE TEMP TABLE _bars_in (LIKE bars INCLUDING DEFAULTS) ON COMMIT DROP` and `copy_records_to_table("_bars_in", ...)`, then executes `_MERGE_BARS_SQL` (`INSERT INTO bars SELECT ... FROM _bars_in ON CONFLICT (symbol, resolution, ts) DO UPDATE ...`).
- [ ] **Step 4:** Run the same command. Expected: PASS. Then apply the migration if Postgres is up: `uv run python -m atlas_shared.migrate up`.
- [ ] **Step 5: Commit** with message `feat(swing): assets table and bulk daily-bar store`.

---

### Task 5: Backfill and incremental update (resumable), CLI entry point

**Files:**
- Create: `apps/swing-engine/src/swing_engine/ingest.py`, `apps/swing-engine/src/swing_engine/__main__.py`
- Test: `tests/test_swing_ingest.py`

**Interfaces:**
- Consumes: `AlpacaData` (Task 2), `classify_asset`/`candidate_symbols` (Task 3), the store functions (Task 4).
- Produces:
  - `class BarStore(Protocol)`: the subset of the Task 4 store used here (`upsert_assets`, `load_assets`, `upsert_daily_bars`, `last_bar_dates`, `stored_close`, `refresh_asset_bar_ranges`), so tests inject an in-memory fake. `swing_engine.store` satisfies it as a module.
  - `@dataclass(frozen=True) class IngestResult: symbols_done: int; symbols_total: int; rows: int; refetched: list[str]`
  - `async refresh_assets(client, store) -> list[dict]`: active + inactive, adds `kind`, upserts, returns rows.
  - `async backfill(client, store, *, start: date, now: datetime, symbols: Sequence[str] | None = None, progress: Callable[[int, int], None] | None = None) -> IngestResult`: resumable; skips symbols whose stored last bar ≥ the target end session.
  - `async incremental_update(client, store, *, now: datetime, overlap_sessions: int = 5, adjust_tolerance: float = 0.005) -> IngestResult`
  - CLI `python -m swing_engine <cmd>` with subcommands `backfill [--start 2016-01-01] [--symbols A,B]`, `update`, `scan [--as-of YYYY-MM-DD]` (Task 10), `insample --family {breakout,pullback}` and `oos --family F --version V` (Task 13). Each calls `atlas_shared.load_env()` and `setup_logging()` first.

- [ ] **Step 1: Write the failing tests** (fake client + in-memory fake store):

```python
async def test_backfill_resumes_from_stored_last_bar():
    store = FakeStore(last={"AAA": date(2026, 10, 16)}, assets=[common("AAA"), common("BBB")])
    res = await backfill(FakeClient(...), store, start=date(2016, 1, 1), now=FRI_EVENING)
    assert client.requested_symbols == ["BBB", "SPY"] and res.symbols_total == 3

async def test_incremental_refetches_full_history_after_split():
    store = FakeStore(closes={("AAA", date(2026, 10, 15)): 400.0})   # pre-split stored scale
    client = FakeClient(bars={"AAA": [("2026-10-15", 100.0), ("2026-10-16", 101.0)]})  # adjusted 4:1
    res = await incremental_update(client, store, now=FRI_EVENING)
    assert res.refetched == ["AAA"] and client.full_history_requests == ["AAA"]

async def test_incremental_does_not_refetch_when_overlap_matches():
    ...  # stored 100.2 vs fetched 100.0 (0.2% < 0.5%)
    assert res.refetched == []
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_ingest.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The target end session comes from `last_completed_session`, using a calendar fetched for the last 10 days.
  - Incremental: fetch the last `overlap_sessions` for every symbol whose `last_bar` is within 30 sessions. Compare the earliest overlap close with `stored_close`. If `abs(Δ)/stored > adjust_tolerance`, refetch from 2016-01-01.
  - Call `refresh_asset_bar_ranges()` at the end of both functions.
- [ ] **Step 4:** Run the same command. Expected: PASS. Also run `uv run python -m swing_engine --help`. Expected: lists the subcommands.
- [ ] **Step 5: Commit** with message `feat(swing): resumable SIP backfill with split-aware incremental update`.

---

### Task 6: Point-in-time universe, market regime, breadth

**Files:**
- Create: `apps/swing-engine/src/swing_engine/universe.py`, `regime.py`
- Test: `tests/test_swing_universe_regime.py`

**Interfaces:**
- Consumes: `Panel`, `Indicators`, `UniverseFilter`, `AssetKind`.
- Produces:
  - `eligibility(panel: Panel, kinds: Mapping[str, AssetKind], f: UniverseFilter) -> pd.DataFrame` (bool). True at (d, s) iff all of:
    - a bar exists at d
    - close ≥ `min_price`
    - 50-session rolling median of close×volume (strict) ≥ `min_median_dollar_volume`
    - the current segment has ≥ `min_history_sessions` bars
    - kind ∈ `TRADEABLE_KINDS`
  - `class Regime(StrEnum)` with members `UPTREND="uptrend"`, `MIXED="mixed"`, `DOWNTREND="downtrend"`
  - `market_regime(spy_close: pd.Series) -> pd.Series`:
    - UPTREND if close > SMA200 and SMA50 > SMA200
    - DOWNTREND if close < SMA200 and SMA50 < SMA200
    - else MIXED (MIXED while warming up)
  - `breadth(close: pd.DataFrame, sma50: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series`: % of eligible symbols with close > SMA50, from 0 to 100.

- [ ] **Step 1: Write the failing tests:**

```python
def test_missing_scan_day_is_not_eligible():
    p = make_panel({"AAA": [50.0] * 300, "SPY": trend_series(300)}, volume=1_000_000)
    p.close.loc[p.dates()[-1], "AAA"] = np.nan
    e = eligibility(p, {"AAA": AssetKind.COMMON}, UniverseFilter())
    assert not e["AAA"].iloc[-1] and e["AAA"].iloc[-2]

def test_dollar_volume_and_price_floor():
    p = make_panel({"CHEAP": [9.99] * 300, "THIN": [50.0] * 300}, volume=300_000)   # THIN: $15M/day
    e = eligibility(p, {"CHEAP": AssetKind.COMMON, "THIN": AssetKind.COMMON}, UniverseFilter())
    assert not e.iloc[-1].any()

def test_etf_and_reference_never_eligible():
    ...  # SPY kind ETF -> False

def test_regime_rules():
    up = pd.Series(np.linspace(100, 200, 300)); down = up[::-1].reset_index(drop=True)
    assert market_regime(up).iloc[-1] == Regime.UPTREND and market_regime(down).iloc[-1] == Regime.DOWNTREND
    assert market_regime(up).iloc[10] == Regime.MIXED
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_universe_regime.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with vectorized frames. The history count is per segment (reuse `segments`).
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): point-in-time universe and market regime`.

---

### Task 7: Relative strength, leader filter, setup candidates

**Files:**
- Create: `apps/swing-engine/src/swing_engine/leaders.py`, `setups.py`
- Test: `tests/test_swing_setups.py`

**Interfaces:**
- Produces:
  - `rs_score(close: pd.DataFrame) -> pd.DataFrame` = `0.4*r63 + 0.2*r126 + 0.2*r189 + 0.2*r252`, where `rN = close / close.shift(N) - 1` (per segment; NaN until 252 bars).
  - `rs_percentile(score, eligible) -> pd.DataFrame`: per-date percentile rank (0–100, `rank(pct=True)*100`) among eligible symbols; NaN elsewhere.
  - `leader_mask(close, ind: Indicators, rs_pct, eligible, p: LeaderParams) -> pd.DataFrame`. True iff all of:
    - eligible
    - close > sma50 > sma200
    - sma200 > sma200.shift(20)
    - rs_pct ≥ 70
    - close ≥ (1 − 0.25) × close_max252
  - `CANDIDATE_COLUMNS = ["date", "symbol", "setup", "version", "trigger", "stop", "atr14", "rs_pct"]`
  - `breakout_candidates(panel, ind, leaders, rs_pct, p: BreakoutParams) -> pd.DataFrame`:
    - Condition: leader, close ≥ (1 − near_high_pct) × close_max50, and atr10 / atr50 ≤ contraction_max.
    - Trigger = high_max50 + trigger_atr × atr14.
    - Stop = trigger − stop_atr × atr14.
    - `setup="breakout"`.
  - `pullback_candidates(panel, ind, leaders, rs_pct, p: PullbackParams) -> pd.DataFrame`:
    - Condition: leader, and (rsi2 ≤ rsi2_max or close ≤ ema20).
    - Trigger = high + trigger_atr × atr14.
    - Raw stop = low_min5 − stop_buffer_atr × atr14.
    - Stop = trigger − clip(trigger − raw, min_stop_atr × atr14, max_stop_atr × atr14).
    - `setup="pullback"`.
  - `candidates_for(panel, ind, leaders, rs_pct, params: SetupParams) -> pd.DataFrame`: dispatch on the params type.

- [ ] **Step 1: Write the failing tests** (build panels whose last row satisfies or breaks one condition at a time, using `testing.make_panel`, then set specific cells):

```python
def test_breakout_trigger_and_stop_values():
    c = breakout_candidates(...).iloc[-1]
    assert c.trigger == pytest.approx(high_max50 + 0.1 * atr14)
    assert c.trigger - c.stop == pytest.approx(2.0 * atr14)

def test_breakout_requires_contraction():
    ...  # atr10/atr50 = 0.9 → no row on last date

def test_pullback_stop_clamped_between_1_and_3_atr():
    ...  # low_min5 far below → distance == 3*atr14; low_min5 just under trigger → distance == 1*atr14

def test_non_leader_never_a_candidate():
    ...  # rs_pct = 60 on last date → no rows for either setup

def test_rs_percentile_ignores_ineligible():
    ...  # ineligible symbol has NaN percentile and does not shift others' ranks
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_setups.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with vectorized masks, then `stack()` into long candidate frames sorted by `date, symbol`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): relative strength, leader filter, breakout and pullback setups`.

---

### Task 8: Fill and exit model

**Files:**
- Create: `apps/swing-engine/src/swing_engine/execution.py`
- Test: `tests/test_swing_execution.py`

**Interfaces:**
- Produces:
  - `class TrackStatus(StrEnum)` with members `PENDING`, `NOT_TRIGGERED`, `SKIPPED_GAP`, `OPEN`, `CLOSED` (lower-case values).
  - `class ExitReason(StrEnum)` with members `STOP="stop"`, `TRAIL="trail"`, `MAX_HOLD="max_hold"`, `DELISTED="delisted"`.
  - `@dataclass(frozen=True) class TradeOutcome` with fields:
    - `status: TrackStatus`
    - `entry_date: date | None`, `entry_price: float | None`, `initial_stop: float`
    - `exit_date: date | None`, `exit_price: float | None`, `exit_reason: ExitReason | None`
    - `r_multiple: float | None`
    - `sessions_held: int`, `current_stop: float | None`, `highest_close: float | None`
  - `simulate_trade(bars: pd.DataFrame, signal_date: date, trigger: float, stop: float, trail_atr: float, max_hold: int, ex: ExecutionParams, delisted: bool = False) -> TradeOutcome`, where `bars` is one symbol's rows indexed by session date with `open, high, low, close, atr14` up to the evaluation date.

- [ ] **Step 1: Write the failing tests** (hand-built bar frames, `ExecutionParams()` defaults):

```python
def test_no_next_bar_is_pending(): ...            # status PENDING
def test_high_below_trigger_not_triggered(): ...  # status NOT_TRIGGERED
def test_gap_above_trigger_plus_one_atr_is_skipped():
    out = simulate_trade(bars(day1_open=trigger + 1.01 * atr), ...)
    assert out.status == TrackStatus.SKIPPED_GAP
def test_fill_at_worse_of_open_and_trigger_plus_slippage():
    assert out.entry_price == pytest.approx(max(open1, trigger) * 1.001)
def test_entry_day_low_through_stop_exits_same_day_at_stop():
    assert out.exit_date == day1 and out.exit_price == pytest.approx(stop * 0.999) and out.exit_reason == ExitReason.STOP
def test_gap_down_through_stop_fills_at_open():
    assert out.exit_price == pytest.approx(open_k * 0.999)
def test_trail_ratchets_from_highest_close_and_never_drops():
    assert out.current_stop == pytest.approx(max(stop, highest_close - 3 * atr_k))
def test_trailed_exit_reason_is_trail(): ...
def test_max_hold_exits_at_close_of_session_30():
    assert out.sessions_held == 30 and out.exit_reason == ExitReason.MAX_HOLD
def test_delisted_closes_at_last_close():
    out = simulate_trade(bars_ending_early, ..., delisted=True)
    assert out.status == TrackStatus.CLOSED and out.exit_reason == ExitReason.DELISTED
def test_r_multiple_uses_fill_price():
    assert out.r_multiple == pytest.approx((out.exit_price - out.entry_price) / (out.entry_price - stop))
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_execution.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** exactly this sequence (`slip = ex.slippage_bps / 10_000`):

```
d1 = first session in bars after signal_date            → none: PENDING
if open[d1] > trigger + ex.max_gap_atr * atr14[signal_date]: SKIPPED_GAP
if high[d1] < trigger: NOT_TRIGGERED
entry = max(open[d1], trigger) * (1 + slip); held = 1
if low[d1] <= stop: exit stop*(1-slip) on d1, STOP
hc = close[d1]; cur = max(stop, hc - trail_atr * atr14[d1])
if held == max_hold: exit close[d1]*(1-slip), MAX_HOLD
for d in later sessions:
    held += 1
    if low[d] <= cur: exit min(open[d], cur)*(1-slip), STOP if cur == stop else TRAIL
    hc = max(hc, close[d]); cur = max(cur, hc - trail_atr * atr14[d])
    if held == max_hold: exit close[d]*(1-slip), MAX_HOLD
end of bars: delisted → exit last close*(1-slip), DELISTED; else OPEN (current_stop=cur)
r = (exit - entry) / (entry - stop)
```

- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): next-session buy-stop fill and chandelier exit model`.

---

### Task 9: Position sizing and overlap flags

**Files:**
- Create: `apps/swing-engine/src/swing_engine/sizing.py`
- Test: `tests/test_swing_sizing.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Sizing: shares: int; risk_dollars: float; position_value: float; capped_by: str | None` (`"max_position"`, `"no_risk"` or `None`)
  - `size_idea(trigger: float, stop: float, settings: RiskSettings, regime_factor: float) -> Sizing`
  - `@dataclass(frozen=True) class Capacity: open_positions: int; max_positions: int; open_risk_dollars: float; max_risk_dollars: float`
  - `overlap_flags(close: pd.DataFrame, as_of: pd.Timestamp, idea_symbols: Sequence[str], open_symbols: Sequence[str], window: int = 60, threshold: float = 0.8) -> dict[str, list[str]]` (daily-return correlation over the last `window` sessions ending at `as_of`).

- [ ] **Step 1: Write the failing tests:**

```python
def test_shares_from_one_percent_risk():
    s = size_idea(148.20, 140.10, RiskSettings(), 1.0)
    assert s.shares == 30 and s.risk_dollars == pytest.approx(30 * 8.10)   # floor(250 / 8.10)
def test_mixed_regime_halves_risk():
    assert size_idea(148.20, 140.10, RiskSettings(), 0.5).shares == 15
def test_position_value_cap_20_percent():
    s = size_idea(100.0, 99.0, RiskSettings(), 1.0)          # 250 shares by risk → capped to 50
    assert s.shares == 50 and s.capped_by == "max_position"
def test_downtrend_or_bad_stop_gives_zero_shares():
    assert size_idea(100, 90, RiskSettings(), 0.0).shares == 0
    assert size_idea(100, 100, RiskSettings(), 1.0).capped_by == "no_risk"
def test_overlap_flag_above_0_8_correlation(): ...   # idea with returns identical to open MSFT → {"IDEA": ["MSFT"]}; independent → {"IDEA": []}
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_sizing.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per the spec in §7.7.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): fixed-fractional sizing with caps and overlap flags`.

---

### Task 10: The scan (pure) and `python -m swing_engine scan`

**Files:**
- Create: `apps/swing-engine/src/swing_engine/scan.py`
- Modify: `apps/swing-engine/src/swing_engine/__main__.py` (wire `scan`)
- Test: `tests/test_swing_scan.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 6, 7, 9.
- Produces:
  - `@dataclass(frozen=True) class SetupSpec: params: SetupParams; live: bool; regime_gate: bool = True`
  - `@dataclass(frozen=True) class Idea` with fields `symbol: str`, `setup: str`, `version: str`, `trigger: float`, `stop: float`, `atr14: float`, `rs_pct: float`, `rank: int`, `sizing: Sizing`, `overlaps: list[str]`, `live: bool`, `regime_warning: bool`
  - Regime gating (spec §7.3): a spec with `regime_gate=False` (the filter didn't help out of sample) keeps its ideas in `ideas` in every regime, sized at factor 1.0, with `regime_warning=True` whenever the regime isn't UPTREND.
  - Ranking (spec §7.6): `run_scan(..., rank_by: Literal["rs", "proximity"] = "rs")`. `"rs"` sorts by `rs_pct` desc then symbol. `"proximity"` sorts by `close / trigger` desc (nearest to filling first) then symbol.
  - `@dataclass(frozen=True) class ScanResult` with fields `session: date`, `regime: Regime`, `regime_factor: float`, `breadth: float`, `universe_size: int`, `ideas: list[Idea]`, `watch_only: list[Idea]`, `capacity: Capacity`
  - `class ScanError(RuntimeError)`
  - `run_scan(panel: Panel, kinds: Mapping[str, AssetKind], as_of: date, settings: RiskSettings, setups: Sequence[SetupSpec], open_positions: Sequence[tuple[str, float]] = (), universe: UniverseFilter = UniverseFilter()) -> ScanResult`. `open_positions` holds (symbol, open risk dollars). `universe` lets Phase 2 apply the user's Settings filters.
  - `@dataclass(frozen=True) class ScanContext` with fields `session: pd.Timestamp`, `panel: Panel`, `ind: Indicators`, `eligible`, `rs_pct`, `leaders: pd.DataFrame`, `regime: pd.Series`, `kinds: Mapping[str, AssetKind]`
  - `build_context(panel, kinds, as_of: date, universe: UniverseFilter = UniverseFilter()) -> ScanContext`: truncates to `as_of`, then computes indicators, eligibility, RS, leaders and regime. `run_scan` is implemented on top of it, and Phase 2's stock page reuses it.
  - `default_setups(repo: Path | None = None) -> list[SetupSpec]`: both families with `live=False` and default params until Task 13 rewires it to `load_verdicts`/`params_for`.
  - `scan_to_dict(r: ScanResult) -> dict` (JSON-safe; used by Phase 2).

- [ ] **Step 1: Write the failing tests:**

```python
def test_scan_is_lookahead_free():
    full = synthetic_universe_panel(seed=1)             # helper: 40 symbols + SPY, 600 sessions
    d = full.dates()[450]
    a = run_scan(full, KINDS, d.date(), RiskSettings(), default_setups())
    future_scrambled = scramble_after(full, d, seed=9)  # randomise every row after d
    b = run_scan(future_scrambled, KINDS, d.date(), RiskSettings(), default_setups())
    assert scan_to_dict(a) == scan_to_dict(b)

def test_downtrend_moves_ideas_to_watch_only_with_zero_shares(): ...
def test_ungated_setup_keeps_ideas_in_downtrend_with_regime_warning(): ...   # SetupSpec(regime_gate=False) → in ideas, factor 1.0, regime_warning True
def test_ideas_ranked_by_rs_desc_then_symbol(): ...
def test_proximity_ranking_orders_by_close_over_trigger(): ...
def test_missing_spy_bar_raises_scan_error(): ...
def test_capacity_reflects_open_positions():
    r = run_scan(..., open_positions=[("MSFT", 240.0)])
    assert r.capacity.open_positions == 1 and r.capacity.max_risk_dollars == pytest.approx(1500.0)
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_scan.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** `run_scan` truncates the panel to `as_of` before computing anything, which is the look-ahead guarantee. The `scan` CLI loads the last 520 sessions for `liquid_prefilter(20e6) ∪ {"SPY"}` and prints the regime line and a top-20 table (symbol, setup, buy above, stop, shares, $risk, RS).
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): look-ahead-free nightly scan and scan CLI`.

---

### Task 11: Trade-level backtest, stats, random-entry baseline

**Files:**
- Create: `apps/swing-engine/src/swing_engine/backtest/__init__.py`, `backtest/trades.py`, `backtest/stats.py`, `backtest/baseline.py`
- Test: `tests/test_swing_backtest_trades.py`

**Interfaces:**
- Produces:
  - `candidate_history(panel, kinds, params: SetupParams, start: date, end: date) -> pd.DataFrame`: `CANDIDATE_COLUMNS` plus a `regime` column, for every date in [start, end].
  - `simulate_candidates(panel, ind, candidates, params, ex, delisted: set[str]) -> pd.DataFrame`: one row per candidate, with `TradeOutcome` fields as columns plus `date, symbol, regime`.
  - `trade_stats(trades: pd.DataFrame) -> dict`: over closed trades only. Keys: `n`, `win_rate`, `avg_win_r`, `avg_loss_r`, `expectancy_r`, `profit_factor`, `avg_hold`. Uses `backtest_service.metrics.expectancy/profit_factor/hit_rate` on `r_multiple`.
  - `by_year(trades) -> dict[int, dict]`
  - `regime_on(trades) -> pd.DataFrame`: drops `regime == "downtrend"`.
  - `random_entry_baseline(panel, ind, leaders, candidates, params, ex, seeds: int = 100, delisted: set[str] = frozenset()) -> np.ndarray`: expectancy per seed. For each date with k candidates, draw k symbols uniformly from that date's `leaders` (rng `np.random.default_rng(seed)`). Each draw enters at the next open (`trigger=0.0`, `ExecutionParams(slippage_bps=ex.slippage_bps, max_gap_atr=inf)`), with initial stop = entry − D×atr14, where D = that date's median `(trigger − stop)/atr14` among the real candidates. Exits use the same `trail_atr`/`max_hold`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_trade_stats_on_known_r_values():
    t = closed_trades(r=[2.0, -1.0, -1.0, 1.0], hold=[10, 3, 4, 8])
    s = trade_stats(t)
    assert s["n"] == 4 and s["win_rate"] == 0.5 and s["expectancy_r"] == pytest.approx(0.25)
    assert s["profit_factor"] == pytest.approx(1.5) and s["avg_hold"] == pytest.approx(6.25)

def test_open_and_untriggered_rows_excluded_from_stats(): ...

def test_random_baseline_is_deterministic_per_seed_and_uses_leaders_only():
    a = random_entry_baseline(..., seeds=5); b = random_entry_baseline(..., seeds=5)
    assert np.array_equal(a, b) and a.shape == (5,)

def test_delisted_candidate_counted_with_delisted_exit(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_backtest_trades.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Simulate per symbol with that symbol's columns (open/high/low/close plus `ind.atr14`). `delisted` comes from `assets.status == "inactive"`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): trade-level backtest, stats and random-entry baseline`.

---

### Task 12: Capacity-limited portfolio simulation and SPY benchmark

**Files:**
- Create: `apps/swing-engine/src/swing_engine/backtest/portfolio.py`
- Test: `tests/test_swing_portfolio.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PortfolioResult` with fields `equity: pd.Series`, `taken: pd.DataFrame`, `cagr: float`, `sharpe: float`, `max_dd: float`, `exposure: float`
  - `simulate_portfolio(trades: pd.DataFrame, close: pd.DataFrame, settings: RiskSettings, start: date, end: date) -> PortfolioResult`. `trades` comes from `simulate_candidates` (closed and delisted rows); it needs `rs_pct` and `regime`.
  - `buy_and_hold(close: pd.Series, start: date, end: date) -> PortfolioResult`
- Algorithm:
  - Walk sessions in order.
  - At each session, first book exits dated that session at their `exit_price`.
  - Then consider entries dated that session, ordered by `rs_pct` desc. Accept while open positions < `max_positions` and open risk + new risk ≤ `max_total_risk_pct × equity`.
  - Shares = `size_idea(entry_price, initial_stop, settings_with_current_equity, REGIME_FACTOR[regime]).shares`; skip if 0.
  - Mark to market at close daily. Cash earns 0.
  - `sharpe`/`max_dd`/`cagr` via `backtest_service.metrics` (`sharpe` on daily returns, 252 periods).

- [ ] **Step 1: Write the failing tests:**

```python
def test_capacity_limits_concurrent_positions_to_max(): ...    # 10 same-day entries, max_positions=8 → 8 taken, top RS first
def test_total_open_risk_cap_6_percent(): ...
def test_equity_marks_to_market_and_books_exit_price(): ...     # 1 trade, hand-computed equity path
def test_buy_and_hold_max_dd_matches_hand_value(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_portfolio.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): capacity-limited portfolio simulation and SPY benchmark`.

---

### Task 13: Validation harness (grid, pre-registration, budget, gate, report), deflated-Sharpe fix

**Files:**
- Create: `apps/swing-engine/src/swing_engine/validation/__init__.py`, `validation/protocol.py`, `validation/prereg.py`, `validation/run.py`
- Modify: `apps/swing-engine/src/swing_engine/scan.py` (`default_setups()` reads `load_verdicts`), `__main__.py` (wire `insample`, `oos`)
- Modify: `apps/backtest-service/src/backtest_service/metrics.py` (`deflated_sharpe`), `tests/test_backtest.py::test_deflated_sharpe_below_one_for_overfit` (replace)
- Create dir: `ml/validation/` (reports and pre-registrations are committed here)
- Test: `tests/test_swing_validation.py`, `tests/test_backtest.py`

**Interfaces:**
- Produces, in `protocol.py`:
  - `IS_START = date(2016, 1, 1)`, `IS_END = date(2022, 12, 31)`, `OOS_START = date(2023, 1, 1)`, `MAX_OOS_RUNS = 3`, `REFRESH_MONTHS = 12`, `MIN_IS_TRADES = 100`
  - `GRIDS = {"breakout": {"stop_atr": [1.5, 2.0, 2.5], "trail_atr": [2.5, 3.0, 3.5]}, "pullback": {"trail_atr": [2.5, 3.0, 3.5], "stop_buffer_atr": [0.25, 0.5]}}`
  - `@dataclass(frozen=True) class GateThresholds(min_expectancy_r=0.0, min_profit_factor=1.2, min_profit_factor_low_coverage=1.3, coverage_floor=0.90, min_trades=100)`
  - `@dataclass(frozen=True) class Verdict: passed: bool; failures: list[str]`
  - `evaluate_gate(oos: dict, baseline_median: float, portfolio_max_dd: float, spy_max_dd: float, coverage: float, t: GateThresholds = GateThresholds()) -> Verdict`. Failure strings are exactly:
    - `"expectancy {x:.2f}R ≤ 0"`
    - `"profit factor {pf:.2f} < {floor:.2f}"`
    - `"only {n} trades (need 100)"`
    - `"expectancy {x:.2f}R not above random entry {b:.2f}R"`
    - `"drawdown {dd:.0%} worse than SPY {spy:.0%}"`
  - `select_variant(rows: list[dict]) -> dict`: highest IS `expectancy_r` among rows with `n ≥ 100`; tie broken by `profit_factor`; raises `ValueError("no variant has ≥100 in-sample trades")`.
- Produces, in `prereg.py`:
  - `@dataclass(frozen=True) class Prereg` with fields `family`, `version`, `params: dict`, `gate: dict`, `is_window: list[str]`, `oos_start: str`, `hypothesis: str`, `created_at: str`, `sha256: str`
  - `write_draft(path: Path, prereg: Prereg) -> None`
  - `check_committed(path: Path, repo: Path) -> Prereg`. It raises `PreregError` if the file is untracked (`git ls-files --error-unmatch`) or differs from HEAD (`git diff --quiet HEAD -- path`), or if `version` ≠ `<family>-v1` and `hypothesis` is empty.
- Produces, in `run.py`:
  - `run_insample(family: str, repo: Path) -> dict`: loads data via the store, writes `ml/validation/<family>/<version>.insample.json` (all grid rows plus the chosen variant) and a draft `<version>.prereg.json`, and returns a summary.
  - `run_oos(family: str, version: str, repo: Path, now: datetime) -> dict`:
    - Checks the budget: count existing `<family>/*.report.json` files; refuse with `BudgetError` at ≥ 3 unless the latest freeze is ≥ 12 months old, in which case the new OOS window starts the day after it.
    - Calls `check_committed`, freezes the OOS end at the last stored session, computes everything, and writes `<version>.report.json`.
    - The report has keys `family, version, prereg_sha256, run_index, n_trials, freeze_date, oos_window, is_stats, grid, oos_trades, oos_trades_regime_off, by_year, baseline:{median,p5,p95}, portfolio_alone, portfolio_combined, spy, coverage, fingerprint, deflated_sharpe, verdict:{passed, failures}`.
  - `load_verdicts(repo: Path) -> dict[str, dict]`: the latest report per family, `{"version", "live": bool, "failures", "report_path"}`. Families with no report map to `{"live": False, "version": None, "failures": ["not yet tested"]}`.
  - `params_for(family: str, version: str | None, repo: Path) -> SetupParams`: the params recorded in `<family>/<version>.prereg.json`. If `version` is `None` or the file is missing, returns the family's dataclass defaults.
  - `scan.default_setups(repo)` now returns `SetupSpec(params_for(f, v["version"], repo), live=v["live"], regime_gate=<report regime_filter.helps, True if no report>)` for each family in `load_verdicts(repo)`.
  - The report also carries:
    - `regime_filter: {"helps": bool, "on": stats, "off": stats}`. `helps` = OOS expectancy with the regime filter ≥ without it **and** portfolio maxDD with it is no deeper.
    - `ranking: {"quintile_expectancy": [q1..q5], "rs_helps": bool}`. These are OOS trade-level expectancies by `rs_pct` quintile, q5 = strongest. `rs_helps` = q5 > q1.
  - `ranking_choice(repo: Path) -> Literal["rs", "proximity"]`: `"proximity"` if any Live family's report has `rs_helps == False`, else `"rs"`. The `scan` CLI and Phase 2 pass it as `rank_by`.
  - `data_fingerprint(meta: dict) -> dict`: symbols count, first/last date, row count, sha256 of the sorted `symbol:last_bar` list.
  - `coverage` = share of inactive COMMON assets with ≥ 1 daily bar. `n_trials` = grid size + run_index.
- Modifies, in `metrics.py`: `deflated_sharpe(observed_sharpe, n_trials, n_obs, skew=0.0, kurtosis=3.0, trial_sharpe_std: float | None = None)`. Then `e_max = trial_sharpe_std * ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))` for N > 1 and `e_max = 0` for N == 1. `trial_sharpe_std` defaults to `1/sqrt(n_obs-1)`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_deflated_sharpe_single_trial_zero_sharpe_is_half():
    assert deflated_sharpe(0.0, 1, 500) == pytest.approx(0.5, abs=1e-6)
def test_deflated_sharpe_penalises_more_trials():
    assert 0.9 < deflated_sharpe(0.1, 9, 1000) < 0.99
    assert deflated_sharpe(0.1, 100, 1000) < deflated_sharpe(0.1, 9, 1000)

def test_gate_failure_messages_exact():
    v = evaluate_gate({"expectancy_r": 0.1, "profit_factor": 1.05, "n": 80}, 0.15, -0.30, -0.25, coverage=0.95)
    assert v.failures == ["profit factor 1.05 < 1.20", "only 80 trades (need 100)",
                          "expectancy 0.10R not above random entry 0.15R", "drawdown 30% worse than SPY 25%"]
def test_low_coverage_raises_profit_factor_floor():
    assert "profit factor 1.25 < 1.30" in evaluate_gate({"expectancy_r": .3, "profit_factor": 1.25, "n": 200}, 0.0, -.1, -.2, 0.80).failures
def test_select_variant_requires_100_trades_and_breaks_ties_by_pf(): ...
def test_prereg_must_be_committed(tmp_git_repo):    # tmp git repo fixture: git init in tmp_path
    with pytest.raises(PreregError): check_committed(untracked_path, tmp_git_repo)
def test_revision_requires_hypothesis(tmp_git_repo): ...
def test_budget_refuses_fourth_oos_run(tmp_path): ...       # 3 existing report files, latest freeze < 12 months
def test_load_verdicts_defaults_to_not_yet_tested(tmp_path):
    assert load_verdicts(tmp_path)["breakout"] == {"live": False, "version": None, "failures": ["not yet tested"], "report_path": None}
def test_default_setups_use_preregistered_params_and_verdict(tmp_path):
    ...  # write breakout-v1.prereg.json with stop_atr=2.5 and a passing report
    spec = {s.params.version: s for s in default_setups(tmp_path)}["breakout-v1"]
    assert spec.live and spec.params.stop_atr == 2.5
def test_regime_filter_that_does_not_help_turns_into_warning(tmp_path):
    ...  # report with regime_filter.helps = False
    assert {s.params.version: s for s in default_setups(tmp_path)}["breakout-v1"].regime_gate is False
def test_ranking_choice_falls_back_to_proximity_when_rs_does_not_help(tmp_path):
    ...  # live report with ranking.rs_helps = False
    assert ranking_choice(tmp_path) == "proximity"
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_validation.py tests/test_backtest.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces. Replace the old overfit test with the two `deflated_sharpe` tests above (the old one asserted < 1 for a per-period Sharpe of 1.0, which is correctly ≈ 1).
- [ ] **Step 4:** Run the same command. Expected: PASS. Then run `uv run pytest -q`. Expected: no new failures.
- [ ] **Step 5: Commit** with message `feat(swing): pre-registered validation harness and ship gate; fix deflated Sharpe`.

---

### Task 14: Legacy pipeline fixes (label leak, legacy model refusal, broadcaster noise)

**Files:**
- Modify: `apps/quant-engine/src/quant_engine/train.py`, `apps/quant-engine/src/quant_engine/trend.py` (`TrendArtifact.label_scheme: str | None = None`; `train_trend` sets `"per_symbol_v2"`)
- Modify: `apps/signal-service/src/signal_service/state.py`, `apps/signal-service/src/signal_service/stream.py`
- Test: `tests/test_quant_label_leak.py`, `tests/test_legacy_guards.py`

**Interfaces:**
- Produces:
  - `quant_engine.train.build_training_set(features: pd.DataFrame, horizon: int, pt_mult: float, sl_mult: float) -> tuple[pd.DataFrame, pd.Series, int]`. It labels each symbol on its own `close`/`atr14` (group by `symbol` when present), then stacks in `ts, symbol` order. It returns `(features, labels, label_horizon_rows)` with `label_horizon_rows = horizon × n_symbols`; `main()` uses it.
  - `signal_service.state.load_trend_model` refuses an artifact whose `label_scheme != "per_symbol_v2"` unless `ATLAS_ALLOW_LEGACY_MODEL=1`. On refusal it logs `trend_model.legacy_refused` and returns `None`.
  - `signal_service.stream._LEGACY_SIGNAL_STREAM = os.environ.get("ATLAS_ENABLE_LEGACY_SIGNAL_STREAM", "0") == "1"`, plus a new `async def _tick(tick: int) -> None` that the loop calls. `_push_signals` runs only when the flag is on; `_push_regime` always runs.

- [ ] **Step 1: Write the failing tests:**

```python
def test_random_walks_score_near_chance_with_per_symbol_labels():
    feats = stacked_random_walk_features(prices=[5, 40, 100, 230, 450, 900], n=4000)   # gbm_bars per symbol, sorted ts,symbol
    X, y, h = build_training_set(feats, horizon=20, pt_mult=2.0, sl_mult=1.0)
    art = train_trend(X, y, label_horizon=h)
    assert art.metrics["oof_auc"] < 0.65 and art.label_scheme == "per_symbol_v2"

def test_legacy_artifact_refused(tmp_path, monkeypatch):
    save(TrendArtifact(model=..., calibrator=..., feature_cols=FEATURE_COLS, version="old"), tmp_path / "old.joblib")
    assert load_trend_model(tmp_path / "old.joblib") is None

async def test_tick_skips_signal_push_when_legacy_stream_off(monkeypatch):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", False)
    push = AsyncMock(); monkeypatch.setattr(stream, "_push_signals", push)
    monkeypatch.setattr(stream, "_push_regime", AsyncMock())
    await stream._tick(3)
    push.assert_not_called()
```

- [ ] **Step 2:** Run `uv run pytest tests/test_quant_label_leak.py tests/test_legacy_guards.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4:** Run the same command, then `uv run pytest -q`. Expected: PASS, no new failures.
- [ ] **Step 5: Commit** with message `fix(quant): per-symbol labels; refuse leaky legacy model; legacy signal stream off by default`.

---

### Task 15: Phase 1 docs

**Files:**
- Modify: `docs/architecture/SYSTEM.md` (add an `` `apps/swing-engine` `` row and a "Swing engine (rules-based daily)" lifecycle block), `README.md` (a "Swing engine" section with `uv run python -m swing_engine backfill`, `update`, `scan`, `insample --family breakout`, `oos --family breakout --version breakout-v1`; remove `ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib` from the quick start), `CHANGELOG.md` (`## 0.24.0` entry), `CLAUDE.md` (architecture: swing engine is the primary path; the composite is legacy)
- Create: `docs/adr/0003-rules-based-swing-engine.md` (the pivot: daily bars, rules-based, validation-gated, composite retired; cite the audit evidence in spec §1)
- Test: existing `tests/test_docs.py`, `tests/test_cli_entrypoints.py`

- [ ] **Step 1:** Edit the docs above.
- [ ] **Step 2:** Run `uv run pytest tests/test_docs.py tests/test_cli_entrypoints.py -q`. Expected: PASS.
- [ ] **Step 3: Commit** with message `docs: swing engine phase 1, ADR-0003`.

---

### Task 16: Checkpoint run on real data (stop for the user)

No new code. This produces the in-sample evidence the user reviews before any out-of-sample run (spec §7.16).

- [ ] **Step 1:** Make sure Docker is running (`docker info`). If it isn't, ask the user to start Docker Desktop. Then run `docker compose -f infra/docker/docker-compose.yml up -d && uv run python -m atlas_shared.migrate up`. Expected: `0009_swing_assets.sql` applied.
- [ ] **Step 2:** Run `uv run python -m swing_engine backfill` (in the background; expect tens of minutes). Expected: completes, logging symbols done / total. Record the row count, symbol count, and delisted coverage.
- [ ] **Step 3:** Run `uv run python -m swing_engine scan`. Expected: a regime line and a top-20 table for the last completed session. Spot-check 3 tickers' trigger and stop against a chart.
- [ ] **Step 4:** Run `uv run python -m swing_engine insample --family breakout` and `... --family pullback`. Expected: an `ml/validation/<family>/<family>-v1.insample.json` file and a draft `.prereg.json` for each.
- [ ] **Step 5: Commit** the two `.insample.json` and two `.prereg.json` files with message `chore(validation): in-sample results and pre-registration for v1 setups`.
- [ ] **Step 6: Stop and report to the user:**
  - Universe size and delisted coverage.
  - For each family: the 9- or 6-row grid, the chosen variant, and its in-sample win rate, expectancy, profit factor, trade count and by-year results.
  - The pre-registered thresholds.

  Ask whether to veto a variant. **Do not run `oos` until the user answers.** Phase 2 may start in parallel.
