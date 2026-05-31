# ATLAS

**Adaptive Trading Logic & Allocation System** — an institutional-grade, multi-asset, AI-powered trading intelligence engine.

**Docs map:** [docs/](docs/README.md) — start with [`architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the delivered shape, [`architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md) for the design target, [`runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) to run it, and [`CHANGELOG.md`](CHANGELOG.md) for the build log. ADRs in [`docs/adr/`](docs/adr/).

## What's built

**16 Python packages + 1 Next.js app + observability stack**, end-to-end:
ingest → 25 indicators → XGBoost quant → macro/sentiment/options sub-scores →
composite + risk → LLM rationale → signal → WS push → alert engine → paper /
Alpaca execution → ladder + chandelier-trail position monitor → journal →
portfolio + VaR → Prometheus/Grafana. **221 backend tests, 0 lint errors.** See
[`docs/architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the full map
and the **deferral ledger** (what's deliberately *not* built and when to revisit
each).

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
uv run python -m ingest_equities backfill --source auto --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ --days 7
#    (b) Offline / no API key — synthetic GBM bars (dev only):
# uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ --n-bars 1500

# 7. Refresh macro + news (offline-friendly fallbacks if no FRED/NewsAPI keys)
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source file --path data/news_seed.jsonl

# 8. Train the trend model (else `s_quant` is null)
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA --version v1

# 9. Seed portfolio + journal so the dashboard pages aren't empty
uv run python -m portfolio_service.seed
uv run python -m journal_service.seed --n-bars 4000

# 10. Run the API (Terminal 1)
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib \
  uv run --package signal-service uvicorn signal_service.main:app --reload   # :8000

# 11. Run the dashboard (Terminal 2)
cd apps/web && pnpm install && pnpm dev                                       # :3000
# open http://localhost:3000

# 12. (optional) Observability stack — Prometheus + Grafana
# docker compose -f infra/observability/docker-compose.yml up -d              # :9090, :3001
```

After step 11, the dashboard renders the fallback watchlist
(AAPL/MSFT/NVDA/TSLA/SPY/QQQ/BTC/ETH) and the WS broadcaster starts
publishing signals every ~15s. Edit the watchlist at
[http://localhost:3000/settings](http://localhost:3000/settings) or via
`PUT /v1/watchlist` (see [`docs/runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md)).

## Configuration

Everything is driven by env vars loaded from `.env` at the repo root. The
**authoritative catalog with inline how-to-get-the-credentials notes** lives in
[`.env.example`](.env.example); copy it to `.env` and uncomment what you use.
Every feature is fail-soft — missing keys degrade gracefully, they don't crash
the pipeline.

| Group | Env var(s) | What it unlocks | Required? |
|---|---|---|---|
| **Infra** | `POSTGRES_DSN`, `REDIS_URL` | Data plane (bars + macro cache) | yes (defaults to local Docker) |
| **Auth** | `ATLAS_AUTH_MODE` (`dev`\|`jwt`), `ATLAS_DEV_TIER` | `dev` trusts `X-Dev-User`/`X-Dev-Tier` headers for offline use | no — `dev` mode is the default |
| | `ATLAS_JWKS_URL`, `ATLAS_JWT_ISSUER`, `ATLAS_JWT_AUDIENCE`, `ATLAS_JWT_TIER_CLAIM` | Real JWT via Clerk/Auth0 once `ATLAS_AUTH_MODE=jwt` | only in prod |
| **Bars (equities)** | `POLYGON_API_KEY` | `ingest_equities backfill --source polygon` pulls real bars | one of Polygon OR Alpaca |
| | `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_FEED=iex\|sip`) | `--source alpaca` pulls bars from Alpaca Market Data v2 (free IEX feed, or paid SIP) | one of Polygon OR Alpaca |
| **Macro** | `FRED_API_KEY` | `macro_engine.refresh` uses real FRED series (else synthetic) | no |
| **News** | `NEWSAPI_KEY` | `news_ingest.refresh --source newsapi` (else RSS / file replay) | no |
| **LLM (rationales)** | `DEEPSEEK_API_KEY` (+ optional `ATLAS_EXPLAIN_MODEL=deepseek-chat\|deepseek-reasoner`) | LLM-generated trade explanations via DeepSeek (OpenAI-compat API, server-side prompt cache) | no — templated fallback otherwise |
| **Execution** | reuses `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_BASE_URL`) | `/v1/execute` routes to Alpaca paper instead of the in-process paper broker | no |
| **ML registry** | `ATLAS_TREND_MODEL` | Points the signal service at a trained joblib model | recommended (else `s_quant` is null) |
| **Alerts — webhook** | `ATLAS_WEBHOOK_URL`, `ATLAS_WEBHOOK_SECRET` | HMAC-SHA256 signed POST to your endpoint | no |
| **Alerts — Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram bot push (see `.env.example` for the @BotFather + `getUpdates` flow) | no |
| **Alerts — Email** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` | SMTP delivery | no |
| **Runtime** | `ENV` (`dev`\|`staging`\|`prod`), `LOG_LEVEL`, `LOG_JSON` | Log shape + level | defaults are fine |

The `log` alert channel is **always on** — no env required — so you always
have a delivery sink for testing rules. See
[`docs/runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) for the per-toggle
behavior matrix and the "going live" checklist.

## Repository layout

```
atlas/  (named "Trading" on disk for historical reasons)
├── apps/
│   ├── ingest-equities/       # OHLCV ingestion (Polygon → Alpaca fallback; synthetic CLI for offline dev)
│   ├── feature-engine/        # 25 technical indicators
│   ├── quant-engine/          # XGBoost trend (triple-barrier + walk-fwd CV + isotonic)
│   ├── scoring-engine/        # sub-scores → composite + gates
│   ├── risk-engine/           # Kelly / vol-target / VaR / veto
│   ├── explanation-engine/    # DeepSeek (OpenAI-compat, server-cached) + templated fallback
│   ├── macro-engine/          # FRED + KMeans regime → s_macro
│   ├── sentiment-engine/      # lexicon + optional FinBERT → s_sent
│   ├── options-analytics/     # BS Greeks + chain analytics → s_opt
│   ├── news-ingest/           # RSS / NewsAPI / file → news_items
│   ├── portfolio-service/     # positions + valuation + reduce_position
│   ├── journal-service/       # closed-trade log + attribution
│   ├── alert-service/         # rules + log/webhook/telegram/email channels
│   ├── backtest-service/      # event-driven sim + cost sweep + bootstrap CI
│   ├── execution-service/     # paper/Alpaca + ladder monitor (plan_exit)
│   ├── signal-service/        # FastAPI surface + WS + lifespan loops + /metrics
│   └── web/                   # Next.js 14 dashboard (7 pages)
├── packages/shared-py/        # logging · db · auth · entitlements · metrics
├── infra/
│   ├── docker/                # Postgres+Timescale+Redis
│   └── observability/         # Prometheus + Grafana + dashboard JSON + alerts
├── ml/                        # registry, notebooks, experiments
├── docs/                      # SYSTEM.md, BLUEPRINT.md, ADRs, runbooks
└── tests/                     # cross-package
```

## What's next

Tracked in [`docs/architecture/SYSTEM.md`'s deferral ledger](docs/architecture/SYSTEM.md#deferral-ledger--whats-deliberately-not-built). High-value items still ahead:
real options / fundamentals / on-chain data adapters (the synthetic fallbacks are
ready), Clerk/Auth0 wire-up (the entitlement layer is built), OTLP distributed
tracing, and Kubernetes + multi-region deployment once paying users justify the
operational tax.

## License

Proprietary. © Steve Yeong.
