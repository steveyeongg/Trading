# ATLAS

**Adaptive Trading Logic & Allocation System** — a low-cost, multi-asset, AI-augmented stock screener + trading signal engine. Built to the spec in [`docs/architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md).

**Docs map:** [docs/](docs/README.md) — start with [`architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the delivered shape, [`architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md) for the design target, [`architecture/GAP_AUDIT.md`](docs/architecture/GAP_AUDIT.md) for the closed punch list, [`runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) to run it, and [`CHANGELOG.md`](CHANGELOG.md) for the build log. ADRs in [`docs/adr/`](docs/adr/).

## What's built

**16 Python packages + 1 Next.js app + observability stack**, end-to-end:
manual symbols / screener universe → ingest → **50 indicators** → XGBoost
quant (with `feature_health`) → news / sentiment / macro / options / liquidity
/ risk sub-scores → BLUEPRINT §8.2 composite weights + §8.5 confirmation gate
→ §9 risk engine (Donchian-structure stops + time stops + news veto) →
DeepSeek §10.3 structured JSON explanation (with §10.4 safety repair + §10.5
local cache) → Signal → WS push → **§12.2 event-aware** alert engine
(§12.3 Telegram format) → paper / Alpaca execution (**opt-in**) →
ladder + chandelier-trail monitor → journal → portfolio + VaR →
Prometheus / Grafana.

**274 backend tests passing**, dashboard typecheck clean. **All 30
GAP_AUDIT items closed; all 10 §22 non-negotiables satisfied.** See
[`docs/architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the full map
and the **deferral ledger** (what's deliberately *not* built and when to
revisit each).

## Quick start

```bash
# 1. Install toolchains
brew install uv
npm install -g pnpm                                          # or `corepack enable pnpm`

# 2. Sync the Python workspace
uv sync

# 3. Configure credentials (every key is optional — fail-soft)
cp .env.example .env
$EDITOR .env                                                 # see Configuration section below

# 4. Start local infra (Postgres + Redis)
docker compose -f infra/docker/docker-compose.yml up -d

# 5. Apply schema
uv run python -m atlas_shared.migrate up                     # migrations 0001–0008

# 6. Ingest bars — pick ONE path
#    (a) Real data (recommended) — auto-picks Polygon, else Alpaca:
uv run python -m ingest_equities backfill --source auto --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ,IONQ,QBTS,NOW,TSM,MRVL --days 7
#    (b) Offline / no API key — synthetic GBM bars (dev only):
# uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ --n-bars 1500

# 7. Refresh macro + news (offline-friendly fallbacks if no FRED/NewsAPI keys)
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source newsapi --hours 48

# 8. Train the trend model (else `s_quant` is null)
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ,IONQ,QBTS,NOW,TSM,MRVL --version v1

# 9. Seed portfolio + journal so the dashboard pages aren't empty
uv run python -m portfolio_service.seed
uv run python -m journal_service.seed --n-bars 4000

# 10. Run the API (Terminal 1)
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib \
  uv run --package signal-service uvicorn signal_service.main:app --reload   # :8000

# 11. Run the dashboard (Terminal 2)
cd apps/web && pnpm install && pnpm dev                                       # :3000
# open http://localhost:3000  → quick-scan box, scanner, alerts, settings...

# 12. (optional) Observability stack — Prometheus + Grafana
# docker compose -f infra/observability/docker-compose.yml up -d              # :9090, :3001
```

After step 11, the dashboard renders the default watchlist
(AAPL/MSFT/NVDA/TSLA/SPY/QQQ — equities-only by default, BLUEPRINT §4.3) and
the WS broadcaster publishes signals every ~15s. Edit the watchlist at
[`/settings`](http://localhost:3000/settings) or via `PUT /v1/watchlist`.
Crypto, US mega-cap, NASDAQ-100 seed, and Core ETFs ship as explicit opt-in
universes in [`infra/data/universes.json`](infra/data/universes.json) — select
them on the [Scanner page](http://localhost:3000/scanner).

## What the dashboard surfaces

| Page | What's there |
|---|---|
| `/` | Quick-scan input (1 ticker → symbol page; many → scanner), watchlist with live composite scores, "how signals are formed" explainer. |
| `/scanner` | BLUEPRINT §11 + §13.2 — universe dropdown, manual symbols (append-friendly), horizon, min-composite, top-N, optional DeepSeek rationale. Rows that don't publish list their *gate reason*. Full §11.3 column set incl. Tech / Quant / News / Macro / Risk per-engine sub-scores. |
| `/symbols/{symbol}` | Live price chart with entry/stop/T1-T3 lines, signal card, **structured §10.3 explanation panel** (summary, bull case, bear case, why entry/stop, target logic, confidence, final view — or the no-signal reason when gated), macro + sentiment snapshot. |
| `/alerts` | Alert rules CRUD, deliveries audit. Rules fire on the §12.2 event set; Telegram body matches §12.3. |
| `/settings` | Watchlist editor, **provider status panel** (12 dependencies × config / availability / fallback), tier switcher. |
| `/backtest` | Walk-forward synthetic backtest with cost-sweep + Sharpe CI. |
| `/portfolio` | Holdings, sector exposure, VaR strip. |
| `/journal` | Closed-trade log + attribution (hit rate, expectancy R, exit reasons). |

## Configuration

Everything is driven by env vars loaded from `.env` at the repo root. The
**authoritative catalog with inline how-to-get-the-credentials notes** lives in
[`.env.example`](.env.example); copy it to `.env` and uncomment what you use.
Every feature is fail-soft — missing keys degrade gracefully and never crash
the pipeline. `GET /v1/providers/status` returns the live availability matrix.

| Group | Env var(s) | What it unlocks | Required? |
|---|---|---|---|
| **Infra** | `POSTGRES_DSN`, `REDIS_URL` | Data plane (bars + macro cache) | yes (defaults to local Docker) |
| **Auth** | `ATLAS_AUTH_MODE` (`dev`\|`jwt`), `ATLAS_DEV_TIER` | `dev` trusts `X-Dev-User`/`X-Dev-Tier` headers for offline use | no — `dev` mode is the default |
| | `ATLAS_JWKS_URL`, `ATLAS_JWT_ISSUER`, `ATLAS_JWT_AUDIENCE`, `ATLAS_JWT_TIER_CLAIM` | Real JWT via Clerk/Auth0 once `ATLAS_AUTH_MODE=jwt` | only in prod |
| **Bars (equities)** | `POLYGON_API_KEY` | `ingest_equities backfill --source polygon` pulls real bars | one of Polygon OR Alpaca |
| | `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_FEED=iex\|sip`) | `--source alpaca` pulls bars from Alpaca Market Data v2 (free IEX feed by default) | one of Polygon OR Alpaca |
| **Macro** | `FRED_API_KEY` | `macro_engine.refresh` uses real FRED series. *Any* failure (missing key, HTTP error, rate limit) silently degrades to synthetic series — never raises. | no |
| **News** | `NEWSAPI_KEY` | `news_ingest.refresh --source newsapi` (else RSS / file replay) | no |
| **LLM (rationales)** | `DEEPSEEK_API_KEY` (+ optional `ATLAS_EXPLAIN_MODEL=deepseek-chat\|deepseek-reasoner`) | DeepSeek emits the strict §10.3 JSON contract; missing key → templated payload with the same schema | no — templated fallback otherwise |
| **Execution** | reuses `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_BASE_URL`) | `/v1/execute` routes to Alpaca paper instead of the in-process paper broker | no |
| **Auto-execution** | `ATLAS_ENABLE_AUTO_EXECUTION=1` | Starts the 10s position-monitor loop (auto-closes on stop/target/time). **Off by default — §22 #6 non-negotiable.** | no |
| **Risk thresholds** | `ATLAS_MIN_COMPOSITE`, `ATLAS_MIN_CONFIDENCE`, `ATLAS_MIN_CONFIRMING_ENGINES`, `ATLAS_MIN_AGREE_THRESHOLD`, `ATLAS_NEWS_VETO_THRESHOLD`, `ATLAS_MAX_BAR_AGE_SWING_H` | Loosen / tighten signal gates without code changes | no (defaults sensible) |
| **ML registry** | `ATLAS_TREND_MODEL` | Points the signal service at a trained joblib model | recommended (else `s_quant` is null) |
| **Alerts — webhook** | `ATLAS_WEBHOOK_URL`, `ATLAS_WEBHOOK_SECRET` | HMAC-SHA256 signed POST to your endpoint | no |
| **Alerts — Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram bot push in the §12.3 layout (emoji header, full trade plan, invalidation, disclaimer) | no |
| **Alerts — Email** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` | SMTP delivery | no |
| **Explanation cache** | `ATLAS_EXPLAIN_CACHE_TTL_S` (default 900), `ATLAS_EXPLAIN_CACHE_SIZE` (default 256) | §10.5 LRU+TTL cache size and lifetime for structured rationales | no |
| **Universes** | `ATLAS_UNIVERSES_PATH` | Override the screener universe file (default `infra/data/universes.json`) | no |
| **Runtime** | `ENV` (`dev`\|`staging`\|`prod`), `LOG_LEVEL`, `LOG_JSON` | Log shape + level | defaults are fine |

The `log` alert channel is **always on** — no env required — so you always
have a delivery sink for testing rules. See
[`docs/runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) for the per-toggle
behavior matrix and the "going live" checklist.

## Repository layout

```
atlas/  (named "Trading" on disk for historical reasons)
├── apps/
│   ├── ingest-equities/       # OHLCV ingestion (Polygon ↔ Alpaca ↔ yfinance ↔ synthetic GBM)
│   ├── feature-engine/        # 50 indicators (BLUEPRINT §5.2): EMAs, SMAs, RSI, MACD,
│   │                          # ATR, BB, VWAP, OBV, ADX, Stoch, Ichimoku, Supertrend,
│   │                          # Donchian, Keltner, Pivots, Fib, RVOL, 52-week dist, gap %,
│   │                          # SMC BOS, divergences, realised vol
│   ├── quant-engine/          # XGBoost trend (triple-barrier + walk-fwd CV + isotonic) +
│   │                          # §6.2 predict_full → {p_up, p_down, s_quant, calibrated, feature_health}
│   ├── scoring-engine/        # §5.3 s_tech weighted blocks · §8.2 composite weights ·
│   │                          # s_news / s_risk / s_liq / §8.5 confirmation gate · GateResult
│   ├── risk-engine/           # Kelly · vol-target · ATR-risk · Donchian-structure stops ·
│   │                          # news-event veto · VaR/CVaR · veto reasons
│   ├── explanation-engine/    # §10.3 ExplanationPayload + §10.4 safety_repair +
│   │                          # §10.5 LRU+TTL cache + DeepSeek JSON contract + templated fallback
│   ├── macro-engine/          # FRED (always fail-soft to synthetic) + KMeans regime → s_macro
│   ├── sentiment-engine/      # lexicon + optional FinBERT → s_sent
│   ├── options-analytics/     # BS Greeks + chain analytics → s_opt
│   ├── news-ingest/           # RSS / NewsAPI / file → news_items
│   ├── portfolio-service/     # positions + valuation + reduce_position
│   ├── journal-service/       # closed-trade log + attribution
│   ├── alert-service/         # §12.2 derive_events (7 triggers) + §12.3 Telegram format +
│   │                          # log/webhook/email channels
│   ├── backtest-service/      # event-driven sim + cost sweep + bootstrap CI
│   ├── execution-service/     # paper/Alpaca + ladder monitor (opt-in via ATLAS_ENABLE_AUTO_EXECUTION)
│   ├── signal-service/        # FastAPI surface + WS + lifespan loops + /metrics + screener
│   └── web/                   # Next.js 14 dashboard — 8 pages
├── packages/shared-py/        # logging · db · auth · entitlements · metrics · schemas
├── infra/
│   ├── data/                  # universes.json (BLUEPRINT §4.3 built-in screener universes)
│   ├── docker/                # Postgres+Timescale+Redis
│   └── observability/         # Prometheus + Grafana + dashboard JSON + alerts
├── ml/                        # registry, notebooks, experiments
├── docs/                      # SYSTEM.md, BLUEPRINT.md, GAP_AUDIT.md, ADRs, runbooks
└── tests/                     # cross-package (274 passing)
```

## What's next

Tracked in [`docs/architecture/SYSTEM.md`'s deferral ledger](docs/architecture/SYSTEM.md#deferral-ledger--whats-deliberately-not-built). Now that BLUEPRINT v2 is fully aligned, the
top remaining items are real options / fundamentals adapters (synthetic
fallbacks exist), Clerk/Auth0 wire-up (entitlement layer is built), OTLP
distributed tracing, and Kubernetes + multi-region deployment once paying
users justify the operational tax.

## License

Proprietary. © Steve Yeong.
