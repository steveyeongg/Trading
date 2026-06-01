# ATLAS — System map

Single-page tour of what's built, how it fits together, and the things that
were intentionally left out. Complements `BLUEPRINT.md` (the design target)
with the actual delivered shape.

> **Status:** BLUEPRINT v2 fully aligned. All 30 GAP_AUDIT items closed.
> All 10 §22 non-negotiables satisfied. 274 backend tests passing,
> dashboard typecheck clean.

## Lifecycle, end-to-end

```
   Manual symbols / Screener universe (BLUEPRINT §4.2 / §4.3)
                       │
                       ▼
   ingest_equities ─► TimescaleDB ─► feature_engine (50 indicators §5.2) ─┐
   (Polygon → Alpaca → yfinance → synthetic GBM, §4.4 fail-soft chain)    │
                                                                          │
   macro_engine.refresh ─► Redis snapshot ──► s_macro                     │
   (FRED → synthetic fallback on any error)                               │
                                                                          │
   news_ingest ─► news_items / news_scores ─► s_news + s_sent             │
   (RSS / NewsAPI / file replay)                                          │
                                                                          ▼
                                          quant_engine.predict_full ─► s_quant
                                          (XGBoost + isotonic, §6.2:
                                           p_up, p_down, calibrated,
                                           feature_health)
                                                  │
   ┌──────────────────────────────────────────────┘
   ▼
   scoring_engine (§5.3 s_tech blocks · §8.2 weights · §8.5 gate)
      tech 30% · quant 25% · news 10% · sent 10% · macro 10% · opt 5% · liq 5% · risk 5%
      ── confirming-engines gate (≥2) ── stale-data veto ── direction sign ──
                                       │
              GateResult ◄──────────────┼─► Signal (+ invalidations + time_stop_at)
              (no_signal_reason)        │
                                        ▼
                          risk_engine.build_plan (§9)
                            · Donchian-structure stops
                            · time stop (horizon-dependent)
                            · news-event veto (§9.6)
                            · Kelly/vol-target/ATR/correlation/per-asset caps
                            · sized position, R:R re-derived
                                       │
                  RiskVeto ◄────────────┼─────► Signal (sized)
                                        ▼
                          explanation_engine.generate_payload
                          (§10.3 JSON contract · §10.4 safety_repair
                           · §10.5 LRU+TTL cache · DeepSeek or templated)
                                       │
                                       ▼
                                   Signal (final)
                                       │
       ┌───────────────────────────────┼────────────────────────────────┐
       ▼                               ▼                                ▼
  WS push                       alert engine                    execution engine
  /v1/stream                    (§12.2 derive_events:           (paper / Alpaca)
  signals.AAPL                   7 triggers + §12.3
                                 Telegram layout)               • monitor (10s loop)
                                       │                          OPT-IN VIA
                                       ▼                          ATLAS_ENABLE_AUTO_EXECUTION
                              log / webhook /                   • ladder exits
                              telegram / email                  • chandelier trail
                                                                      │
                                                                      ▼
                                                              orders + positions
                                                                      │
                                                                      ▼
                                                              Journal entries
                                                              (closed trades + attribution)
```

Background loops in the `signal-service` lifespan:

- **broadcaster** (5s) — push `regime.global`; every 3rd tick, recompute
  signals for subscribed symbols, evaluate alert rules with §12.2 event
  derivation.
- **monitor** (10s) — **disabled by default**; enable with
  `ATLAS_ENABLE_AUTO_EXECUTION=1`. Sweeps open positions, persists trailing
  state, exits triggered positions via the execution engine. §22 #6
  non-negotiable.

## Packages (16 Python + 1 Next.js)

| Package | Concern |
|---|---|
| `packages/shared-py` (`atlas_shared`) | logging · config · DB · auth · entitlements · metrics · jsonable · **`ExplanationPayload` shared schema** |
| `apps/ingest-equities` | Polygon · Alpaca · synthetic GBM · CLI `--source polygon\|alpaca\|synthetic` |
| `apps/feature-engine` | **50 indicators** (§5.2): EMA / SMA / RSI / MACD / BB / ATR / ADX / VWAP / OBV / Stoch / Ichimoku / **Supertrend / Donchian / Keltner / Pivots / Fibonacci / Relative Volume / 52-week distance / Gap %** / SMC BOS / divergences / realised vol |
| `apps/quant-engine` | XGBoost trend · triple-barrier labels · walk-forward purged CV · isotonic calibration · joblib registry · **`predict_full` → §6.2 schema including `feature_health`** |
| `apps/scoring-engine` | **§5.3 s_tech weighted blocks** · `s_news` · `s_risk` · `s_liq` · `s_quant` · **§8.2 composite weights** · **§8.5 confirmation gate** · `GateResult` carrying `no_signal_reason` |
| `apps/risk-engine` | Kelly / vol-target / ATR-risk / correlation / sector / ADV caps · VaR / CVaR · **Donchian-structure stops (§9.3)** · **news-event veto (§9.6)** · `RiskVeto` |
| `apps/explanation-engine` | **§10.3 JSON contract** (`ExplanationPayload`: summary / bull / bear / why-entry / why-stop / target-logic / confidence / final view) · **§10.4 safety_repair** · **§10.5 LRU+TTL cache** · DeepSeek + templated fallback |
| `apps/macro-engine` | FRED client (**always fail-soft to synthetic** on any error) · 4-regime KMeans · `s_macro` |
| `apps/sentiment-engine` | Lexicon scorer · optional FinBERT · per-ticker aggregation · `s_sent` |
| `apps/news-ingest` | RSS / NewsAPI / file replay · ticker extraction · idempotent persistence + scoring |
| `apps/options-analytics` | Black-Scholes + IV solver · synthetic chain · put/call · IV rank · max pain · dealer GEX · `s_options` |
| `apps/portfolio-service` | positions store · valuation / sector / VaR · `reduce_position` (partial, direction-correct) |
| `apps/journal-service` | auto-log closed trades · attribution · `log_live_close` |
| `apps/alert-service` | **`derive_events()`** (§12.2: 7 triggers) · `AlertEngine` (cooldown + dispatch + last-signal state) · log / webhook / telegram / email channels · **§12.3 Telegram layout** |
| `apps/backtest-service` | event-driven simulator · walk-forward · cost sensitivity · bootstrap-CI Sharpe · deflated Sharpe |
| `apps/execution-service` | paper + Alpaca brokers · `ExecutionEngine` · ladder `plan_exit` + chandelier trail · `run_monitor_once` (**opt-in**) |
| `apps/signal-service` | FastAPI surface — routes / WS / lifespan loops / middleware · `/metrics` · `screener.py` · `providers.py` |
| `apps/web` | Next.js 14 dashboard — **8 pages**, WS-driven cache, lightweight-charts, full §11 surface |

## HTTP surface

```
GET    /healthz, /readyz, /metrics
GET    /v1/me                                 — identity + tier + entitlements
GET    /v1/regime                             — cached macro snapshot
GET    /v1/watchlist           PUT            — per-user, tier-capped
GET    /v1/symbols/{symbol}/bars              — OHLCV for charts
GET    /v1/symbols/{symbol}/options           — chain analytics + s_opt
GET    /v1/signals/{symbol}                   — Signal | null
GET    /v1/signals/{symbol}/debug             — adds veto, snapshots, no_signal_reason, sub-score breakdown
POST   /v1/scan                               — multi-symbol filtered scan
POST   /v1/backtests                          — sync synthetic backtest
GET    /v1/portfolios/{id}                    — holdings + VaR + sector
GET    /v1/journal                            — entries + attribution
GET    /v1/alerts              POST  DELETE   — rules CRUD
GET    /v1/alerts/deliveries                  — delivery audit
POST   /v1/execute                            — open/close (tier-gated)
GET    /v1/orders                             — order history (per user)
WS     /v1/stream                             — regime + signals push

# Phase 2 — Screener (BLUEPRINT §13.2)
GET    /v1/screener/universes                 — built-in universes + metadata
POST   /v1/screener/run                       — ranked candidates with gate reasons

# Phase 4 — Structured explanation (BLUEPRINT §10.3)
POST   /v1/explain/signal                     — full ExplanationPayload (cached)

# Phase 5 — Provider observability (BLUEPRINT §13.1)
GET    /v1/providers/status                   — 12 deps × configured / available / fallback
GET    /v1/data/freshness                     — per-symbol last-bar age + macro snapshot age
```

## Dashboard pages (8)

| Page | Surface |
|---|---|
| `/` | Quick-scan input (1 ticker → symbol page; many → `/scanner`), watchlist with live composite scores, BLUEPRINT explainer card, §23 disclaimer. |
| `/scanner` | Universe dropdown, manual symbol append, horizon / min-composite / top-N / explain controls. Full §11.3 column set. Collapsible no-signal-reasons + skipped-symbols. |
| `/symbols/{symbol}` | Price chart (entry/stop/T1-T3 lines) · SignalCard · **§10.3 ExplanationPanel** (renders the structured payload inline — or the no-signal reason when gated) · macro + sentiment snapshots. |
| `/alerts` | Alert rules CRUD + deliveries audit. |
| `/settings` | Watchlist editor · **ProvidersStatusPanel** (12 deps × config / fallback) · TierSwitcher. |
| `/backtest` | Walk-forward synthetic backtest with cost sweep + Sharpe CI. |
| `/portfolio` | Holdings · sector exposure · VaR strip. |
| `/journal` | Closed-trade log + attribution. |

## Data stores

- **TimescaleDB** (Postgres + extensions) — `bars` hypertable + OLTP rows.
- **Redis** — macro snapshot (read by the API every signal request).
- **joblib** — trend model registry (`ml/registry/trend/<version>.joblib`).
- **JSON file** — `infra/data/universes.json` (screener universe registry,
  env-overridable via `ATLAS_UNIVERSES_PATH`).
- **In-memory LRU+TTL** — explanation cache (§10.5; default 256 entries × 15
  min, env-tunable).

## Migrations

`0001_initial` (bars + signals) · `0002_news` · `0003_portfolio` · `0004_journal`
· `0005_watchlist` · `0006_alerts` · `0007_user_scoping` · `0008_orders`.

## §22 non-negotiables status

| Rule | Status |
|---|---|
| No LLM-only trading signals | ✅ Tech + quant dominate the §8.2 composite |
| No expensive provider dependency | ✅ Synthetic fallback all the way (yfinance + GBM) |
| No hidden black-box score | ✅ `/v1/signals/{symbol}/debug` + `no_signal_reason` |
| No signal without risk plan | ✅ Risk engine wired |
| No alert without invalidation | ✅ §12.3 format includes invalidation |
| No auto-execution by default | ✅ Monitor loop opt-in via `ATLAS_ENABLE_AUTO_EXECUTION=1` |
| No enterprise architecture until real usage | ✅ docker-compose + single process |
| No fake confidence | ✅ Backed by calibrated `p_up`; `feature_health` surfaced |
| No stale data pretending to be live | ✅ Stale-data veto + `/v1/data/freshness` |
| No promise of profit | ✅ §10.4 `safety_repair` rewrites forbidden phrases |

## Deferral ledger — what's deliberately *not* built

| Deferred | Why | When to revisit |
|---|---|---|
| **Rust services** | ADR-0001. Phase-1 volume is comfortable in pure Python. | Profiling shows Python can't keep up, OR tick-level ingest required. |
| **Kafka / event bus** | No fan-out problem at current scale. The broadcaster + Redis Streams are sufficient. | Multi-replica deployments, or multi-tenant fan-out beyond a single process. |
| **ClickHouse** | TimescaleDB alone is fine until multi-year backtests at scale. | Backtests slow / contend with OLTP. |
| **Qdrant / vector DB** | No RAG; in-prompt context is enough for §10.3 JSON output. | When LLM hallucinates or needs historical analog retrieval. |
| **Kubernetes / Helm / ArgoCD** | docker-compose + a single VM gets us to ~100 paying users. | First paying customer requires it, or 24×7 SLAs. |
| **Distributed tracing (OTLP)** | Metrics + structured logs cover the timing need. | Multi-service call path needs span correlation. |
| **Full multi-agent LangGraph** | A single Explanation Writer + §10.4 safety covers the user-facing rationale. | Investigations need >1 agent type (devil's advocate, news analyst). |
| **Real options feed** | Greeks + analytics are real; the chain is synthetic. | When `s_opt` is on a real signal path — needs Polygon options / Unusual Whales. |
| **Real on-chain / crypto pipeline** | BLUEPRINT defers; `crypto_majors` ships as opt-in universe only. | When crypto signals need true on-chain confirmation. |
| **Fundamentals adapter** | Removed from active blueprint (no longer in sub-scores). Finnhub / FMP are paid; not a Phase-1 differentiator. | When equities signals need earnings-quality discrimination — re-introduce as a new sub-score. |
| **Real Clerk/Auth0 wire-up** | Dev mode covers single-tenant + tier-gating end-to-end. | First real user signs up. |
| **Mobile-optimized layout** | The dashboard is desktop-first. | First mobile user complaint. |
| **Backtest job queue** | The current endpoint runs sub-second synthetic backtests. | Real-bar multi-symbol/multi-year backtests start blocking requests. |
| **Alpaca trade-updates WS reconciliation** | Fills treated as immediate at limit/last. | Live (non-paper) trading goes on. |
| **Short-side `close_position` PnL** | Unused — engine routes through `reduce_position` (direction-correct). | Any new caller needs full close semantics. |
| **Hosted notebooks / browser strategy editor** | The CLI + API cover the use case. | Power users want browser-based strategy authoring. |
| **Multi-region active-active** | One region, one VM. | Latency SLO per geography becomes a contract obligation. |
| **Public API SDKs / marketplace / billing** | No paying users yet. | First customer signs the dotted line. |
| **Real social sentiment (Twitter / Reddit)** | `s_sent` formula reserves 30% weight for social; today wired from news data only. | When a social-data adapter exists and the lift on signal quality justifies it. |

See `docs/adr/` for the *why* behind irreversible architectural choices.
GAP_AUDIT.md tracks every item closed during the BLUEPRINT v2 alignment.
