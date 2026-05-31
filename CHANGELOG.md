# Changelog

Chronological record of what shipped across the build. Newest first.
Every entry maps to a turn in the build log; phase numbers reference
`docs/architecture/BLUEPRINT.md` §18.

## 0.18.2 — Multi-resolution candle aggregation + chart timeframe selector

The chart was showing ~5 hours of data because the `PriceChart` component
hardcoded `limit=300` at `resolution='1m'` and the backend only stored 1m
bars. Even maxing `limit=5000` only got you ~12.8 trading days, and 11,700
1-minute candles is visually unusable anyway.

### Backend — on-the-fly aggregation off the 1m storage

- `ingest_equities.store.latest_bars()` now accepts `1m | 5m | 15m | 30m |
  1h | 4h | 1d | 1w`. For `1m` it reads raw rows (unchanged). For everything
  else it aggregates via TimescaleDB `time_bucket()` + `first()`/`last()`
  aggregates — `first(open, ts)`, `max(high)`, `min(low)`, `last(close, ts)`,
  volume-weighted `vwap`, `sum(trade_count)`. No new tables, no new ingest
  pipelines — one source of truth, zoom out at read time.
- `GET /v1/symbols/{symbol}/bars` validates the `resolution` parameter via
  the new `is_supported_resolution()` helper and returns 400 on unknown
  values instead of silently returning empty.
- `tests/test_bars_resolution.py` — 6 tests pinning the cross-stack contract
  (frontend timeframes ↔ backend buckets), the GROUP BY shape, and the
  OHLC-aggregate correctness clues (first(open), last(close), max(high),
  min(low), volume-weighted vwap).

### Frontend — timeframe pill selector

- `PriceChart.tsx` gained a pill row with **1D / 5D / 1M / 3M / 6M / 1Y**.
  Each pill maps to a `(resolution, limit)` pair tuned for ~80–260 visible
  candles — dense enough to see structure, sparse enough that each candle
  reads cleanly. `refetchInterval` scales with the timeframe (30s for 1D,
  5min for 6M/1Y).
- Prop API changed: `resolution?: string` → `initialTimeframe?: TimeframeId`.
  The single existing caller (`apps/web/src/app/symbols/[symbol]/page.tsx`)
  doesn't pass either prop, so this is a clean break.
- `pnpm typecheck` clean.

### Why on-the-fly aggregation, not separate ingest passes per resolution

We could have run `ingest_equities backfill --resolution 1d --days 365` etc.
and stored each TF as its own rows. But that means N copies of the ingest
pipeline, N opportunities for resolution drift, and N× the storage. The
TimescaleDB hypertable + `time_bucket()` path is fast enough for the chart
read pattern (~30 rows after aggregation), and when 30-day reads start
hurting (BLUEPRINT §6.2 ledger item) we promote the aggregations to
**continuous aggregates** (materialised views Timescale keeps incrementally
fresh) — same SQL, no app-code change.

Test count **217 → 221**.

## 0.18.1 — NewsAPI source: spec audit + 3 bugs fixed

Audit of `apps/news-ingest/src/news_ingest/sources/newsapi.py` against
[newsapi.org/docs/endpoints/everything](https://newsapi.org/docs/endpoints/everything)
turned up three real bugs and one best-practice gap. All are fixed with
regression tests pinning the contract.

- 🔴 **URL was wrong.** The previous implementation set
  `httpx.AsyncClient(base_url="https://newsapi.org/v2/everything")` then called
  `client.get("/everything")`. httpx treats leading-slash relative URLs as
  *replacing* the base URL's path, so the actual request went to
  `https://newsapi.org/everything` (no `/v2/`). Every NewsAPI call was hitting
  a 404; only the `raise_for_status` masked it as a generic HTTP error. Fixed
  by passing the full URL on every call and removing `base_url` entirely.
- 🔴 **JSON-body errors were silently swallowed.** NewsAPI returns
  **HTTP 200** with `{"status":"error","code":"...","message":"..."}` for
  quota / invalid-key / param errors. The previous code only checked
  `r.raise_for_status()`. Now the payload's `status` field is explicitly
  inspected; a non-`ok` response raises typed `NewsApiError(code, message)`.
- 🟡 **Tenacity was retrying app-layer errors.** Invalid API key would retry
  3× before surfacing — useless and slow. The retry decorator now uses
  `retry_if_not_exception_type(NewsApiError)` so only transient httpx errors
  (network, 5xx) get backed off; app-layer errors propagate immediately.
- 🟡 **Auth moved to `X-Api-Key` header.** Previously sent as `apiKey` query
  param — works, but the key leaked into URL access logs. Header form is the
  documented best practice and matches the Alpaca-style auth pattern used
  elsewhere in the codebase.
- 🟢 **Added pagination.** `since` requests for gaps > 100 articles silently
  dropped data. Now walks `page=1..max_pages` (default 5 → up to 500
  articles/fetch) until either `totalResults` is consumed, a short page
  arrives, or the cap is hit. The `maximumResultsReached` code (dev-plan
  cap) is treated as a benign termination, not an error.
- 🟢 Stripped microseconds from the `from` parameter; added `searchIn=title,description`
  default for cleaner finance results; propagated `urlToImage`, `source_id`,
  and `author` into `RawItem.metadata` (was author-only).
- 🆕 `tests/test_newsapi_source.py` — 8 tests, all using `httpx.MockTransport`
  so they run offline: URL targeting, header auth, JSON-body-error handling,
  no-key graceful skip, article→RawItem mapping, pagination walk, max-pages
  cap, microsecond stripping.

Test count **209 → 217**.

## 0.18.0 — Active-set weight renormalisation (unblocks bars-only signal generation)

The "every signal returns null with null veto" symptom on fresh installs was
**a math bug, not a data bug**. The blueprint default weights allocate 0.30
of the composite to `fund` (0.15), `opt` (0.10), and `chain` (0.05) — three
sub-scores whose adapters are deferred (no `fund` or `chain` ingester exists;
the signal route never passes `options_features`). Achievable composite on
the production route was capped at:

  - Best case  (every wired sub-score at ±100) →  0.70 × 100 = ±70
  - Bars-only  (only tech+quant non-zero)      →  0.45 × 100 = ±45  ❌ can never clear ≥50

Result: even a perfect bullish setup with 180 days of healthy data on AAPL /
MSFT / TSLA was mathematically unable to publish a signal.

### Fix

- `scoring_engine.composite.composite()` accepts an `active: Iterable[str] | None`
  set. When provided, weights are **renormalised over the active set** so
  deferred sub-scores contribute nothing AND don't dilute the total. Default
  call shape (no `active`) is unchanged — preserves blueprint-default math for
  the backtest harness and existing tests.
- `scoring_engine.signal.generate_signal()` now **builds the active set
  dynamically**: tech/quant/liq are always on; macro/sent/opt join when their
  feature dicts are supplied by the caller; fund/chain are excluded until
  they're built. `_engines_agree()` was updated to iterate the active set,
  not a hard-coded tuple — so the "≥ 2 confirming engines" gate stops
  counting deferred zeros against the signal.
- Gate thresholds (`MIN_COMPOSITE`, `MIN_CONFIDENCE`, `MIN_CONFIRMING_ENGINES`,
  `MIN_AGREE_THRESHOLD`) are now env-overridable via `ATLAS_MIN_*` for
  sparse-data deployments. Defaults unchanged.
- New `tests/test_active_weight_renorm.py` (5 tests) pins the contract:
  default-call behavior unchanged, active-set renormalisation correct, active
  set excludes `fund`/`chain` and grows when features are supplied, and an
  end-to-end "bars-only with strong tech+quant" signal now fires.

Test count **202 → 209**. No regressions.

## 0.17.0 — DeepSeek LLM swap + Alpaca ingest adapter + env catalog

Three orthogonal changes the user asked for in one pass; each is independently
revertible.

- **LLM provider: Anthropic → DeepSeek.** `explanation-engine` now talks to
  `https://api.deepseek.com` via the `openai` SDK (DeepSeek is wire-compatible
  with OpenAI chat/completions). Server-side context caching kicks in
  automatically — we drop the explicit `cache_control` blocks but still get
  ~10× input-cost savings on repeated system prompts; usage logs now record
  `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` so the savings curve
  is observable. Env: `DEEPSEEK_API_KEY` (was `ANTHROPIC_API_KEY`),
  `ATLAS_EXPLAIN_MODEL=deepseek-chat` default (or `deepseek-reasoner` for R1).
  Templated fallback path unchanged — pipeline still survives no-key mode.
  `pyproject.toml`: `anthropic` dep replaced by `openai>=1.40`. Test +
  OPERATIONS + SYSTEM docs synced.
- **Ingest source: Alpaca added alongside Polygon.** New
  `apps/ingest-equities/src/ingest_equities/alpaca.py` mirrors
  `PolygonClient`'s public surface (same dataframe shape, same column names),
  so `store.upsert_bars` accepts both with no branching. CLI gains
  `--source polygon|alpaca|auto` (default `auto` — picks polygon if its key
  is set, else alpaca). Alpaca's free IEX feed works the moment trading keys
  exist; flip `ALPACA_FEED=sip` on a paid plan for the consolidated tape.
  Polygon adapter kept (not deleted) so existing Polygon-paying users aren't
  forced to switch.
- **`.env.example` is now the authoritative env catalog.** Was 4 lines of
  Polygon/Alpaca placeholders; now mirrors every row of OPERATIONS.md's
  "Going live (real-data) toggles" table — auth (`ATLAS_AUTH_MODE`,
  `ATLAS_JWKS_URL`, …), market data (Polygon + Alpaca + FRED + NewsAPI), LLM
  (DeepSeek), and every alert channel (webhook, Telegram, email) — grouped
  with inline how-to-get-the-credentials notes (e.g. the @BotFather +
  getUpdates flow for Telegram). The original prompt — "where do I write the
  telegram_bot_token in?" — is now answerable by `cat .env.example`.

## 0.16.1 — Production-readiness fixes (caught while running it for real)

Three small bugs surfaced the moment the system left synthetic tests and hit
real users / a real DB. Each is fixed with a regression guard so they can't
come back.

- **CLI shim** — `python -m ingest_equities` failed with `No module named
  ingest_equities.__main__` because the package had no `__main__.py` (the
  other CLIs use submodule paths). Added the shim. New
  [`tests/test_cli_entrypoints.py`](tests/test_cli_entrypoints.py) scans the
  README + OPERATIONS docs for every `python -m <module>` and asserts it's
  importable with a working entrypoint — catches this class of bug for every
  CLI, every release.
- **Ingest source-shape contract** — synthetic GBM bars omit `vwap`/
  `trade_count`; Polygon bars include them. The upsert SQL bound all 10
  fields, blowing up with
  `InvalidRequestError: A value is required for bind parameter 'vwap'`.
  `upsert_bars` now normalises every row through a `_BAR_FIELDS` tuple,
  defaulting missing optionals to `None`. New
  [`tests/test_ingest_store.py`](tests/test_ingest_store.py) asserts every
  `:name` in the upsert SQL exists in `_BAR_FIELDS`, and round-trips the
  synthetic generator through the normaliser.
- **NUMERIC → JSON-number coercion** — Postgres `NUMERIC` columns came
  through as `Decimal`; FastAPI's default encoder serializes them as
  *strings* (preserves precision), so the journal page broke at
  `e.entry_price.toFixed is not a function`. New
  [`atlas_shared.to_jsonable`](packages/shared-py/src/atlas_shared/jsonable.py)
  walks rows/nested-dicts/lists and casts `Decimal → float`. Applied in
  `journal_service.list_entries` and `execution_service.list_orders` (the
  Orders panel would have hit the same bug next). New
  [`tests/test_jsonable.py`](tests/test_jsonable.py) covers top-level /
  nested / list / passthrough / `json.dumps`-round-trip cases.
- **Test-suite robustness** — the two "watchlist 503-without-DB" tests now
  check connectivity via a socket probe and **skip** when local Postgres is
  reachable, instead of failing. A complementary `..._returns_200_in_any_state`
  test runs regardless.

Test count **195 → 202**.

## 0.16.0 — `s_opt` options sub-score (final §8 sub-engine)

- New `apps/options-analytics` package: Black-Scholes price/gamma/delta + bisection IV
  solver; `OptionChain` types + a deterministic `synthetic_chain` for offline dev;
  chain analytics — put/call ratio (OI + volume), IV rank, **max pain**, **dealer GEX**
  + gamma-flip strike; `s_options()` sub-score blending put/call skew, UOA, GEX, IV
  rank, max-pain pull.
- `scoring-engine.generate_signal` now takes `options_features` and emits the `opt`
  sub-score (was hardcoded 0).
- `GET /v1/symbols/{symbol}/options` — analytics + s_opt, DB-resilient, labelled
  `synthetic: true`. The pipeline does **not** auto-inject synthetic options into live
  signals — `s_opt` activates only when a real feed supplies `options_features`.
- +13 tests (put-call parity, gamma positivity, IV round-trip, chain determinism,
  put-skew/OI, max-pain bounds, GEX finiteness, s_opt bull/bear/clamp).

## 0.15.0 — Direction-correct realized PnL

- Extracted `realized_pnl(direction, avg_cost, exit_price, qty)` — a pure helper.
- `reduce_position` reads `direction` from the position metadata and applies the
  correct sign; `ClosedLot` carries `direction`.
- Engine's journal write now uses `lot.direction` instead of a hardcoded `"long"`.
- +6 PnL tests (long↔short mirror, sign on rise/fall, unknown direction defaults long).

## 0.14.0 — Partial ladder exits + chandelier trailing stop

- New `ladder.py`: pure `plan_exit(meta, last_price, open_qty, now)` decides the next
  action — high-water-mark ratchet → break-even after T1 → chandelier trailing stop
  (`hwm − 3×ATR`) once >1R → stop/trail/time/target. Returns the updated trailing
  metadata to persist.
- Portfolio store: `reduce_position` (partial-aware close — splits the lot), `get_open`,
  `update_position_meta`; `add_position(monitor=…)` accepts the full ladder metadata.
- Engine close honors `quantity` (partial via `reduce_position`); engine open builds
  the ladder metadata (entry/stop_init/atr/targets/allocations/initial_qty/hwm/trail_mult).
- Monitor: persists trailing state every tick (so the trail keeps climbing); applies
  partial/full closes via the engine.
- Frontend `SignalCard` sends the full target list + ATR (≈ initial risk / 1.8).
- +11 ladder tests (in-band hwm ratchet, T1/T2 partials, final-rung close, break-even
  floor, chandelier trail-up, trail-triggered exit, hard stop, time stop, short partial,
  short stop).

## 0.13.0 — Prometheus + Grafana infra

- `infra/observability/`: docker-compose for Prometheus (`:9090`) + Grafana (`:3001`),
  prebuilt **ATLAS — Engine Overview** dashboard (8 panels: signal outcomes, reject
  ratio gauge, WS gauge, pipeline P50/P95 by stage, HTTP rate + P95 by route, alerts by
  channel, orders + monitor exits), 5 alert rules (high reject rate, pipeline latency,
  alert delivery failures, order rejections, service-down).
- Drift guard: `test_observability_infra.py` asserts every `atlas_*` metric referenced
  in the dashboard and alert rules is actually defined in `atlas_shared.metrics`.

## 0.12.0 — Observability (metrics)

- `atlas_shared.metrics` — single source of metric definitions on the default registry:
  `atlas_signals_total{result}`, `atlas_alerts_fired_total{channel,ok}`,
  `atlas_orders_total{intent,status}`, `atlas_monitor_exits_total{reason}`,
  `atlas_pipeline_seconds{stage}`, `atlas_http_requests_total{method,path,status}`,
  `atlas_http_request_seconds`, `atlas_ws_connections`. `time_stage()` context manager
  and `render()` for `/metrics`.
- HTTP middleware records count/latency per **route template** (not raw path).
- Instrumentation woven through pipeline / stream / alert engine / execution engine /
  monitor.

## 0.11.0 — Position monitor (single-target/time)

- Background `_monitor_loop` (10s cadence) auto-exits open positions on stop/target/
  time via the execution engine (later expanded to the ladder in 0.14).
- +11 monitor tests including `decide_exit` decision matrix.

## 0.10.0 — Broker execution (paper / Alpaca)

- New `apps/execution-service`: `PaperBroker` (always-available, fills at limit or
  reference price); `AlpacaBroker` (paper REST, enabled only with keys); broker
  registry falls back to paper. `ExecutionEngine.execute(open|close)` records orders,
  opens/closes positions, writes journal on close.
- Migration `0008_orders.sql` (orders lifecycle table); routes `POST /v1/execute`,
  `GET /v1/orders` (tier-gated on `broker_autotrade`).
- Frontend: SignalCard **Paper Buy** button (tier-gated), per-holding **Close** on
  portfolio, `OrdersPanel` history.

## 0.9.0 — Per-user data scoping

- Migration `0007_user_scoping.sql`: adds `user_id` to watchlists, alert_rules,
  portfolios; backfills seeds to `'dashboard'`.
- Stores keyed by `user_id`; alert broadcaster uses `list_all_rules()` across users.
- +4 scoping tests including signature guards (`user_id` in store sigs).

## 0.8.0 — Auth + tiers (Clerk/Auth0-compatible)

- `atlas_shared.entitlements` — §17.1 tier matrix (free→enterprise).
- `atlas_shared.auth` — JWT (PyJWT + JWKS) for prod, dev-mode `X-Dev-User`/`X-Dev-Tier`
  headers for offline. `AuthContext` carries identity + tier → entitlements.
- `current_user` dependency, `/v1/me`, watchlist size cap + alert channel/count caps
  enforced server-side. Frontend `TierBadge` + `TierSwitcher`.

## 0.7.0 — Settings + persisted watchlist + alert rules CRUD

- `0005_watchlist.sql`, `0006_alerts.sql`. Watchlist read falls back to default list
  when DB is down. Alert engine ships with `log`/`webhook` (HMAC SHA-256, §6.5)/
  `telegram`/`email` channels — all fail-soft on missing creds. Frontend `/settings`
  with chip-style watchlist editor, alert rule form, deliveries feed.

## 0.6.0 — WebSocket live push

- `/v1/stream` — connection manager with `signals.*` wildcard subjects; single
  broadcaster recomputes regime + signals on a cadence and fans out; alert engine
  fires from the same recompute. Frontend `StreamProvider` writes WS messages
  straight into the React Query cache; `LiveDot` shows status.

## 0.5.0 — Portfolio + Journal + Backtest UIs

- `apps/portfolio-service` (holdings + sector + parametric VaR), `apps/journal-service`
  (auto-log closed trades from backtest fills + attribution), `backtest-service`
  exposed via `/v1/backtests`. Frontend pages for each, plus candlestick chart
  (lightweight-charts v5) with entry/stop/T1–T3 price lines on the symbol page.

## 0.4.0 — Macro, sentiment, news

- `apps/macro-engine` (FRED + KMeans regime), `apps/sentiment-engine` (lexicon +
  optional FinBERT), `apps/news-ingest` (RSS / NewsAPI / file replay → scored items).
  `s_macro` + `s_sent` wired into the composite.

## 0.3.0 — Backtest harness

- `apps/backtest-service`: event-driven simulator, walk-forward purged CV (cost-aware
  metrics, block-bootstrap Sharpe CI, deflated Sharpe), cost-sensitivity sweep.
  `AtlasStrategy` (production pipeline replay) + `TrendFollower` demo strategy.

## 0.2.0 — Risk engine + LLM rationale

- Sizing (Kelly + vol-target + ATR-risk + correlation cap + per-asset cap), VaR/CVaR,
  veto reasons. Explanation writer with prompt-cached Anthropic system prompt, templated
  fallback when no API key.

## 0.1.0 — Phase 1 vertical slice

- Phase 0 monorepo (uv workspace, two ADRs pushing back on premature Rust + the 24-service
  scale-up).
- Phase 1: TimescaleDB schema; ingest CLI (Polygon + synthetic); feature-engine (25
  indicators); XGBoost trend model with triple-barrier labels + walk-forward purged CV
  + isotonic calibration; scoring engine (`s_tech` faithful to §8.4); `/v1/signals/{symbol}`,
  `/v1/scan`, `/v1/signals/{symbol}/debug`.

---

**Provenance:** every entry corresponds to a turn in the build conversation; CHANGELOG
serves as the index. ADRs in `docs/adr/` record the *why* behind irreversible decisions
(language choice, Phase-0 scope deferrals).
