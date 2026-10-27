# Swing Engine Phase 2 (Product: Nightly Job, Positions, Dashboard, Digest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1 engine into something the user opens and uses.
- After every close, an automatic nightly scan (with catch-up) produces a ranked "Today" list.
- Every published idea is forward-tracked.
- The user records positions and ATLAS tells them nightly what to do with them.
- Track record and Settings pages, a Telegram digest, and one command to start it all.

**Architecture:** New orchestration modules in `swing_engine` (store, tracking, positions, narrate, digest, job, scheduler) stay pure where possible. Only `product_store.py` touches the DB, and the job takes injected dependencies. `signal_service` gains `swing_routes.py` and starts the scheduler in its lifespan. `apps/web` gets four new primary pages; the old pages stay reachable by URL but leave the nav.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, TimescaleDB, httpx, zoneinfo; Next.js 14 App Router, TanStack Query, Tailwind, lightweight-charts 5.

**Spec:** `docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md` (§7.9–§7.14, §8). **Depends on** `docs/superpowers/plans/2026-10-18-swing-engine-phase1.md` Tasks 1–13 being merged. Interfaces named below without a task number are defined there.

## Global Constraints

- Everything in Phase 1's Global Constraints applies: branch `swing-rebuild`, stage only your files, commit attribution line, free data only, long-only, no auto-execution, no unmeasured "confidence", tests pass without Postgres, flat `tests/test_swing_<area>.py`.
- New tables use `DOUBLE PRECISION`/`DATE`/`TIMESTAMPTZ`/`JSONB`/`TEXT[]`, never `NUMERIC`.
- **Unproven setups** (user decided):
  - Their ideas show on Today under the exact heading `Unproven ideas: these setups have not passed testing`, with muted styling and an `Unproven` tag on every row.
  - They are never included in the Telegram digest.
- The scheduler never starts under pytest: `"PYTEST_CURRENT_TEST" not in os.environ`, and it is also disabled by `ATLAS_DISABLE_SCHEDULER=1`.
- The UI uses plain-English labels. No `§` references, sub-score jargon, tier badges or the word "confidence" on primary pages. Keep the Tailwind tokens `bg`, `ink`, `line`, `bull`, `bear`, `warn`, `accent`.
- Every page shows a stated reason instead of an empty area. Exact empty-state copy:
  - `Market in downtrend: no new buy ideas (stand aside). Strongest names below are watch-only.`
  - `First-time setup: downloading 10 years of daily data ({pct}%)`
  - `No setup has passed testing yet`
  - `Last scan failed at step {step}: {reason}. Showing the previous scan.`
  - `No stock met a setup's rules after {session}'s close.`
  - `Can't reach the ATLAS API. Start it with scripts/atlas.sh up.`
- Regime label copy:
  - `UPTREND, new trades OK`
  - `MIXED, half-size trades`
  - `DOWNTREND, stand aside: no new buy ideas`
- Disclaimer line (digest and Today footer): `Informational only, not financial advice. You place every order yourself.`
- Frontend checks: `cd apps/web && pnpm typecheck && pnpm lint`. There is no JS unit-test runner, so UI behaviour is verified in the browser in Task 16.

## Review Focus

1. **The Mac was asleep for two or more sessions.** Expected: catch-up scans each missed session in order, marks those ideas `reconstructed`, and sends exactly one digest that lists the missed days (test in Task 6).
2. **Half days and the DST switch.** Expected: the next scan is close + 45 min ET. That's 13:45 ET on 2026-11-27 and 16:45 EST (21:45 UTC) on Mon 2026-11-02, after DST ends (test in Task 7).
3. **Two scans for the same session** (scheduled + "Run scan now", or two clicks). Expected: one set of ideas per (session, symbol, setup), `published_at` preserved from the first, no second digest, and a concurrent request gets 409 (tests in Tasks 6 and 8).
4. **A position in a stock with no bar on the scan day** (delisted, halted, typo'd symbol). Expected: action `no price data for {symbol} since {date}`, with no crash and no silently stale stop (test in Task 3).
5. **API or DB unreachable, or the very first run with no data.** Expected: Today shows the matching empty-state copy, never a blank page or a spinner forever (test in Task 8 for the API side; browser check in Task 16 for the UI side).

---

### Task 1: Migration 0010, product store, settings model

**Files:**
- Create: `packages/shared-py/migrations/0010_swing_product.sql`
- Create: `apps/swing-engine/src/swing_engine/product_store.py`, `apps/swing-engine/src/swing_engine/settings.py`
- Test: `tests/test_swing_product_store.py`

**Interfaces:**
- Migration tables:
  - `swing_settings(id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1), equity, risk_pct, max_position_pct, max_total_risk_pct, min_price, min_dollar_volume DOUBLE PRECISION, max_positions INT, telegram_digest BOOLEAN, updated_at TIMESTAMPTZ)`
  - `swing_scans(session DATE PRIMARY KEY, target_session DATE, generated_at TIMESTAMPTZ, regime TEXT, breadth DOUBLE PRECISION, universe_size INT, reconstructed BOOLEAN, payload JSONB)`
  - `swing_ideas(id UUID PK DEFAULT gen_random_uuid(), session DATE, target_session DATE, symbol TEXT, setup TEXT, version TEXT, live BOOLEAN, trigger, stop, atr14, rs_pct DOUBLE PRECISION, rank INT, shares INT, risk_dollars, position_value DOUBLE PRECISION, overlaps TEXT[], published_at TIMESTAMPTZ, reconstructed BOOLEAN, track_status TEXT DEFAULT 'pending', entry_date DATE, entry_price DOUBLE PRECISION, exit_date DATE, exit_price DOUBLE PRECISION, exit_reason TEXT, r_multiple DOUBLE PRECISION, current_stop DOUBLE PRECISION, UNIQUE (session, symbol, setup))`
  - `swing_positions(id UUID PK, symbol TEXT, opened_on DATE, entry_price, shares DOUBLE PRECISION, initial_stop, current_stop, trail_atr DOUBLE PRECISION, idea_id UUID NULL, status TEXT DEFAULT 'open', closed_on DATE, exit_price DOUBLE PRECISION, exit_reason TEXT, r_multiple DOUBLE PRECISION, last_action TEXT, last_action_text TEXT, last_action_on DATE, notes TEXT, created_at TIMESTAMPTZ DEFAULT now())`
  - `swing_job_runs(id UUID PK, kind TEXT, session DATE, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, status TEXT, steps JSONB, error TEXT)`
  - `swing_digests(session DATE, kind TEXT, sent_at TIMESTAMPTZ, ok BOOLEAN, detail TEXT, PRIMARY KEY (session, kind))`
- Produces, in `settings.py`: `class SwingSettings(BaseModel)`. Fields and validation:

  | Field | Default | Validation |
  |---|---|---|
  | `equity` | 25000 | > 0 |
  | `risk_pct` | 0.01 | 0 < x ≤ 0.05 |
  | `max_position_pct` | 0.20 | 0 < x ≤ 1 |
  | `max_positions` | 8 | 1–50 |
  | `max_total_risk_pct` | 0.06 | 0 < x ≤ 0.5 |
  | `min_price` | 10.0 | ≥ 1 |
  | `min_dollar_volume` | 20_000_000 | ≥ 1_000_000 |
  | `telegram_digest` | True | |

  Methods: `.risk() -> RiskSettings`, `.universe() -> UniverseFilter`, `.differs_from_default_universe() -> bool`.
- Produces, in `product_store.py` (async):
  - `get_settings() -> SwingSettings` (defaults when no row), `put_settings(s) -> SwingSettings`
  - `save_scan(scan: ScanResult, target_session: date, published_at: datetime, reconstructed: bool) -> None`. Upserts the scan and its ideas (`ideas` + `watch_only`). On conflict it updates sizing, rank and live, but keeps `published_at` and `reconstructed`.
  - `latest_scan() -> dict | None`, `scanned_sessions(since: date) -> set[date]`
  - `trackable_ideas() -> list[dict]` (`track_status IN ('pending','open')`), `update_idea_tracks(rows: list[dict]) -> None`, `idea_rows(setup: str | None = None) -> list[dict]`
  - `list_positions(status: str | None) -> list[dict]`, `create_position(row) -> dict`, `update_position(id, fields) -> dict`, `get_position(id) -> dict | None`
  - `start_job(kind, session) -> UUID`, `finish_job(id, status, steps, error) -> None`, `latest_job() -> dict | None`
  - `digest_sent(session, kind) -> bool`, `mark_digest(session, kind, ok, detail) -> None`

- [ ] **Step 1: Write the failing tests:**

```python
def test_settings_defaults_and_bounds():
    assert SwingSettings().risk().risk_pct == 0.01 and SwingSettings().universe().min_price == 10.0
    with pytest.raises(ValidationError): SwingSettings(risk_pct=0)
    with pytest.raises(ValidationError): SwingSettings(max_positions=0)
    with pytest.raises(ValidationError): SwingSettings(equity=-1)
def test_differs_from_default_universe():
    assert SwingSettings(min_price=5).differs_from_default_universe()
def test_migration_0010_no_numeric_and_unique_idea_key():
    sql = (MIGRATIONS / "0010_swing_product.sql").read_text().upper()
    assert "NUMERIC" not in sql and "UNIQUE (SESSION, SYMBOL, SETUP)" in sql
def test_save_scan_sql_preserves_published_at_on_conflict():
    assert "published_at" not in _UPSERT_IDEA_SQL.text.split("DO UPDATE SET")[1]
@db
async def test_save_scan_twice_keeps_first_published_at(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_product_store.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces. Route responses use plain floats; there are no NUMERIC columns.
- [ ] **Step 4:** Run the same command. Expected: PASS. If Postgres is up, run `uv run python -m atlas_shared.migrate up`.
- [ ] **Step 5: Commit** with message `feat(swing): product tables, store and validated settings`.

---

### Task 2: Forward tracking and the live/reconstructed rule

**Files:**
- Create: `apps/swing-engine/src/swing_engine/tracking.py`
- Test: `tests/test_swing_tracking.py`

**Interfaces:**
- Consumes: `simulate_trade`, `TrackStatus` (P1 Task 8), `params_for` (P1 Task 13), `ScanContext` (P1 Task 10).
- Produces:
  - `is_live(published_at: datetime, target_session: date) -> bool`: True iff `published_at < 09:30 America/New_York on target_session`.
  - `track_ideas(ideas: Sequence[dict], ctx: ScanContext, delisted: set[str], repo: Path) -> list[dict]`. Each row: `{id, track_status, entry_date, entry_price, exit_date, exit_price, exit_reason, r_multiple, current_stop}`. It re-simulates each idea from its `session` through `ctx.session`, using `params_for(setup, version, repo)` for `trail_atr`/`max_hold` and `ExecutionParams()`. That makes it idempotent with no incremental state.
  - `live_stats(rows: Sequence[dict], reconstructed: bool) -> dict`: `trade_stats` over closed rows with matching `reconstructed`, plus `{"open": n_open}`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_is_live_cutoff_is_0930_et_of_target_session():
    assert is_live(datetime(2026, 10, 16, 21, 0, tzinfo=UTC), date(2026, 10, 19))
    assert not is_live(datetime(2026, 10, 19, 13, 31, tzinfo=UTC), date(2026, 10, 19))   # 09:31 EDT
def test_track_ideas_is_idempotent_and_matches_simulate_trade(): ...
def test_reconstructed_rows_kept_out_of_live_stats(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_tracking.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): forward tracking of published ideas`.

---

### Task 3: Nightly position actions

**Files:**
- Create: `apps/swing-engine/src/swing_engine/positions.py`
- Test: `tests/test_swing_positions.py`

**Interfaces:**
- Produces:
  - `class ActionKind(StrEnum)` with members `HOLD="hold"`, `RAISE_STOP="raise_stop"`, `STOP_HIT="stop_hit"`, `MAX_HOLD="max_hold"`, `NO_DATA="no_data"`
  - `@dataclass(frozen=True) class Action: position_id: str; symbol: str; kind: ActionKind; stop: float | None; text: str`
  - `position_action(pos: dict, ctx: ScanContext, max_hold: int = 30) -> Action`. `pos` has `id, symbol, opened_on, entry_price, initial_stop, current_stop, trail_atr`.
- Rules, in order, evaluated at `d = ctx.session`:
  1. No bar for the symbol at `d` → `NO_DATA`, text `no price data for {symbol} since {last_bar_date}`.
  2. `opened_on == d` → `HOLD`, text `hold, stop {current_stop:.2f}`.
  3. `low[d] ≤ current_stop` → `STOP_HIT`, text `stop hit, exit (stop {current_stop:.2f})`.
  4. Sessions with bars from `opened_on` through `d` inclusive ≥ `max_hold` → `MAX_HOLD`, text `max hold reached (30 sessions), consider exiting`.
  5. New trail = max(current_stop, max close since opened_on − trail_atr × atr14[d]). If it exceeds current_stop by ≥ $0.01 → `RAISE_STOP`, text `raise stop to {new:.2f} (trail)`.
  6. Otherwise `HOLD`, text `hold, stop {current_stop:.2f}`.

- [ ] **Step 1: Write the failing tests:** one test per rule above, asserting `kind`, `stop` and the exact `text`. Include `test_no_bar_on_scan_day_is_no_data_not_crash`.
- [ ] **Step 2:** Run `uv run pytest tests/test_swing_positions.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): nightly actions for user positions`.

---

### Task 4: Plain-English narration and the eligibility checklist

**Files:**
- Create: `apps/swing-engine/src/swing_engine/narrate.py`, `apps/swing-engine/src/swing_engine/checklist.py`
- Test: `tests/test_swing_narrate.py`

**Interfaces:**
- Produces, in `narrate.py`:
  - `SETUP_LABEL = {"breakout": "Breakout", "pullback": "Pullback"}`
  - `why(idea: Idea, ctx: ScanContext) -> str`:
    - Breakout: `"{SYM} is in a long-term uptrend (above its 50- and 200-day averages) and stronger than {rs:.0f}% of US stocks over the past year. It closed within {gap:.1f}% of its 50-day high after a quiet stretch (recent daily range {c:.0%} of normal). A move above {trigger:.2f} next session would be a breakout."`
    - Pullback: `"{SYM} is in a long-term uptrend and stronger than {rs:.0f}% of US stocks, but has dipped over the last few days ({detail}). Buying only above {trigger:.2f}, today's high, waits for buyers to step back in."`, where `detail` is `"2-day RSI {rsi2:.0f}"` or `"closed below its 20-day average"`.
  - `cancels(idea: Idea, target_session: date) -> str`: `"Cancelled if it doesn't trade above {trigger:.2f} on {Mon 19 Oct}, or if it opens more than one ATR above that. After entry, the trade is wrong below {stop:.2f}; the stop then trails {trail:.1f} ATR under the highest close, and the trade ends after {max_hold} sessions."`
- Produces, in `checklist.py`:
  - `@dataclass(frozen=True) class CheckItem: rule: str; passed: bool; value: str; threshold: str`
  - `eligibility_checklist(ctx: ScanContext, symbol: str, universe: UniverseFilter) -> list[CheckItem]`. The rules, exactly these labels in this order:
    1. `Has a bar for {session}`
    2. `Price at least ${min_price:.0f}`
    3. `50-day median dollar volume at least ${m}M`
    4. `At least 252 sessions of history`
    5. `Common stock (not an ETF or fund)`
    6. `Close above 50-day average`
    7. `50-day average above 200-day average`
    8. `200-day average rising (vs 20 sessions ago)`
    9. `Stronger than 70% of US stocks (relative strength)`
    10. `Within 25% of 52-week high`
    11. `Breakout: close within 2% of 50-day closing high`
    12. `Breakout: quiet stretch (ATR10 / ATR50 at most 0.85)`
    13. `Pullback: 2-day RSI at most 10 or close below 20-day average`

- [ ] **Step 1: Write the failing tests:** golden strings for one breakout and one pullback idea built from a small panel. `test_checklist_marks_failed_rules_with_values` covers an ineligible ETF and a non-leader. `test_copy_has_no_promises` asserts that no text from `why`/`cancels` contains any of `guarantee`, `will rise`, `sure`, `profit`.
- [ ] **Step 2:** Run `uv run pytest tests/test_swing_narrate.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): plain-English idea narration and eligibility checklist`.

---

### Task 5: Telegram `send_text` and the digest format

**Files:**
- Modify: `apps/alert-service/src/alert_service/channels/telegram.py`. Add a `transport: httpx.AsyncBaseTransport | None = None` ctor param used by both `send` and new `async send_text(self, text: str) -> DeliveryResult`, which goes through the same `redact_secrets` path.
- Create: `apps/swing-engine/src/swing_engine/digest.py` (add `alert-service` to swing-engine deps)
- Test: `tests/test_swing_digest.py`

**Interfaces:**
- Produces:
  - `format_digest(scan: ScanResult, target_session: date, actions: Sequence[Action], missed: Sequence[date], unproven_count: int) -> str`
  - `format_failure(session: date, step: str, reason: str) -> str` = `"ATLAS scan failed at step {step}: {reason}"`
  - `async send_digest(text: str, channel: TelegramChannel | None = None) -> DeliveryResult`

- [ ] **Step 1: Write the failing tests.** The golden digest, live ideas only, top 5:

```python
def test_digest_golden():
    assert format_digest(scan_uptrend_two_live_one_unproven, date(2026, 10, 19), [raise_msft], [], 1) == (
        "ATLAS · ideas for Mon 19 Oct\n"
        "Market: UPTREND, new trades OK · breadth 61%\n"
        "Top ideas (validated setups):\n"
        "1. XYZ Breakout · buy above 148.20 · stop 140.10 · 30 sh · risk $243\n"
        "2. ABC Pullback · buy above 72.15 · stop 69.30 · 87 sh · risk $248\n"
        "Your positions:\n"
        "• MSFT raise stop to 512.40 (trail)\n"
        "Informational only, not financial advice. You place every order yourself."
    )
def test_digest_without_live_setups_lists_no_unproven_ideas():
    text = format_digest(scan_only_unproven, date(2026, 10, 19), [], [], 6)
    assert "No validated setups yet; 6 unproven ideas on the dashboard" in text and "buy above" not in text
def test_digest_lists_missed_sessions():
    assert "Missed sessions caught up: Thu 15 Oct, Fri 16 Oct" in format_digest(..., missed=[date(2026,10,15), date(2026,10,16)], ...)
def test_downtrend_digest_says_stand_aside(): ...   # contains "Market: DOWNTREND, stand aside: no new buy ideas" and no "buy above"
async def test_send_text_redacts_token_on_error(): ...   # MockTransport raising → detail has no token
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_digest.py tests/test_phase6_alerts.py -q`. Expected: FAIL (new tests), existing tests PASS.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): Telegram digest (validated ideas only)`.

---

### Task 6: Nightly job orchestrator with catch-up, failure handling and idempotency

**Files:**
- Create: `apps/swing-engine/src/swing_engine/job.py`
- Modify: `apps/swing-engine/src/swing_engine/__main__.py`: add a `job` subcommand that runs `run_job(kind="manual")` once with real deps and prints the step statuses (spec §7.11 "manual … CLI").
- Test: `tests/test_swing_job.py`

**Interfaces:**
- Consumes: Phase 1 ingest/scan/context/validation, plus Tasks 1–5.
- Produces:
  - `STEPS = ("assets", "bars", "scan", "tracking", "positions", "digest")`
  - `@dataclass class JobDeps` with fields `client: AlpacaData`, `calendar: Callable[[], Awaitable[TradingCalendar]]`, `bars_store` (Phase 1 `BarStore`), `store` (the `product_store` module or a fake with the same functions), `send: Callable[[str], Awaitable[DeliveryResult]]`, `repo: Path`, `now: Callable[[], datetime]`
  - `class JobBusy(RuntimeError)`
  - `async run_job(deps: JobDeps, kind: Literal["nightly", "catchup", "manual"]) -> dict` (the finished `swing_job_runs` row). A module-level `asyncio.Lock` raises `JobBusy` when a run is in progress.
  - `on_context: Callable[[ScanContext], None] | None` hook in `JobDeps`, used by the API cache in Task 8.
- Behaviour:
  1. The target session is `last_completed_session(now, 16)`.
  2. `assets`: refresh if `latest_job` shows none in the last 7 days.
  3. `bars`: if the store has no SPY bars, run `backfill` from 2016-01-01, writing `{"bars": {"done": n, "total": m}}` progress into the job's `steps` as it goes. Otherwise run `incremental_update`.
  4. `scan`: the sessions to scan are `sessions_between(last scanned, target)`, or `[target]` if nothing was ever scanned. For each, in order:
     - `build_context` on the panel truncated to that session, with `settings.universe()` applied.
     - `run_scan` with `default_setups(repo)`, `settings.risk()`, `rank_by=ranking_choice(repo)` and open positions.
     - `save_scan(..., target_session=next_session(s), published_at=now, reconstructed=not is_live(now, next_session(s)))`.
  5. `tracking`: `track_ideas` at the target, then `update_idea_tracks`.
  6. `positions`: `position_action` for each open position. Persist `last_action*`, and update `current_stop` when the action is `RAISE_STOP`.
  7. `digest`: if `settings.telegram_digest` and not `digest_sent(target, "digest")`, send `format_digest` with `missed` = scanned sessions other than the target, then mark it.
  8. Failure in any step: record `{step: {"status": "failed", "error": redact_secrets(str(e))}}`, skip the remaining steps, and set job status `failed`. Send `format_failure` at most once per target (`kind="failure"`). The previous scan stays untouched.

- [ ] **Step 1: Write the failing tests** (fake deps, in-memory store, a synthetic panel with SPY):

```python
async def test_catch_up_scans_each_missed_session_in_order_and_sends_one_digest():
    store = FakeProductStore(scanned={date(2026, 10, 14)})
    run = await run_job(deps(now=MON_19_OCT_1700_ET, store=store, sent=sent), "catchup")
    assert store.saved_sessions == [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]
    assert store.reconstructed == {date(2026, 10, 15): True, date(2026, 10, 16): True, date(2026, 10, 19): False}
    assert len(sent) == 1 and "Missed sessions caught up: Thu 15 Oct, Fri 16 Oct" in sent[0]
async def test_second_run_same_session_sends_no_second_digest(): ...
async def test_concurrent_run_raises_job_busy(): ...
async def test_failed_bars_step_keeps_previous_scan_and_notifies_once():
    run = await run_job(deps(bars_raise=RuntimeError("alpaca 403")), "nightly")
    assert run["status"] == "failed" and run["steps"]["bars"]["status"] == "failed" and "scan" not in run["steps"]
    await run_job(deps(bars_raise=RuntimeError("alpaca 403")), "nightly")
    assert len(sent_failures) == 1
async def test_first_ever_run_scans_only_target_session(): ...
async def test_raise_stop_action_updates_position_stop(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_job.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(swing): nightly job with catch-up, idempotent scans and one digest`.

---

### Task 7: Scheduler and lifespan wiring

**Files:**
- Create: `apps/swing-engine/src/swing_engine/scheduler.py`
- Modify: `apps/signal-service/src/signal_service/main.py` (start `swing_scheduler` in the lifespan when enabled), `apps/signal-service/pyproject.toml` (add the `swing-engine` dep)
- Test: `tests/test_swing_scheduler.py`

**Interfaces:**
- Produces:
  - `next_scan_at(cal: TradingCalendar, now: datetime, delay_minutes: int = 45) -> datetime`: the first `cal.scan_time(d)` strictly after `now`.
  - `scheduler_enabled() -> bool`: `os.environ.get("ATLAS_DISABLE_SCHEDULER") != "1" and "PYTEST_CURRENT_TEST" not in os.environ`.
  - `async swing_scheduler(make_deps: Callable[[], JobDeps]) -> None`:
    - On start, if the last scanned session < `last_completed_session(now)`, run `run_job(kind="catchup")`.
    - Then loop: sleep until `next_scan_at`, run `run_job(kind="nightly")`.
    - Log and continue on errors. Refresh the calendar daily.
  - `scheduler_state() -> dict`: `{"running": bool, "next_scan_at": datetime | None}` for the status endpoint.

- [ ] **Step 1: Write the failing tests:**

```python
def test_next_scan_weekend_rolls_to_monday():
    assert next_scan_at(CAL, datetime(2026, 10, 16, 17, 0, tzinfo=ET)) == datetime(2026, 10, 19, 16, 45, tzinfo=ET)
def test_next_scan_half_day():
    assert next_scan_at(CAL, datetime(2026, 11, 27, 9, 0, tzinfo=ET)) == datetime(2026, 11, 27, 13, 45, tzinfo=ET)
def test_next_scan_after_dst_end_is_2145_utc():
    assert next_scan_at(CAL, datetime(2026, 11, 2, 12, 0, tzinfo=ET)).astimezone(UTC) == datetime(2026, 11, 2, 21, 45, tzinfo=UTC)
def test_scheduler_disabled_under_pytest():
    assert scheduler_enabled() is False
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_scheduler.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `make_deps` to build `AlpacaData()`, a cached calendar provider (`client.calendar(today − 400d, today + 30d)`), the stores, and `send_digest`.
- [ ] **Step 4:** Run the same command, then `uv run pytest -q`. Expected: PASS, no new failures (existing `TestClient(app)` tests must not trigger network calls).
- [ ] **Step 5: Commit** with message `feat(swing): after-close scheduler with startup catch-up`.

---

### Task 8: Swing API routes

**Files:**
- Create: `apps/signal-service/src/signal_service/swing_routes.py` (Pydantic response models in the same file)
- Modify: `apps/signal-service/src/signal_service/main.py` (`include_router`)
- Test: `tests/test_swing_routes.py` (TestClient with `product_store`, the context cache and `run_job` monkeypatched; no DB)

**Interfaces:**
- Consumes: Tasks 1–7, `load_verdicts`, `scan_to_dict`, `build_context`.
- Produces:
  - A module-level context cache: `set_context(ctx)` (passed as `JobDeps.on_context`) and `get_context() -> ScanContext | None` (lazy build from the store on first use).
  - Routes and response shapes:

```
GET  /v1/today -> {
  freshness: {data_as_of, ideas_for, next_scan_at, last_scan_status: "ok"|"failed"|"running"|"never", failed_step, failed_reason},
  regime: {state, factor, breadth, label} | null,
  ideas: IdeaOut[], unproven: IdeaOut[], watch_only: IdeaOut[],
  positions: ActionOut[], capacity: {open_positions, max_positions, open_risk_dollars, max_risk_dollars},
  empty_reason: "first_run"|"downtrend"|"no_candidates"|"last_scan_failed"|null,
  backfill: {done, total} | null }
IdeaOut = {symbol, name, setup, setup_label, version, buy_above, stop, target_2r, shares, risk_dollars,
           position_value, rs_pct, rank, live, regime_warning, overlaps, why,
           track_record: {verdict: "live"|"research"|"not_tested", verdict_text, win_rate, expectancy_r, trades, source: "oos"|"insample"|null}}
GET  /v1/stocks/{symbol} -> {symbol, name, session, bars[{time,open,high,low,close,volume}] (last 300),
                             lines: {sma50, sma200, ema20: [{time,value}]}, checklist: CheckItem[],
                             idea: IdeaOut|null, why, cancels, levels: {trigger, stop, target_2r, trail}|null}   (404 if unknown symbol)
GET  /v1/track-record -> {families: [{family, label, verdict, verdict_text, versions: [{version, run_index, freeze_date, passed}],
                          insample, oos, live, reconstructed, baseline: {median, p5, p95},
                          equity: {strategy: [{time,value}], spy: [{time,value}]}, warnings: string[]}]}
GET  /v1/positions?status=open|closed -> PositionOut[]
PositionOut = {id, symbol, name, opened_on, entry_price, shares, initial_stop, current_stop, status, last_close, r_now,
               last_action, last_action_text, last_action_on, closed_on, exit_price, exit_reason, r_multiple, held_sessions}
               (r_now = (last_close - entry)/(entry - initial_stop); held_sessions counted from bars)
ActionOut   = {position_id, symbol, kind, stop, text}
POST /v1/positions {symbol, opened_on, entry_price, shares, initial_stop?, idea_id?} -> PositionOut   (422 if stop >= entry or shares <= 0)
PATCH /v1/positions/{id} {current_stop?, shares?, notes?} -> PositionOut
POST /v1/positions/{id}/close {closed_on, exit_price} -> PositionOut   (r_multiple = (exit - entry)/(entry - initial_stop))
GET  /v1/settings -> SwingSettings ;  PUT /v1/settings -> SwingSettings (422 on invalid)
POST /v1/settings/telegram-test -> {ok, detail}
POST /v1/scan/run -> 202 {job_id} | 409 {detail: "a scan is already running"}
GET  /v1/system/status -> {data_as_of, last_scan: {session, generated_at, status, steps}, next_scan_at, backfill,
                           alpaca_configured, telegram_configured, auto_execution: false, scheduler_running}
```

  - Further rules:
    - `target_2r = buy_above + 2 × (buy_above − stop)`.
    - `track_record` uses OOS stats when a report exists, else in-sample stats (`source`), else nulls.
    - Track record `warnings` include `live underperforming: expectancy {x:.2f}R after {n} live ideas` (when live expectancy < 0 with ≥ 30 closed live ideas) and `validated with default universe filters; yours differ`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_today_first_run_has_empty_reason_and_backfill_progress(client_with_store(no_scan, job_steps={"bars": {"done": 380, "total": 1000}})):
    j = client.get("/v1/today").json()
    assert j["empty_reason"] == "first_run" and j["backfill"] == {"done": 380, "total": 1000}
def test_today_splits_live_and_unproven(): ...
def test_today_downtrend_empty_reason(): ...
def test_today_last_scan_failed_still_returns_previous_ideas(): ...
def test_scan_run_conflict_returns_409(): ...       # run_job raises JobBusy
def test_position_validation_422(): ...
def test_close_position_computes_r(): ...
def test_settings_put_rejects_zero_risk(): ...
def test_stock_unknown_symbol_404(): ...
def test_system_status_reports_auto_execution_false(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_swing_routes.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same command, then `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat(api): today, stocks, track record, positions, settings, scan and status routes`.

---

### Task 9: Web foundation (types, client, format helpers, navigation, stock search)

**Files:**
- Create: `apps/web/src/lib/swing-types.ts` (TypeScript mirrors of every Task 8 shape), `apps/web/src/lib/swing-api.ts`, `apps/web/src/lib/format.ts`, `apps/web/src/components/StockSearch.tsx`, `apps/web/src/components/Term.tsx`, `apps/web/src/components/ApiDown.tsx`
- Modify: `apps/web/src/app/layout.tsx`. The nav becomes `Today · Positions · Track record · Settings` plus `StockSearch` ("Check a stock", Enter routes to `/stocks/{SYM}`). Remove `TierBadge` and `LiveDot` from the header. Title `ATLAS · Swing trade ideas`.

**Interfaces:**
- `swingApi` exposes: `today()`, `stock(symbol)`, `trackRecord()`, `positions(status?)`, `createPosition(body)`, `updatePosition(id, body)`, `closePosition(id, body)`, `settings()`, `saveSettings(body)`, `telegramTest()`, `runScan()`, `status()`. It uses the existing `/api/v1` base and error style from `lib/api.ts`. Network failure throws `ApiError('unreachable', 0)`.
- `format.ts` exposes:
  - `money(x)` → `"$1,234"`
  - `price(x)` → `"148.20"`
  - `pct(x, digits=0)`
  - `r(x)` → `"+0.4R"`
  - `sessionDate(iso)` → `"Mon 19 Oct"`
  - `dateTimeET(iso)` → `"Mon 4:45pm ET"`
- `Term` exposes `<Term k="R"|"ATR"|"RS">label</Term>` with tooltips:
  - R: `"R = the amount you risk on the trade (buy price minus stop). +2R means you made twice what you risked."`
  - ATR: `"ATR = the stock's average daily price range over the last 14 days."`
  - RS: `"Relative strength: how this stock's past-year performance ranks against all US stocks in the universe (99 = strongest)."`
- `ApiDown` renders the exact unreachable copy from Global Constraints.

- [ ] **Step 1:** Implement the files above.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): swing API client, formatting, nav and stock search`.

---

### Task 10: Today page

**Files:**
- Modify: `apps/web/src/app/page.tsx` (replace)
- Create: `apps/web/src/components/RegimeBanner.tsx`, `PositionActionsCard.tsx`, `IdeasTable.tsx`, `EmptyState.tsx`

**Interfaces:**
- Consumes: `swingApi.today()`, `swingApi.runScan()`, `format.ts`, `Term`.
- Layout follows spec §7.13 "Today", top to bottom:
  1. `RegimeBanner`: regime label, breadth, and the freshness line `Data as of {Fri 16 Oct} close · ideas are for {Mon 19 Oct} · next scan {Mon 4:45pm ET}`, plus a "Run scan now" button.
  2. `PositionActionsCard`: `YOUR POSITIONS: {n} actions`, one line each, linking to `/positions`.
  3. `IdeasTable` for validated ideas. Columns: `#, Stock (symbol + name), Setup, Buy above, Stop, Shares, Risk $, Strength, Track record` (`{win%} win · {expectancy} · {n} trades`). Badges: `overlaps with {X}`, and `market not in uptrend` when `regime_warning` is true. Filters: setup type, min price. Rows link to `/stocks/{symbol}`.
  4. The same table for `unproven` under the exact heading. It's collapsible: collapsed when live ideas exist, expanded when none. Muted styling, `Unproven` tag, and the track record column shows `verdict_text`.
  5. In a downtrend: a `watch_only` table with no Shares/Risk columns.
  6. Capacity line `{open} of {max} positions open · open risk {money} of {money}`.
  7. Footer disclaimer.
- Empty states use the exact copy for each `empty_reason`. When there are no live ideas, show `No setup has passed testing yet` above the unproven section. `ApiDown` covers fetch failure.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): Today page with ranked ideas, unproven section and position actions`.

---

### Task 11: Stock page

**Files:**
- Create: `apps/web/src/app/stocks/[symbol]/page.tsx`, `apps/web/src/components/DailyChart.tsx`, `apps/web/src/components/EligibilityChecklist.tsx`

**Interfaces:**
- Consumes: `swingApi.stock(symbol)`.
- `DailyChart({bars, lines, levels})` uses lightweight-charts 5:
  - Candlestick and volume series follow the `PriceChart.tsx` setup.
  - Line series: SMA50 (`accent`), SMA200 (`warn`), EMA20 (`ink-muted`).
  - Price lines: `Buy above` (dashed `bull`), `Stop` (`bear`), `2R reference` (dotted `ink-muted`), `Trail` (when present).
- The page shows:
  - The header: symbol, name and session.
  - The chart.
  - "Why this setup" (`why`) and "What cancels it" (`cancels`).
  - `EligibilityChecklist` (✓/✗ per item with value and threshold).
  - The setup's track record line.
  - An `I bought this` button routing to `/positions?new={symbol}&idea={idea_id}`.
  - A non-qualifying stock shows the checklist with failed rules highlighted, and no idea block.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): stock page with daily chart, levels and eligibility checklist`.

---

### Task 12: Positions page

**Files:**
- Create: `apps/web/src/app/positions/page.tsx`, `apps/web/src/components/PositionForm.tsx`, `apps/web/src/components/ClosePositionForm.tsx`

**Interfaces:**
- Open table columns: `Stock, Opened, Entry, Shares, Stop, Now (R), Latest action`. "Now (R)" is `PositionOut.r_now`; "Latest action" is `last_action_text`.
- The add form is prefilled from `?new=&idea=` (stop defaults to the idea's stop). Per-row close form takes date and exit price.
- Closed table columns: `Stock, Opened, Closed, Entry, Exit, R, Held (sessions), Reason`.
- Validation errors from 422 are shown inline.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): positions page (record, follow and close trades)`.

---

### Task 13: Track record page

**Files:**
- Create: `apps/web/src/app/track-record/page.tsx`, `apps/web/src/components/SetupRecordCard.tsx`

**Interfaces:**
- Consumes: `swingApi.trackRecord()`; reuses `components/EquityCurve.tsx` if its props fit, else a small lightweight-charts line chart.
- One card per family:
  - Verdict badge: `Live`, `Research only: {first failure}` or `Not yet tested`.
  - A table with columns `In-sample · Out-of-sample · Live · Reconstructed` and rows `Trades, Win rate, Avg win, Avg loss, Expectancy, Profit factor, Avg hold`.
  - Strategy-vs-SPY equity chart.
  - Random-entry line `Random picks with the same exits: median {x}R (5–95%: {a}R to {b}R)`.
  - The version list and warnings.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): track record page (in-sample, out-of-sample, live)`.

---

### Task 14: Settings page and system status

**Files:**
- Modify: `apps/web/src/app/settings/page.tsx` (replace)
- Create: `apps/web/src/components/SystemStatusPanel.tsx`

**Interfaces:**
- The form covers every `SwingSettings` field. Percent fields are shown as %. Save sends PUT and shows 422 messages inline.
- Telegram digest toggle and a `Send test message` button.
- `SystemStatusPanel` (from `/v1/system/status`):
  - Data as of, last scan (status plus per-step ✓/✗ with error text), next scan, backfill progress bar.
  - Alpaca/Telegram configured dots.
  - `Automatic order placement: off`.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Run `cd apps/web && pnpm typecheck && pnpm lint`. Expected: no errors.
- [ ] **Step 3: Commit** with message `feat(web): settings and system status`.

---

### Task 15: One-command start script and docs

**Files:**
- Create: `scripts/atlas.sh` (executable; subcommands `up`, `down`, `status`, `help`)
- Modify: `.gitignore` (add `.atlas/`), `README.md` (the quick start becomes `scripts/atlas.sh up`; describe the four pages), `docs/runbooks/OPERATIONS.md` (nightly job, catch-up, `ATLAS_DISABLE_SCHEDULER`, `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM`, digest, kill switches), `docs/architecture/SYSTEM.md` (new routes and pages; legacy pages listed as legacy), `CHANGELOG.md` (`## 0.25.0`)
- Test: `tests/test_atlas_script.py`

**Interfaces:**
- `up` does the following, in order:
  1. Checks `docker info`. If that fails, prints `Docker isn't running. Start Docker Desktop, then run: scripts/atlas.sh up` and exits 1.
  2. Runs `docker compose -f infra/docker/docker-compose.yml up -d`.
  3. Waits for `pg_isready` in `atlas-postgres`.
  4. Runs `uv run python -m atlas_shared.migrate up`.
  5. Starts the API (`uv run --package signal-service uvicorn signal_service.main:app --port 8000`) and web (`pnpm install --frozen-lockfile && pnpm dev` in `apps/web`) in the background. Logs go to `logs/api.log` and `logs/web.log`, PIDs to `.atlas/*.pid`.
  6. Waits for `/healthz`, prints `ATLAS is up: http://localhost:3000 (first run downloads data in the background; progress is on the Today page)`, and runs `open` on the URL on macOS.
- `down` kills the PIDs and runs `docker compose stop`.
- `status` prints docker/postgres/api/web state and the `/v1/system/status` summary.

- [ ] **Step 1: Write the failing test:**

```python
def test_atlas_script_parses_and_prints_usage():
    assert subprocess.run(["bash", "-n", "scripts/atlas.sh"]).returncode == 0
    out = subprocess.run(["bash", "scripts/atlas.sh", "help"], capture_output=True, text=True)
    assert out.returncode == 0 and all(w in out.stdout for w in ("up", "down", "status"))
```

- [ ] **Step 2:** Run `uv run pytest tests/test_atlas_script.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the script and docs.
- [ ] **Step 4:** Run `uv run pytest tests/test_atlas_script.py tests/test_docs.py tests/test_cli_entrypoints.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `feat: one-command start (scripts/atlas.sh) and product docs`.

---

### Task 16: End-to-end verification in the browser

No new code unless a defect is found. Fix any defects in the owning task's files with a regression test.

- [ ] **Step 1:** Run `scripts/atlas.sh up`. Expected: the "ATLAS is up" line. Run `scripts/atlas.sh status`. Expected: API and web up, scheduler running.
- [ ] **Step 2:** Open the preview (`preview_start` with name `atlas-web`) at `/`.
  - Expected: either the first-run progress copy, or (if Phase 1's backfill already ran) a populated Today page with the regime banner, the freshness line naming the right sessions, and validated or unproven sections per the verdicts.
  - Take a screenshot.
- [ ] **Step 3:** Click the top idea. Expected: chart with Buy above, Stop and 2R lines, why/cancels text, and a checklist with all ✓. Search a non-qualifying ticker (for example `SPY`). Expected: checklist with `Common stock (not an ETF or fund)` ✗ and no idea block.
- [ ] **Step 4:** On Positions, add a position from an idea, then trigger `POST /v1/scan/run`. Expected: the position shows a latest action. Close it. Expected: the R value is shown in the closed table.
- [ ] **Step 5:** On Settings, set risk to 0. Expected: an inline validation error. Restore it. `Send test message` returns ok when Telegram is configured.
- [ ] **Step 6:** Stop the API (`kill $(cat .atlas/api.pid)`) and reload Today. Expected: the exact `Can't reach the ATLAS API...` copy. Restart with `scripts/atlas.sh up`.
- [ ] **Step 7:** Resize to the mobile preset. Expected: Today is readable with no horizontal page scroll (tables scroll inside their card). Reset to desktop.
- [ ] **Step 8:** Run `uv run pytest -q && (cd apps/web && pnpm typecheck && pnpm lint)`. Expected: all green. Report the screenshots and results to the user.
