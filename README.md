# ATLAS

**Adaptive Trading Logic & Allocation System** — an institutional-grade, multi-asset, AI-powered trading intelligence engine.

**Docs map:** [docs/](docs/README.md) — start with [`architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the delivered shape, [`architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md) for the design target, [`runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) to run it, and [`CHANGELOG.md`](CHANGELOG.md) for the build log. ADRs in [`docs/adr/`](docs/adr/).

## What's built

**16 Python packages + 1 Next.js app + observability stack**, end-to-end:
ingest → 25 indicators → XGBoost quant → macro/sentiment/options sub-scores →
composite + risk → LLM rationale → signal → WS push → alert engine → paper /
Alpaca execution → ladder + chandelier-trail position monitor → journal →
portfolio + VaR → Prometheus/Grafana. **202 backend tests, 0 lint errors.** See
[`docs/architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the full map
and the **deferral ledger** (what's deliberately *not* built and when to revisit
each).

## Quick start

```bash
# 1. Install toolchains
brew install uv
npm install -g pnpm        # or use npm/yarn directly

# 2. Sync the Python workspace
uv sync

# 3. Start local infra (Postgres + Redis)
docker compose -f infra/docker/docker-compose.yml up -d

# 4. Apply schema
uv run python -m atlas_shared.migrate up

# 5. Ingest bars + refresh macro + news (offline-friendly)
uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA --n-bars 1500
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source file --path data/news_seed.jsonl

# 6. Train trend model
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA --version v1

# 7. Run the API (Terminal 1)
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib \
  uv run --package signal-service uvicorn signal_service.main:app --reload

# 8. Run the dashboard (Terminal 2)
cd apps/web
pnpm install && pnpm dev
# open http://localhost:3000
```

## Repository layout

```
atlas/  (named "Trading" on disk for historical reasons)
├── apps/
│   ├── ingest-equities/       # OHLCV ingestion (Polygon + synthetic)
│   ├── feature-engine/        # 25 technical indicators
│   ├── quant-engine/          # XGBoost trend (triple-barrier + walk-fwd CV + isotonic)
│   ├── scoring-engine/        # sub-scores → composite + gates
│   ├── risk-engine/           # Kelly / vol-target / VaR / veto
│   ├── explanation-engine/    # Claude (prompt-cached) + templated fallback
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
# Trading
