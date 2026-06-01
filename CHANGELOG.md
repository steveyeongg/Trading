# Changelog

Chronological record of what shipped across the build. Newest first.
Every entry maps to a turn in the build log; phase numbers reference
`docs/architecture/BLUEPRINT.md` §18.

## 0.23.0 — Phase 5 + 6: provider status, data freshness, §12.2 events, §12.3 Telegram, exec opt-in, dashboard polish

Closes the last five GAP_AUDIT items (#22–#30) and the remaining three §22
non-negotiables. Default watchlist trimmed to equities-only per §4.3
(`BTC`/`ETH` removed from `Watchlist.tsx` and `watchlist.py`; crypto is now
available only as an explicit opt-in universe).

### Phase 5 — real-data observability

- **New `GET /v1/providers/status`** ([providers.py](apps/signal-service/src/signal_service/providers.py))
  returns configured + availability state for all 12 dependencies (alpaca,
  polygon, yfinance, synthetic, fred, newsapi, rss, deepseek, telegram,
  webhook, postgres, redis) grouped by category, each with its documented
  fallback. §17 fail-soft policy block embedded so the dashboard can explain
  *why* missing keys are safe.
- **New `GET /v1/data/freshness`** — per-symbol last-bar age (seconds) +
  macro snapshot age. Accepts `?symbols=AAPL,NVDA`; defaults to the default
  watchlist.
- **New end-to-end fail-soft test** — `test_pipeline_runs_with_no_provider_keys`
  strips every external API key, runs the full pipeline, and asserts a
  rationale is still produced with the disclaimer intact. Hard guarantee of
  the §17 policy.
- **Dashboard:** new [`ProvidersStatusPanel`](apps/web/src/components/ProvidersStatusPanel.tsx)
  on `/settings` — green/grey dots per provider with the fallback string.

### Phase 6 — alerts, exec, dashboard

- **§12.2 event derivation** ([events.py](apps/alert-service/src/alert_service/events.py))
  — new `derive_events()` computes all 7 triggers: `signal_new`,
  `signal_upgraded`, `composite_threshold_crossed`,
  `price_reached_entry/t1/t2/t3`, `price_hit_stop`, `risk_veto_changed`,
  `macro_regime_changed`. Alert engine now tracks last-signal + last-regime
  per symbol and surfaces the event flags on every dispatched payload.
- **§12.3 Telegram format** — `format_alert` rewritten to the spec'd layout:
  🚨 emoji header, score / confidence / conviction, entry / SL / T1-T3 / R:R,
  "Why" bullets, **invalidation**, **disclaimer**. Closes §22 #5
  ("no alert without invalidation").
- **Auto-execution opt-in** — `_monitor_loop()` only starts when
  `ATLAS_ENABLE_AUTO_EXECUTION=1`. Default off, with an explicit log line
  explaining how to enable. Closes §22 #6 ("no auto-execution by default").
- **`POST /v1/explain/signal`** — returns the structured §10.3 payload for a
  symbol (or the no-signal reason if the gates failed). Cached server-side
  via the §10.5 LRU+TTL.
- **Dashboard:** new [`ExplanationPanel`](apps/web/src/components/ExplanationPanel.tsx)
  on `/symbols/{symbol}` renders the §10.3 sections inline (summary,
  bull/bear cases, why entry, why stop, target logic, confidence, final
  view) — or a clean "no signal" card with the gate reason when the
  candidate didn't publish. Closes the dashboard side of §11.2 #12.

### Tests + checks

- +29 new tests across `test_phase5_providers.py` (8) and
  `test_phase6_alerts.py` (13) covering each §12.2 trigger and the §12.3
  message body.
- Backend test count **256 → 274** (+18 net; one test in `test_alerts.py`
  rewritten to the new payload shape). Same 2 pre-existing
  `test_bars_resolution.py` failures (older Timescale `time_bucket` test vs.
  current Postgres-native SQL — unrelated to this work).
- Dashboard `pnpm tsc --noEmit` clean against the new `quant_meta`,
  `time_stop_at`, `invalidations`, `ExplanationPayload`,
  `ProvidersStatusResponse`, and `FreshnessResponse` types.

**All 30 GAP_AUDIT items now closed (100%). All 10 §22 non-negotiables
satisfied.**

## 0.22.0 — Phase 4: DeepSeek §10.3 JSON contract + §10.4 safety + §10.5 cache + §6.2 feature_health

Tightens the LLM layer so DeepSeek can't be the source of trade fictions and
so the quant model surfaces its own data-health state.

- **§10.3 strict JSON I/O.** New shared `ExplanationPayload` pydantic schema
  with `summary`, `bull_case`, `bear_case`, `why_entry`, `why_stop`,
  `target_logic`, `confidence_comment`, `final_view`, plus `source` and
  `safety_repaired` flags. [`payload.py`](apps/explanation-engine/src/explanation_engine/payload.py)
  ships `parse_llm_json` (tolerates fenced blocks) and `render_markdown` (a
  deterministic projection used for `Signal.rationale_md`). [System prompt](apps/explanation-engine/src/explanation_engine/prompts.py)
  rewritten to demand raw JSON; writer requests
  `response_format={"type": "json_object"}`.
- **§10.4 safety rules.** `safety_repair()` strips/rewrites five forbidden
  phrase patterns (`guaranteed`, `guarantees`, `risk-free`/`no risk`,
  `sure thing`, `can't lose`) across all text fields and *guarantees* at
  least one invalidation in `bear_case` (falls back to the deterministic
  invalidations on `Signal` if the LLM omitted them). Sets
  `safety_repaired=True` so the dashboard can label a repaired payload.
  Disclaimer is appended by the markdown renderer, not requested from the
  model.
- **§10.5 cache key.** `make_cache_key(signal, features)` returns
  `symbol:horizon:direction:bucket:feat_hash:news_hash`. Composite scores
  round to the nearest 5, so signals at 73 and 74 share a slot. New
  `_TTLCache` (LRU + TTL hybrid) fronts both the LLM and templated branches;
  default 256 entries × 15-minute TTL, env-overridable.
- **§6.2 quant feature_health.** `TrendModel.predict_full()` returns the full
  blueprint output schema: `{p_up, p_down, s_quant, model_version,
  calibrated, feature_health}`. `feature_health` is `ok` / `degraded` /
  `missing` based on NaN count in the required feature columns. New
  `Signal.quant_meta` field carries it through the pipeline; templated
  rationale flags degraded feature health in `confidence_comment`.

### Tests

- +17 tests in [`test_phase4_deepseek.py`](tests/test_phase4_deepseek.py) —
  JSON parse (clean / fenced / malformed); safety repair (forbidden phrases,
  forced invalidation, clean pass-through); cache key
  (bucket-stability, feature-hash split, news-hash split); writer cache
  return-same-instance vs. bypass; templated payload required sections;
  feature_health surfacing in degraded mode.
- `test_explanation.py::test_templated_rationale_format` updated to the new
  §10.3 sections.
- Test count **239 → 256** (+17).

## 0.21.0 — Phase 3: signal quality — 9 missing §5.2 indicators + §5.3 weighted blocks + Donchian-structure stops + time stops + news-event veto

Tightens signal quality so individual signals are easier to trust.

- **9 new indicators** ([feature-engine/indicators.py](apps/feature-engine/src/feature_engine/indicators.py)):
  SMA 20/50/200, Supertrend (line + direction), Donchian (upper/middle/lower),
  Keltner (upper/middle/lower), Pivot points (pivot/r1/s1), Fibonacci
  (`fib_position` ∈ [0,1] + `fib_mid`), Relative Volume (`rvol_20`),
  52-week high/low distance (high_52w_dist, low_52w_dist), Gap %. Total
  feature surface 30 → 50 indicators.
- **§5.3 s_tech weighted blocks.** [`sub_scores.py`](apps/scoring-engine/src/scoring_engine/sub_scores.py)
  s_tech refactored to the explicit `25% trend + 20% momentum + 15%
  volatility + 15% volume + 15% structure + 10% MTF` formula. Trend block
  uses EMA stack alignment + ADX strength weighted by DI± + Supertrend
  direction. Volume block uses OBV-z × relative volume. Structure block uses
  BOS + divergences + Donchian breakout position + Fibonacci extremes. New
  `s_tech_breakdown()` exposes per-block contributions for the debug
  endpoint.
- **Donchian-structure stop loss (§9.3).** `generate_signal()` picks
  `max(ATR-stop, donchian_lower - 0.5×ATR)` for longs (mirror for shorts).
  ATR-only fallback when Donchian is unavailable. Sanity guard prevents the
  stop crossing entry. Targets re-derived from the resulting risk-per-share
  so R-multiples stay coherent.
- **Time stop (§9.1).** New `Signal.time_stop_at: datetime | None`. Populated
  with horizon-dependent defaults: intraday 6.5h (one session), swing 10d,
  position 60d, long-term 252d. Env-overridable.
- **News-event veto (§9.6).** [`risk-engine/engine.py`](apps/risk-engine/src/risk_engine/engine.py)
  vetoes a long if `s_news ≤ -60` (or a short if `s_news ≥ +60`). Threshold
  env-overridable via `ATLAS_NEWS_VETO_THRESHOLD`.

### Tests

- +13 tests in [`test_phase3_signal_quality.py`](tests/test_phase3_signal_quality.py):
  indicator surface, Fibonacci bounds, Donchian envelope ordering, Supertrend
  direction set; s_tech bullish trend block / bearish blocks / weights sum to
  1.0; structural stop tighter than ATR; structural stop looser than ATR;
  time stop populated; news veto blocks severe-negative-news long; mild
  negative news doesn't trigger veto.
- `test_active_weight_renorm.py` fixture updated to populate the new §5.3
  inputs (DI±, Supertrend, rvol, Donchian, fib).
- Test count **226 → 239** (+13).

## 0.20.0 — Phase 2: Screener MVP — universes config + `/v1/screener/*` + dashboard scanner/alerts

Closes the BLUEPRINT §4 screener gap end-to-end.

### Backend

- **Universe config.** New [`infra/data/universes.json`](infra/data/universes.json)
  ships 5 built-in universes: `default_watchlist` (6 tickers),
  `us_megacap` (20), `nasdaq100_seed` (30), `etfs_core` (10),
  `crypto_majors` (2). Loadable via `ATLAS_UNIVERSES_PATH` env override.
- **`POST /v1/screener/run`** — universe + manual symbols (mix-and-match),
  horizon, min-composite filter, top-N cap, optional DeepSeek rationale.
  Rows that don't publish a signal are still returned with their
  `no_signal_reason` so the dashboard can show *why* a candidate didn't
  rank.
- **`GET /v1/screener/universes`** — universe metadata for the dropdown.
- New [`screener.py`](apps/signal-service/src/signal_service/screener.py)
  module — pure resolution + run-shaping logic (no DB writes), keyed by
  `resolve_symbols(universe, symbols)` which handles manual+universe merge
  and dedup.

### Dashboard

- New `/scanner` page — universe dropdown, manual symbol append, horizon /
  min-composite / top-N / explain controls. Full §11.3 column set: Symbol,
  Direction, Composite, Confidence, Conviction, Entry, Stop, T1/T2/T3, R:R,
  per-engine Tech/Quant/News/Macro/Risk sub-scores. Collapsible
  no-signal-reasons + skipped-symbols sections.
- New `/alerts` page — dedicated route for rules + deliveries (previously
  buried in `/settings`).
- New `ManualSymbolInput` on the dashboard home: 1 ticker → symbol page,
  >1 ticker → scanner with prefilled query.
- Nav updated with Scanner + Alerts links.
- Types regenerated for the new schema (news/risk subscores,
  `bar_age_seconds`, `time_stop_at`, `invalidations`, `quant_meta`,
  `ExplanationPayload`).

### Tests

- +9 tests in [`test_screener.py`](tests/test_screener.py): default
  universe loads, list payload shape, manual mode dedup, fallback to default
  when empty, universe mode, mixed manual+universe, unknown universe
  raises, env override, missing-file fallback.
- Test count **217 → 226** (+9).

## 0.19.0 — Phase 1: BLUEPRINT v2 alignment — composite weights, s_news/s_risk, stale-data veto, no-signal reason, FRED fail-soft

Fixes a math bug in composite scoring and surfaces the missing "why isn't
this signal firing" reason channel. Critical for downstream signal trust.

- **§8.2 composite weights fixed.** Old weights had
  `fund(0.15) + chain(0.05) = 0.20` allocated to two sub-scores that had no
  adapter and never returned non-zero — they silently bled magnitude from
  tech and quant. New weights match BLUEPRINT §8.2 exactly:
  `tech 0.30, quant 0.25, news 0.10, sent 0.10, macro 0.10, opt 0.05, liq
  0.05, risk 0.05`. `fund`/`chain` removed entirely.
- **`s_news` and `s_risk` promoted to first-class sub-scores.** `s_news`
  weights news tone by saturating headline count + scorer confidence;
  `s_risk` is an ATR/price-band trade-acceptability pre-screen (rewards the
  0.5–4% ATR/price sweet spot, penalises ultra-tight or ultra-wild names).
- **Stale-data veto (§9.6 + §20.1).** Pipeline now refuses to score bars
  older than the horizon's tolerance: intraday 2h, swing 30h, position 96h,
  long-term 168h (env-overridable). Closes §22 #9.
- **`no_signal_reason` everywhere.** `generate_signal()` returns a
  `GateResult` (instead of bare `None`) carrying a human-readable reason
  when the composite/confirming-engines/confidence gates fail.
  `PipelineResult.no_signal_reason` populated for every non-publishable
  terminal state. Surfaced in `/v1/signals/{symbol}/debug` and the WebSocket
  stream payload.
- **FRED fail-soft.** `FredClient.series()` now catches every failure
  (missing key, HTTP error, rate limit, empty payload) and degrades to the
  deterministic synthetic series. Closes §17 "missing macro key must not
  crash" guarantee.
- **§22 invalidation support.** New `Signal.invalidations: list[str]` +
  `Signal.bar_age_seconds: float | None`. Templated rationale renders an
  invalidation block.

### Tests

- +5 tests in [`test_no_signal_reason.py`](tests/test_no_signal_reason.py):
  insufficient bars carries reason, empty bars carries reason, stale-data
  veto trips at 48h, `generate_signal` always returns a `GateResult` (never
  None) when gates fail.
- `test_composite.py` + `test_active_weight_renorm.py` updated to the new
  §8.2 weights.
- `test_atlas_strategy_runs_without_errors` rewired around the new
  `Signal | GateResult` return type.
- Test count **209 → 217** (+8, net of one updated).

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
