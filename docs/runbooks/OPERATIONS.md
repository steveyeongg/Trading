# Operations runbook

How to bring the system up, refresh data, and respond to the alert rules.

## Bring-up (full local stack)

```bash
# 1. Toolchains
brew install uv
npm install -g pnpm   # or use corepack: `corepack enable pnpm`

# 2. Python workspace
uv sync --dev

# 3. App data plane (Postgres + Redis)
docker compose -f infra/docker/docker-compose.yml up -d
uv run python -m atlas_shared.migrate up   # applies migrations 0001–0008

# 4. Seed market + signals data (offline-clean, no API keys needed)
uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA,SPY,QQQ,BTC,ETH --n-bars 1500
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source file --path data/news_seed.jsonl
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA --version v1

# 5. Seed portfolio + journal (so the dashboard is non-empty)
uv run python -m portfolio_service.seed
uv run python -m journal_service.seed --n-bars 4000

# 6. API + dashboard
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib \
  uv run --package signal-service uvicorn signal_service.main:app --reload   # :8000

cd apps/web && pnpm install && pnpm dev                                       # :3000

# 7. Observability (separate stack)
docker compose -f infra/observability/docker-compose.yml up -d                # :3001 Grafana, :9090 Prom
```

## Cron-driven refreshes (production)

| Job | Cadence | Command |
|---|---|---|
| Bar backfill (REST) | 1× daily off-hours | `uv run python -m ingest_equities backfill --symbols … --days 1` |
| Macro snapshot | every 1h | `uv run python -m macro_engine.refresh` |
| News pull | every 5m | `uv run python -m news_ingest.refresh --source rss` |
| Model retrain | weekly | `uv run python -m quant_engine.train --symbols … --version vN` |

## Going live (real-data) toggles

| Env | Effect |
|---|---|
| `POLYGON_API_KEY` | `ingest_equities backfill` switches from synthetic to Polygon |
| `FRED_API_KEY` | `macro_engine.refresh` uses real FRED series |
| `NEWSAPI_KEY` | `news_ingest.refresh --source newsapi` works |
| `ANTHROPIC_API_KEY` | LLM rationales activate (templated fallback otherwise) |
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | `/v1/execute` routes to Alpaca paper instead of in-process paper |
| `ATLAS_AUTH_MODE=jwt` + `ATLAS_JWKS_URL` + `ATLAS_JWT_ISSUER` + `ATLAS_JWT_AUDIENCE` | switches dashboard from dev-headers to real JWT (Clerk/Auth0) |
| `ATLAS_WEBHOOK_URL` + `ATLAS_WEBHOOK_SECRET` | alerts can use the `webhook` channel (HMAC-SHA256 signed) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | alerts can use the `telegram` channel |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO` | alerts can use the `email` channel |

## Responding to Prometheus alerts

`infra/observability/prometheus/alerts.yml` ships five rules. Triage steps:

### `HighSignalRejectRate` (>95% over 10m)

1. Look at the dashboard's **Signal outcomes** panel — which result label dominates?
2. `vetoed` → the risk engine is over-rejecting. Check tier caps, vol-target,
   per-asset cap, correlation matrix.
3. `gated` → the scoring composite isn't clearing 50. Check sub-score breakdown
   on `/v1/signals/{symbol}/debug` for a representative symbol; macro/sent
   neutrality is common when feeds aren't refreshed.
4. `insufficient_bars` → ingest stalled; check the ingest cron and
   `atlas_pipeline_seconds{stage="features"}` count.

### `PipelineLatencyHigh` (P95 features stage > 1s)

1. Check the **Pipeline latency** panel by stage — which stage spiked?
2. `features` → the indicator pipeline is contending. Check feed volume,
   bar resolution drift, CPU saturation. ADR-0001 prescribes Rust if this
   becomes structural.
3. `quant` → XGBoost predict_proba blowing up; check feature NaN rate.
4. `rationale` → Anthropic API slow / rate-limited; consider downgrading
   the model or disabling explanations for scans.

### `AlertDeliveryFailures` (any non-OK delivery over 10m)

1. **Recent deliveries** panel and `/v1/alerts/deliveries` show which channel.
2. `webhook` → check `ATLAS_WEBHOOK_URL` reachable; HMAC secret matches consumer.
3. `telegram` → bot token rotated? Chat id correct? Bot in the chat?
4. `email` → SMTP throttling; check `SMTP_HOST` reachable from the API host.

### `OrderRejections` (any rejected order over 15m)

1. `GET /v1/orders` — what's the `detail`?
2. Paper broker rejections almost always mean no reference price (no bars
   ingested for the symbol) or non-positive qty.
3. Alpaca rejections: check API credentials, paper buying power, symbol
   validity, market hours.

### `SignalServiceDown`

1. Is uvicorn running?
2. Is `/metrics` reachable from the Prometheus container? Linux hosts
   may need `host.docker.internal` mapped via `extra_hosts`.

## Kill switches

| Action | How |
|---|---|
| Stop firing new alerts | `UPDATE alert_rules SET enabled = FALSE WHERE …` (or DELETE in `/settings`). |
| Stop auto-executing trades | Lower the dev tier below Elite via `/settings` (production: revoke `broker_autotrade` on the user). |
| Pause the position monitor | Bounce the API; monitor cancels in lifespan teardown. |
| Stop WS live push | Drop the Prometheus alert `SignalServiceDown` cascading; or set a firewall rule on `:8000`. |

## Common one-off recoveries

| Symptom | Fix |
|---|---|
| Dashboard sidebar empty | `GET /v1/watchlist` returned the hardcoded fallback. Apply migration `0005`, hit `/settings` → Save. |
| Portfolio page 500 | Migration `0003` not applied, or no positions for `user_id='dashboard'`. Run `portfolio_service.seed`. |
| Journal page empty | `journal_service.seed --n-bars 4000` to backfill from a synthetic backtest. |
| `/v1/metrics` empty | No traffic yet; hit `/healthz` then re-scrape. |
| Macro regime stuck at `unknown` | Synthetic mode with < 900 days. Run `macro_engine.refresh` (default 900d) and confirm with `/v1/regime`. |
| `python -m <pkg>` says *"No module named __main__"* | The package needs a `__main__.py` shim. `ingest_equities` was the original offender; `tests/test_cli_entrypoints.py` now guards every documented CLI against this. |
| `ingest_equities` upsert errors with *"A value is required for bind parameter 'vwap'"* | The bar source omitted optional columns. Fixed in `upsert_bars` via `_BAR_FIELDS` normalisation. If a new source is added, add its columns to `_BAR_FIELDS` and the SQL together; `tests/test_ingest_store.py` enforces the contract. |
| Journal / Orders page throws `e.<field>.toFixed is not a function` | A NUMERIC column came through as a JSON string (Decimal). Coerce the store output with `atlas_shared.to_jsonable` before returning to the route. `journal_service.list_entries` and `execution_service.list_orders` already do; new stores should too. |
