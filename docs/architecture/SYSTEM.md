# ATLAS — System map

Single-page tour of what's built, how it fits together, and the things that
were intentionally left out. Complements `BLUEPRINT.md` (the design target)
with the actual delivered shape.

## Lifecycle, end-to-end

```
                ┌─ macro_engine.refresh ────► Redis ─────┐
                │   (FRED → KMeans regime)               │
                │                                        ▼
ingest_equities ─► TimescaleDB ─► feature_engine (25 ind.) ─► s_tech
   (Polygon →     bars                                     ─► quant_engine ─► s_quant
    Alpaca fallback)                                          (XGBoost,
                                                              calibrated)
                                                                     │
news_ingest ─► news_items / news_scores ─► s_sent                    │
                (RSS / NewsAPI / file)                                │
                                                                     ▼
options-analytics (synthetic chain) ─► s_opt                  ┌─────────────┐
                                                              │  composite  │
                                                              └──────┬──────┘
                                                                     │
                                                              ┌──────▼──────┐
                                                              │  risk gate  │
                                                              │  (Kelly /   │
                                                              │   VaR /     │
                                                              │  caps)      │
                                                              └──────┬──────┘
                                                                     │
                                                            ┌────────▼────────┐
                                                            │ explanation     │
                                                            │ writer          │
                                                            │ (Claude cached) │
                                                            └────────┬────────┘
                                                                     │
                                                            ┌────────▼────────┐
                                                            │     Signal      │
                                                            └────┬───┬────┬───┘
                                                                 │   │    │
                                            ┌────────────────────┘   │    └──────────────────┐
                                            ▼                        ▼                       ▼
                                   ┌────────────────┐       ┌────────────────┐      ┌────────────────┐
                                   │   WS push     │        │  alert engine  │      │ execution      │
                                   │ /v1/stream    │        │ (rules, cooldown,│    │ engine         │
                                   │  signals.AAPL │        │ channels)      │      │ (paper/Alpaca) │
                                   └───────┬───────┘        └────────┬───────┘      └────────┬───────┘
                                           │                         │                       │
                                           ▼                         ▼                       ▼
                                   Dashboard live              log/webhook/             orders +
                                                              telegram/email             positions
                                                                                          │
                                                                              ┌───────────┴────────────┐
                                                                              ▼                        ▼
                                                                       Position monitor         Journal entries
                                                                       (10s loop)               (closed trades)
                                                                       partial ladder
                                                                       chandelier trail
                                                                              │
                                                                              ▼
                                                                       reduce_position
                                                                       → realized PnL
                                                                       → journal
```

Background loops in the `signal-service` lifespan:

- **broadcaster** (5s) — push `regime.global`; every 3rd tick, recompute signals
  for symbols any WS client subscribed to and evaluate alert rules.
- **monitor** (10s) — sweep open positions, persist trailing state, exit
  triggered positions via the execution engine.

## Packages (16 Python + 1 Next.js)

| Package | Concern |
|---|---|
| `packages/shared-py` (`atlas_shared`) | logging · config · DB (async SQLAlchemy + migrations) · auth (JWT + dev) · entitlements · metrics · jsonable (Decimal→float for API responses) |
| `apps/ingest-equities` | Polygon REST · Alpaca Market Data v2 · synthetic GBM → `bars` hypertable (CLI `--source polygon\|alpaca\|synthetic`) |
| `apps/feature-engine` | 25 indicators (pandas-ta) — RSI/MACD/BB/EMA stack/ATR/ADX/VWAP/OBV/Stoch/Ichimoku/SMC-BOS/divergences/realized-vol |
| `apps/quant-engine` | XGBoost trend model · triple-barrier labels · walk-forward purged CV · isotonic calibration · joblib registry |
| `apps/scoring-engine` | `s_tech`/`s_quant`/`s_liq` · composite + regime-conditional weights · `generate_signal` (gates + conviction) |
| `apps/risk-engine` | Kelly · vol-target · ATR-risk · correlation/sector/ADV caps · VaR/CVaR · `RiskVeto` |
| `apps/explanation-engine` | DeepSeek via OpenAI-compatible API · server-side prompt cache · templated fallback (offline-safe) |
| `apps/macro-engine` | FRED client (with synthetic fallback) · 4-regime KMeans · `s_macro` |
| `apps/sentiment-engine` | Lexicon scorer (always-on) · optional FinBERT · per-ticker aggregation · `s_sent` |
| `apps/news-ingest` | RSS / NewsAPI / file replay · ticker extraction · idempotent persistence + scoring |
| `apps/options-analytics` | Black-Scholes + IV solver · synthetic chain · put/call · IV rank · max pain · dealer GEX · `s_options` |
| `apps/portfolio-service` | positions store · valuation/sector/VaR analytics · `reduce_position` (partial-aware, direction-correct PnL) |
| `apps/journal-service` | auto-log closed trades · attribution (R-multiple/hit rate/expectancy/exit reason) · `log_live_close` |
| `apps/alert-service` | rule predicate · `AlertEngine` (cooldown + dispatch) · log/webhook/telegram/email channels |
| `apps/backtest-service` | event-driven simulator · walk-forward · cost sensitivity · bootstrap-CI Sharpe · deflated Sharpe |
| `apps/execution-service` | paper + Alpaca brokers · `ExecutionEngine` (open/close) · `plan_exit` (ladder + trail) · `run_monitor_once` |
| `apps/signal-service` | the FastAPI surface — all routers, WS, lifespan loops, middleware, `/metrics` |
| `apps/web` | Next.js 14 dashboard — 7 pages, WS-driven cache, lightweight-charts |

## HTTP surface

```
GET    /healthz, /readyz, /metrics
GET    /v1/me                                 — identity + tier + entitlements
GET    /v1/regime                             — cached macro snapshot
GET    /v1/watchlist           PUT            — per-user, tier-capped
GET    /v1/symbols/{symbol}/bars              — OHLCV for charts
GET    /v1/symbols/{symbol}/options           — chain analytics + s_opt
GET    /v1/signals/{symbol}                   — Signal | null (gates)
GET    /v1/signals/{symbol}/debug             — adds veto, snapshots
POST   /v1/scan                               — multi-symbol filtered scan
POST   /v1/backtests                          — sync synthetic backtest
GET    /v1/portfolios/{id}                    — holdings + VaR + sector
GET    /v1/journal                            — entries + attribution
GET    /v1/alerts              POST  DELETE   — rules CRUD
GET    /v1/alerts/deliveries                  — delivery audit
POST   /v1/execute                            — open/close (tier-gated)
GET    /v1/orders                             — order history (per user)
WS     /v1/stream                             — regime + signals push
```

## Data stores

- **TimescaleDB** (Postgres + extensions) — `bars` hypertable + OLTP rows.
- **Redis** — macro snapshot (read by the API every signal request).
- **joblib** — trend model registry (`ml/registry/trend/<version>.joblib`).

## Migrations

`0001_initial` (bars + signals) · `0002_news` · `0003_portfolio` · `0004_journal`
· `0005_watchlist` · `0006_alerts` · `0007_user_scoping` · `0008_orders`.

## Deferral ledger — what's deliberately *not* built

| Deferred | Why | When to revisit |
|---|---|---|
| **Rust services** | ADR-0001. Phase-1 volume (500 × 1m bars) is comfortable in pure Python. | Profiling shows Python can't keep up, OR tick-level ingest is required. |
| **Kafka / event bus** | Phase-1 has no fan-out problem. The broadcaster + Redis Streams are sufficient. | Multi-replica deployments, or multi-tenant fan-out beyond a single process. |
| **ClickHouse** | TimescaleDB alone is fine until multi-year backtests at scale. | Backtests are slow / contend with OLTP. |
| **Qdrant / vector DB** | No RAG until LLM rationales exist; even then, in-prompt context is enough. | When LLM is hallucinating or needs historical analog retrieval. |
| **Kubernetes / Helm / ArgoCD** | docker-compose + a single VM gets us to ~100 paying users. | First paying customer requires it, or 24×7 SLAs. |
| **Distributed tracing (OTLP)** | Metrics + structured logs cover the timing need without a collector. | A multi-service call path needs span correlation. |
| **Full multi-agent LangGraph** | A single Explanation Writer covers the user-facing rationale. | Investigations need >1 agent type (devil's advocate, news analyst). |
| **Real options feed** | Greeks + analytics are real; the chain is synthetic. | When `s_opt` is on a real signal path — needs Polygon options / Unusual Whales. |
| **Real on-chain feed (`s_chain`)** | Glassnode / Nansen are paid. | When crypto signals need true on-chain confirmation. |
| **Fundamentals (`s_fund`)** | Finnhub / FMP are paid; not a Phase-1 differentiator. | When equities signals need earnings-quality discrimination. |
| **OTel OTLP exporter** | Adds collector dependency for marginal benefit at this scale. | Multi-service request tracing becomes critical. |
| **Real Clerk/Auth0 wire-up** | Dev mode covers single-tenant + tier-gating end-to-end. | First real user signs up. |
| **Mobile-optimized layout** | The dashboard is desktop-first. | First mobile user complaint. |
| **Backtest job queue** | The current endpoint runs sub-second synthetic backtests. | Real-bar multi-symbol/multi-year backtests start blocking requests. |
| **Alpaca trade-updates WS reconciliation** | Fills are treated as immediate at limit/last. | Live (non-paper) trading goes on. |
| **Short-side `close_position` PnL** | Unused — engine routes through `reduce_position` (direction-correct). | Any new caller needs full close semantics. |
| **Backtest UI with strategy editor / hosted notebooks** | The CLI + API cover the use case. | Power users want browser-based strategy authoring. |

See `docs/adr/` for the *why* behind irreversible architectural choices.
