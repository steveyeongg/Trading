# Changelog

Chronological record of what shipped across the build. Newest first.
Every entry maps to a turn in the build log; phase numbers reference
`docs/architecture/BLUEPRINT.md` §18.

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
