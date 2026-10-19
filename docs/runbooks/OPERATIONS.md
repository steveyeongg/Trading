# Operations runbook

How to bring the system up, refresh data, and respond to the alert rules.

The **swing engine** (`apps/swing-engine`, CLI only so far) is the primary
path to a trade idea; everything under "Legacy composite" below (1-minute bars,
the XGBoost model, the composite signal API and dashboard) is kept but is no
longer the default path. See `docs/adr/0003-rules-based-swing-engine.md`.

## Bring-up (full local stack)

```bash
# 1. Toolchains
brew install uv
npm install -g pnpm   # or use corepack: `corepack enable pnpm`

# 2. Python workspace
uv sync --dev

# 3. App data plane (Postgres + Redis)
docker compose -f infra/docker/docker-compose.yml up -d
uv run python -m atlas_shared.migrate up   # applies migrations 0001–0009

# 4. Swing engine (primary path). Needs ALPACA_API_KEY + ALPACA_API_SECRET in .env
#    (the free plan is enough: SIP daily bars, 15-min delay).
uv run python -m swing_engine backfill     # FIRST RUN ONLY: loads 2016-01-01 -> last completed
                                           # session for every stock + SPY. Resumable: if it
                                           # exits 1 or is interrupted, run the same command again.
uv run python -m swing_engine scan         # tonight's regime + ranked ideas (stored bars only)

# 5. Legacy composite: seed market + signals data (offline-clean, no API keys needed)
#    BLUEPRINT §4.3 default watchlist is equities-only; the crypto pipeline
#    is deferred. Crypto is available via the `crypto_majors` opt-in universe.
uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ --n-bars 1500
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source file --path data/news_seed.jsonl
#    Optional: the trend model. Labels are per-symbol now. The old v0/v1 joblibs
#    were trained on leaked labels and the API refuses them (s_quant stays null).
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA --version v2

# 6. Seed portfolio + journal (so the dashboard is non-empty)
uv run python -m portfolio_service.seed
uv run python -m journal_service.seed --n-bars 4000

# 7. API + dashboard (add ATLAS_TREND_MODEL=ml/registry/trend/v2.joblib in front to load the retrained model)
uv run --package signal-service uvicorn signal_service.main:app --reload     # :8000

cd apps/web && pnpm install && pnpm dev                                       # :3000

# 8. Observability (separate stack)
docker compose -f infra/observability/docker-compose.yml up -d                # :3001 Grafana, :9090 Prom
```

The 1-minute signal recompute and its alert evaluation in the WebSocket
broadcaster are **off by default**, so `/v1/stream` only pushes the regime
unless you set `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` in the shell before
starting the API (see "Legacy composite switches" below).

Verify everything by hitting these endpoints once the API is up:

```bash
curl http://localhost:8000/healthz                                 # {"status":"ok"}
curl http://localhost:8000/v1/providers/status | jq '.summary'      # which deps are wired
curl http://localhost:8000/v1/data/freshness | jq '.symbols'        # bar age per default symbol
curl http://localhost:8000/v1/screener/universes | jq '.universes'  # built-in universes
```

Then open the dashboard at <http://localhost:3000>:

- `/scanner` → run the default universe, confirm rows with composite + entry/stop/T1-T3.
- `/symbols/AAPL` → confirm the **ExplanationPanel** renders the §10.3 sections (templated when no `DEEPSEEK_API_KEY`).
- `/settings` → confirm the **ProvidersStatusPanel** shows which deps are configured.

## Cron-driven refreshes (production)

| Job | Cadence | Command |
|---|---|---|
| **Swing first load** | once, before anything else | `uv run python -m swing_engine backfill` (resumable: re-run until it exits 0) |
| **Swing daily update** | every trading day after the close (allow 16+ minutes for the SIP delay, e.g. 17:00 ET) | `uv run python -m swing_engine update` |
| Swing new-listing catch-up | weekly, optional | `uv run python -m swing_engine backfill` (refreshes the asset list, loads new listings, catches up any symbol more than 30 sessions behind; `update` does not discover new listings) |
| **Swing scan** | right after the update succeeds | `uv run python -m swing_engine scan` (stored bars only; capture stdout) |
| Bar backfill (REST, legacy 1m) | 1× daily off-hours | `uv run python -m ingest_equities backfill --symbols … --days 1` |
| Macro snapshot (legacy) | every 1h | `uv run python -m macro_engine.refresh` |
| News pull (legacy) | every 5m | `uv run python -m news_ingest.refresh --source rss` |
| Model retrain (legacy, optional) | weekly | `uv run python -m quant_engine.train --symbols … --version vN` (use a **new** `N`; point `ATLAS_TREND_MODEL` at it) |

`update` and `scan` are safe to run on weekends and holidays: both work from
the last completed session. `backfill` and `update` exit with status 1 when
some symbols failed, so chain the scan on success (`update && scan`) and
re-run `update` to resume. Run `update` before `scan`; the scan never fetches.

Validation is **not** a cron job. `insample` and `oos` are run by hand, with a
git commit of the pre-registration in between (see the README's Swing engine
section). `oos` spends one of a family's 3 out-of-sample runs.

## Going live (real-data) toggles

| Env | Effect |
|---|---|
| `POLYGON_API_KEY` | `ingest_equities backfill --source polygon` (or `--source auto`) pulls real bars from Polygon |
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | (a) **`swing_engine backfill` / `update` pull SIP daily bars** (free plan, 15-min delay, always `feed=sip`; `ALPACA_FEED` does not apply); (b) legacy: `ingest_equities backfill --source alpaca` pulls 1m bars from Alpaca Market Data v2 (free IEX feed; set `ALPACA_FEED=sip` on a paid plan); (c) `/v1/execute` routes to Alpaca paper instead of in-process paper |
| `FRED_API_KEY` | `macro_engine.refresh` uses real FRED series. *Any* failure (missing key, HTTP error, rate limit, empty payload) silently degrades to deterministic synthetic series — never raises. |
| `NEWSAPI_KEY` | `news_ingest.refresh --source newsapi` works |
| `DEEPSEEK_API_KEY` | LLM rationales activate via DeepSeek; emit the strict §10.3 JSON contract with `response_format={"type":"json_object"}`. Missing key → templated payload with the same schema. Set `ATLAS_EXPLAIN_MODEL=deepseek-reasoner` to swap from `deepseek-chat` to R1. |
| `ATLAS_AUTH_MODE=jwt` + `ATLAS_JWKS_URL` + `ATLAS_JWT_ISSUER` + `ATLAS_JWT_AUDIENCE` | Switches dashboard from dev-headers to real JWT (Clerk/Auth0) |
| `ATLAS_WEBHOOK_URL` + `ATLAS_WEBHOOK_SECRET` | Alerts can use the `webhook` channel (HMAC-SHA256 signed) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Alerts can use the `telegram` channel — body rendered in BLUEPRINT §12.3 layout (🚨 header, trade plan, invalidation, disclaimer) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO` | Alerts can use the `email` channel |
| **`ATLAS_ENABLE_AUTO_EXECUTION=1`** | **Starts the 10s position-monitor loop** that auto-closes positions on stop / target / time. **Off by default — BLUEPRINT §22 #6 non-negotiable.** The log line `monitor.disabled` at startup confirms it's off; `monitor.auto_execution_enabled` (warning) confirms it's on. |

### Legacy composite switches

Both are **off by default** (ADR-0003). Neither is needed for the swing engine.

| Env | Effect |
|---|---|
| `ATLAS_ALLOW_LEGACY_MODEL=1` | Lets the API load a trend model trained before the per-symbol label fix (the old `v0` / `v1`). Their labels leaked across tickers, so `p_up` is not evidence of skill. **Not recommended**; retrain instead. Without it, the API logs `trend_model.legacy_refused` and runs with `s_quant` null. |
| `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` | Re-enables the broadcaster's 1-minute signal recompute (every 3rd 5s tick) and the alert-rule evaluation on it. Read once at process start from the **real shell environment, not from `.env`**: `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1 uv run --package signal-service uvicorn …`. Without it, only `regime.global` is pushed and alert rules do not fire from the stream. |

### Signal-quality knobs (BLUEPRINT §8.5 / §9.6)

Loosen / tighten gates without code changes. Defaults are deliberately
conservative; sparse-data deployments may want to relax them.

| Env | Default | Purpose |
|---|---|---|
| `ATLAS_MIN_COMPOSITE` | `50.0` | Minimum \|composite\| to publish |
| `ATLAS_MIN_CONFIDENCE` | `0.55` | Minimum calibrated `p_up` distance from 0.5 |
| `ATLAS_MIN_CONFIRMING_ENGINES` | `2` | Active engines that must lean in the signal direction |
| `ATLAS_MIN_AGREE_THRESHOLD` | `40.0` | Sub-score magnitude that counts as "confirming" |
| `ATLAS_NEWS_VETO_THRESHOLD` | `60.0` | \|s_news\| against the signal direction that vetoes |
| `ATLAS_MAX_BAR_AGE_INTRADAY_H` | `2` | Stale-data veto (intraday horizon) |
| `ATLAS_MAX_BAR_AGE_SWING_H` | `30` | Stale-data veto (swing horizon) |
| `ATLAS_MAX_BAR_AGE_POSITION_H` | `96` | Stale-data veto (position horizon) |
| `ATLAS_MAX_BAR_AGE_LONG_TERM_H` | `168` | Stale-data veto (long-term horizon) |
| `ATLAS_EXPLAIN_CACHE_TTL_S` | `900` | §10.5 explanation cache TTL (15 min) |
| `ATLAS_EXPLAIN_CACHE_SIZE` | `256` | §10.5 explanation cache max entries |
| `ATLAS_UNIVERSES_PATH` | `infra/data/universes.json` | Screener universe registry override |

## Responding to Prometheus alerts

`infra/observability/prometheus/alerts.yml` ships five rules. Triage steps:

### `HighSignalRejectRate` (>95% over 10m)

1. Look at the dashboard's **Signal outcomes** panel — which result label dominates?
2. `vetoed` → the risk engine is over-rejecting. Check tier caps, vol-target,
   per-asset cap, correlation matrix. **New in v2:** the `adverse_news`
   veto fires when `\|s_news\|` clears the `ATLAS_NEWS_VETO_THRESHOLD`
   against the signal direction.
3. `gated` → the scoring composite isn't clearing the floor. Pull
   `/v1/signals/{symbol}/debug` — it now returns a `no_signal_reason`
   string that names the failing gate (composite below floor / not enough
   confirming engines / confidence below floor / stale data).
4. `stale_data` → ingest stalled. Pull `/v1/data/freshness` and check the
   ingest cron + `atlas_pipeline_seconds{stage="features"}` count.
5. `insufficient_bars` → no bars stored for the symbol. Hit
   `ingest_equities backfill` for it.

### `PipelineLatencyHigh` (P95 features stage > 1s)

1. Check the **Pipeline latency** panel by stage — which stage spiked?
2. `features` → the indicator pipeline is contending. With 50 indicators
   (BLUEPRINT §5.2 full set) the work is larger than the v1 build; check
   feed volume, bar resolution drift, CPU saturation. ADR-0001 prescribes
   Rust if this becomes structural.
3. `quant` → XGBoost predict_proba blowing up; check feature NaN rate.
   `quant_meta.feature_health` now exposes `ok` / `degraded` / `missing`
   per call — degraded means median-imputed.
4. `rationale` → DeepSeek API slow / rate-limited; consider switching
   `ATLAS_EXPLAIN_MODEL` from `deepseek-reasoner` to `deepseek-chat`, or
   relying on the §10.5 cache (15-min TTL — repeat reads are free). For
   bulk scans, leave `include_explanation=false` (the default).

### `AlertDeliveryFailures` (any non-OK delivery over 10m)

1. **Recent deliveries** panel and `/v1/alerts/deliveries` show which channel.
2. `webhook` → check `ATLAS_WEBHOOK_URL` reachable; HMAC secret matches consumer.
3. `telegram` → bot token rotated? Chat id correct? Bot in the chat? The
   body now follows §12.3 — confirm Telegram isn't truncating it (max
   4096 chars; ATLAS messages are well under).
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
| Stop firing new alerts | `UPDATE alert_rules SET enabled = FALSE WHERE …` (or DELETE in `/alerts`). |
| Stop auto-executing trades | **Unset `ATLAS_ENABLE_AUTO_EXECUTION` and bounce the API.** The monitor loop is now opt-in (BLUEPRINT §22 #6) so this is also the *default* state. |
| Lower the dev tier below Elite | Via `/settings` (production: revoke `broker_autotrade` on the user). Blocks the `/v1/execute` route. |
| Pause the position monitor (without restart) | Not currently supported — bouncing the API is the path. The loop cancels cleanly in lifespan teardown. |
| Stop WS live push | Drop the Prometheus alert `SignalServiceDown` cascading, or firewall `:8000`. |
| Force every signal through the templated rationale | Unset `DEEPSEEK_API_KEY` and bounce. The §10.3 payload shape stays identical — only `payload.source` changes from `deepseek-chat` to `templated`. |

## Common one-off recoveries

| Symptom | Fix |
|---|---|
| Dashboard sidebar empty | `GET /v1/watchlist` returned the hardcoded fallback (AAPL/MSFT/NVDA/TSLA/SPY/QQQ — equities-only since 0.23). Apply migration `0005`, hit `/settings` → Save. |
| Portfolio page 500 | Migration `0003` not applied, or no positions for `user_id='dashboard'`. Run `portfolio_service.seed`. |
| Journal page empty | `journal_service.seed --n-bars 4000` to backfill from a synthetic backtest. |
| `/v1/metrics` empty | No traffic yet; hit `/healthz` then re-scrape. |
| Macro regime stuck at `unknown` | Synthetic mode with < 900 days. Run `macro_engine.refresh` (default 900d) and confirm with `/v1/regime`. |
| Signal returns `null` and you don't know why | Hit `/v1/signals/{symbol}/debug` — the response now carries `no_signal_reason` and per-engine sub-scores. The most common reasons are "composite \|X\| < 50" (gate floor), "only N engine(s) confirm direction" (§8.5 gate), and "stale data: last bar is Xh old" (§9.6 stale veto). |
| Provider keys missing → afraid the pipeline will crash | It won't. `GET /v1/providers/status` enumerates every dep and the documented fallback. `test_pipeline_runs_with_no_provider_keys` pins this guarantee in CI. |
| `python -m <pkg>` says *"No module named __main__"* | The package needs a `__main__.py` shim. `tests/test_cli_entrypoints.py` guards every documented CLI. |
| `ingest_equities` upsert errors with *"A value is required for bind parameter 'vwap'"* | The bar source omitted optional columns. Fixed in `upsert_bars` via `_BAR_FIELDS` normalisation. |
| Journal / Orders page throws `e.<field>.toFixed is not a function` | A NUMERIC column came through as a JSON string. Apply `atlas_shared.to_jsonable` in the route. |
| `swing_engine scan` exits with *"no assets stored"* or *"no SPY bars stored"* | Nothing loaded yet. Run `uv run python -m swing_engine backfill` (needs Alpaca keys and migration `0009`). |
| `swing_engine scan` exits with *"no SPY bar on <date>"* | `--as-of` was a weekend, holiday or a day outside the stored history, so the market regime can't be read and nothing was scanned. Omit `--as-of` to use the latest stored SPY session, or run `update` first. |
| `backfill` / `update` exit 1 with *"failed, re-run to resume"* | Some chunks hit rate limits or network errors. Re-run the same command: it only fetches what is missing, and stored chunks stay stored. |
| `update` prints *"refetched full history (adjustment moved)"* | A split or dividend rewrote that symbol's adjusted prices. Expected: the whole history was refetched so two price scales are never spliced. |
| `swing_engine oos` refuses to start | The pre-registration is missing, uncommitted, changed since commit or hand-edited, the version is not the in-sample choice, a revision has no `--hypothesis`, or the family's 3 out-of-sample runs are spent. Fix the cause it names (for example re-run `insample`, review and commit the draft) and try again. A spent budget cannot be retried until 12 months of new data accrue. |
| Every scan idea is flagged `UNPROVEN` | Expected until a family's latest report under `ml/validation/` passed and its pre-registration still matches the one that run used. |
| API log shows `trend_model.legacy_refused`, `s_quant` is null | The model on disk predates the per-symbol label fix. Retrain with a new version and point `ATLAS_TREND_MODEL` at it (see Bring-up), or accept the refusal. |
| `/v1/stream` only ever pushes `regime.global` | By design: the 1-minute signal recompute is off. See "Legacy composite switches". |
| Telegram message looks plain / no emoji | Pre-0.23 format. Confirm the alert-service is running 0.23+ — the §12.3 layout includes the 🚨 header, trade plan, invalidation, and disclaimer automatically via `channels/base.py format_alert`. |
