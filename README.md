# ATLAS

**Adaptive Trading Logic & Allocation System** — a rules-based daily swing-trading engine for US stocks: after each close it lists next-session buy-stop ideas (trigger, stop, share count) and says plainly which setups have passed out-of-sample testing ([ADR-0003](docs/adr/0003-rules-based-swing-engine.md)). The original multi-asset, AI-augmented composite signal engine, built to the spec in [`docs/architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md), is kept as a **legacy** path behind the dashboard.

**Docs map:** [docs/](docs/README.md) — start with [`architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the delivered shape, [`architecture/BLUEPRINT.md`](docs/architecture/BLUEPRINT.md) for the design target, [`architecture/GAP_AUDIT.md`](docs/architecture/GAP_AUDIT.md) for the closed punch list, [`runbooks/OPERATIONS.md`](docs/runbooks/OPERATIONS.md) to run it, and [`CHANGELOG.md`](CHANGELOG.md) for the build log. ADRs in [`docs/adr/`](docs/adr/), most recently [ADR-0003: the rules-based swing engine](docs/adr/0003-rules-based-swing-engine.md). Design specs and implementation plans: [`docs/superpowers/specs/`](docs/superpowers/specs/) and [`docs/superpowers/plans/`](docs/superpowers/plans/).

## What's built

**18 Python workspace packages (17 apps + the shared library) + 1 Next.js app + observability stack.**

**Primary path (0.24.0): the rules-based daily swing engine** (`apps/swing-engine`,
CLI only so far). After each US close it scans a point-in-time universe of
liquid US stocks and lists next-session buy-stop ideas (trigger, stop, share
count) ranked by relative strength, under a SPY trend-regime gate. It is
long-only, uses daily SIP bars, has no ML input and places no orders. Every
setup is flagged **UNPROVEN** until it passes a pre-registered out-of-sample
test. See [Swing engine](#swing-engine-rules-based-daily) below and
[ADR-0003](docs/adr/0003-rules-based-swing-engine.md).

**Legacy composite pipeline** (still in the repo and behind the dashboard; no
longer the default path to a trade idea, see ADR-0003): manual symbols /
screener universe → ingest → **50 indicators** → XGBoost quant (with
`feature_health`) → news / sentiment / macro / options / liquidity / risk
sub-scores → BLUEPRINT §8.2 composite weights + §8.5 confirmation gate → §9
risk engine (Donchian-structure stops + time stops + news veto) → DeepSeek
§10.3 structured JSON explanation (with §10.4 safety repair + §10.5 local
cache) → Signal → WS push → **§12.2 event-aware** alert engine (§12.3 Telegram
format) → paper / Alpaca execution (**opt-in**) → ladder + chandelier-trail
monitor → journal → portfolio + VaR → Prometheus / Grafana. Its ML trend model
was trained on leaked labels and is refused unless retrained; its 1-minute
signal recompute is off by default (see [Legacy switches](#legacy-composite-switches)).

**1,115 backend tests passing** (9 skipped: they need a local Postgres, a DeepSeek
key or seeded data), dashboard typecheck clean. All 30 GAP_AUDIT items for the
BLUEPRINT v2 alignment are closed. The swing engine is built to the BLUEPRINT
§22 non-negotiables; the legacy composite is known to fall short of "no fake
confidence" (its `p_up` came from a leaky-label model, see ADR-0003). See
[`docs/architecture/SYSTEM.md`](docs/architecture/SYSTEM.md) for the full map
and the **deferral ledger** (what's deliberately *not* built and when to
revisit each).

## Swing engine (rules-based daily)

Run everything from the repo root. You need the schema applied (Quick start
step 5, which includes `0009_swing_assets`) and, for the two data jobs,
`ALPACA_API_KEY` and `ALPACA_API_SECRET` in `.env`.

```bash
uv run python -m swing_engine backfill                       # first run: load history (later: add --refresh-assets for new listings)
uv run python -m swing_engine update                         # then nightly after the close
uv run python -m swing_engine scan                           # tonight's regime + ranked ideas
uv run python -m swing_engine insample --family breakout     # tune in-sample, draft the pre-registration
uv run python -m swing_engine oos --family breakout --version breakout-v1   # the one out-of-sample test
```

| Command | What it does |
|---|---|
| `backfill` | Loads SIP daily bars from 2016-01-01 (`--start`, or `--symbols A,B` for a few tickers) for every active **and delisted** common stock plus SPY, up to the last completed session. The asset list is fetched from Alpaca on the first run only, and later runs reuse the stored list, so add `--refresh-assets` (for example weekly) to re-fetch the active and inactive lists first and pick up new listings and delistings. Resumable: re-run it after an interruption, even a day later, and it fetches only what is missing. Exits 1 if some symbols failed. |
| `update` | Appends the newest sessions to everything stored. If a split or dividend rewrote a symbol's adjusted history, it detects that on the overlap day and refetches that symbol in full instead of splicing two price scales. It never discovers new listings (`backfill --refresh-assets` does), and symbols more than 30 sessions behind are left to `backfill`. Exits 1 if some symbols failed. |
| `scan` | Reads the stored bars (no Alpaca keys needed) and prints the SPY regime, breadth, the position and risk budget, and the top 20 ideas for the next session: buy-above price, stop, shares, dollars at risk, relative-strength percentile (RS, 0-100) and flags. Shares are sized on the default account (`RiskSettings`: $25,000, 1% risk per trade, 20% max position, 8 positions, 6% open risk); there are no position records or settings UI until Phase 2, so the book shown is empty. `--as-of YYYY-MM-DD` replays an earlier session. In a downtrend, ideas are listed as watch-only at zero shares. |
| `insample --family breakout` | Runs the setup family's declared parameter grid on 2016-2022 only, picks the variant with the highest expectancy among those with 100+ trades, and writes a **draft** pre-registration to `ml/validation/breakout/breakout-v1.prereg.json`. The draft seals the chosen parameters **and the rules around them** (leader gate, universe filter, fill model, the gate portfolio's risk settings, regime size factors, the declared grid). The printout and `breakout-v1.insample.json` also show what to review first: the data fingerprint, delisted coverage, the median eligible universe, distinct trades per variant and the chosen variant's year-by-year results. Review the draft and commit it. A revision (v2, v3) needs `--hypothesis "..."` and must change the parameters or the rules: one identical to an earlier version is refused. `--family pullback` works the same way. |
| `oos --family breakout --version breakout-v1` | Runs that version's out-of-sample test (2023 onward) once. It refuses to start unless the pre-registration is committed and unchanged, its sealed rules are the code's rules now, it is not a repeat of an earlier version, the last stored session is fully loaded, and the family's budget of 3 runs is not spent. It writes `ml/validation/breakout/breakout-v1.report.json` (with the code commit it ran on) and prints **PASSED, now Live** or **FAILED, stays Research only**. Commit the report. |

**Live means validated.** A setup is Live only if its latest report passed the
ship gate (out-of-sample expectancy above 0R, profit factor at least 1.2, 100+
**distinct** trades, better than random entries, drawdown no worse than SPY),
its pre-registration is still the sealed file that run used (the report records
the file's sha256), **and** the rules that file sealed are still the code's
rules (edit the leader gate, universe, fills, risk settings, regime factors or
grid in `config.py` and the family drops to Research only: "rules changed since
its run"). Everything else is scanned too but every idea carries an `UNPROVEN`
flag. With nothing under `ml/validation/` yet, all ideas are UNPROVEN. Scan
output is a research list, not advice or a prediction.

How the gate counts: expectancy and profit factor use every triggered idea, the
same basis as the forward record, but a stock that sits near its high triggers
a breakout every day, so the 100-trade floor counts only **distinct** trades
(entered while no earlier trade on that symbol was still open). The regime
filter stays a gate unless the out-of-sample run shows strong evidence it hurt
(30+ downtrend trades, a higher Sharpe without it, and the ungated version
passing the full gate). The combined portfolio in a report includes the other
family only after that family's own out-of-sample run, with its registered
parameters.

**Data notes**

- The **free Alpaca plan** provides SIP historical daily bars with a 15-minute
  delay (about 200 requests per minute), which is all this engine needs. The
  swing engine always requests `feed=sip`, split/dividend adjusted
  (`adjustment=all`), and never asks for data newer than 16 minutes, so
  `ALPACA_FEED` does not apply to it.
- Alongside the adjusted bars (`resolution='1d'`) it stores **raw, unadjusted
  bars as `1d_raw`**. They are used **only for the $10 price floor**, so the
  universe is point-in-time across splits: an adjusted history would show a
  stock that later split as trading under $10 in years it did not. Everything
  else (dollar volume, indicators, fills) uses adjusted prices.
- A stopped database, a schema without migration `0009`, or a dropped network
  ends any command with the error and a one-line `hint:` saying what to run.
  Network failures and timeouts are retried; a chunk Alpaca rejects because of
  one bad symbol is split until only that symbol fails.
- Phase 1 is CLI only. The nightly job, forward tracking, positions, dashboard
  pages and Telegram digest are Phase 2
  ([plan](docs/superpowers/plans/2026-10-18-swing-engine-phase2.md)); the
  design is in the [spec](docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md).

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
uv run python -m atlas_shared.migrate up                     # migrations 0001–0009

# 6. Ingest bars — pick ONE path
#    (a) Real data (recommended) — auto-picks Polygon, else Alpaca:
uv run python -m ingest_equities backfill --source auto --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ,IONQ,QBTS,NOW,TSM,MRVL --days 7
#    (b) Offline / no API key — synthetic GBM bars (dev only):
# uv run python -m ingest_equities synthetic --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ --n-bars 1500

# 7. Refresh macro + news (offline-friendly fallbacks if no FRED/NewsAPI keys)
uv run python -m macro_engine.refresh
uv run python -m news_ingest.refresh --source newsapi --hours 48

# 8. (legacy composite only) Train the trend model, else `s_quant` is null.
#    Labels are per-symbol now; the old v0/v1 files were trained on leaked labels
#    and are refused. Optional: step 10 loads this one if you prefix it with
#    ATLAS_TREND_MODEL=ml/registry/trend/v2.joblib.
uv run python -m quant_engine.train --symbols AAPL,MSFT,NVDA,TSLA,SPY,QQQ,IONQ,QBTS,NOW,TSM,MRVL --version v2

# 9. Seed portfolio + journal so the dashboard pages aren't empty
uv run python -m portfolio_service.seed
uv run python -m journal_service.seed --n-bars 4000

# 10. Run the API (Terminal 1). ATLAS_TREND_MODEL is optional: without a retrained model
#     (step 8) the legacy `s_quant` stays null. To load it, prefix the command with
#     ATLAS_TREND_MODEL=ml/registry/trend/v2.joblib
uv run --package signal-service uvicorn signal_service.main:app --reload     # :8000

# 11. Run the dashboard (Terminal 2)
cd apps/web && pnpm install && pnpm dev                                       # :3000
# open http://localhost:3000  → quick-scan box, scanner, alerts, settings...

# 12. (optional) Observability stack — Prometheus + Grafana
# docker compose -f infra/observability/docker-compose.yml up -d              # :9090, :3001
```

After step 11, the dashboard renders the default watchlist
(AAPL/MSFT/NVDA/TSLA/SPY/QQQ — equities-only by default, BLUEPRINT §4.3). The
WS broadcaster pushes the regime every ~5s; live signal recompute is a legacy
feature that is off unless you set `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1`. Edit the watchlist at
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
| | `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_FEED=iex\|sip`) | `--source alpaca` pulls bars from Alpaca Market Data v2 (free IEX feed by default). The same keys power the swing engine's `backfill` / `update` (always SIP daily bars; `ALPACA_FEED` is ignored there). | one of Polygon OR Alpaca; **required** for the swing engine's data jobs |
| **Macro** | `FRED_API_KEY` | `macro_engine.refresh` uses real FRED series. *Any* failure (missing key, HTTP error, rate limit) silently degrades to synthetic series — never raises. | no |
| **News** | `NEWSAPI_KEY` | `news_ingest.refresh --source newsapi` (else RSS / file replay) | no |
| **LLM (rationales)** | `DEEPSEEK_API_KEY` (+ optional `ATLAS_EXPLAIN_MODEL=deepseek-chat\|deepseek-reasoner`) | DeepSeek emits the strict §10.3 JSON contract; missing key → templated payload with the same schema | no — templated fallback otherwise |
| **Execution** | reuses `ALPACA_API_KEY` + `ALPACA_API_SECRET` (+ optional `ALPACA_BASE_URL`) | `/v1/execute` routes to Alpaca paper instead of the in-process paper broker | no |
| **Auto-execution** | `ATLAS_ENABLE_AUTO_EXECUTION=1` | Starts the 10s position-monitor loop (auto-closes on stop/target/time). **Off by default — §22 #6 non-negotiable.** | no |
| **Risk thresholds** | `ATLAS_MIN_COMPOSITE`, `ATLAS_MIN_CONFIDENCE`, `ATLAS_MIN_CONFIRMING_ENGINES`, `ATLAS_MIN_AGREE_THRESHOLD`, `ATLAS_NEWS_VETO_THRESHOLD`, `ATLAS_MAX_BAR_AGE_SWING_H` | Loosen / tighten signal gates without code changes | no (defaults sensible) |
| **ML registry (legacy composite)** | `ATLAS_TREND_MODEL` | Points the signal service at a trained joblib model. Only models trained with per-symbol labels load; the old `v0` / `v1` files are refused. Unset or refused means `s_quant` is null. Read at import time: export it in the shell that launches `uvicorn`, not in `.env`. | no |
| **Legacy switches** | `ATLAS_ALLOW_LEGACY_MODEL=1`, `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` | See [Legacy composite switches](#legacy-composite-switches) | no (both off by default) |
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

### Legacy composite switches

Two env vars undo the safe defaults chosen in
[ADR-0003](docs/adr/0003-rules-based-swing-engine.md). Neither is needed for
the swing engine.

- `ATLAS_ALLOW_LEGACY_MODEL=1` lets the signal service load a trend model
  trained before the per-symbol label fix (the existing `v0` / `v1` joblibs).
  Those learned each ticker's price level from leaked labels (the same method
  scores AUC 0.93 on pure random walks), so their `p_up` is not evidence of
  skill. **Not recommended.**
  It is read each time a model is loaded (at API startup, after `.env` is
  loaded, so it may live in `.env`). Retrain with
  `uv run python -m quant_engine.train ... --version v2` instead.
- `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` re-enables the WebSocket broadcaster's
  1-minute signal recompute (every ~15s for subscribed symbols) and the alert
  rules evaluated on it. It is read once at process start from the **real shell
  environment, not from `.env`**: export it in the shell that launches
  `uvicorn`. The regime push (`regime.global`) is always on.

## Repository layout

```
atlas/  (named "Trading" on disk for historical reasons)
├── apps/
│   ├── ingest-equities/       # OHLCV ingestion (Polygon ↔ Alpaca ↔ yfinance ↔ synthetic GBM)
│   ├── feature-engine/        # 50 indicators (BLUEPRINT §5.2): EMAs, SMAs, RSI, MACD,
│   │                          # ATR, BB, VWAP, OBV, ADX, Stoch, Ichimoku, Supertrend,
│   │                          # Donchian, Keltner, Pivots, Fib, RVOL, 52-week dist, gap %,
│   │                          # SMC BOS, divergences, realised vol
│   ├── quant-engine/          # LEGACY: XGBoost trend (per-symbol triple-barrier + walk-fwd CV + isotonic) +
│   │                          # §6.2 predict_full → {p_up, p_down, s_quant, calibrated, feature_health}
│   ├── scoring-engine/        # LEGACY composite: §5.3 s_tech weighted blocks · §8.2 composite weights ·
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
│   ├── swing-engine/          # PRIMARY PATH: rules-based daily swing engine — SIP daily bars,
│   │                          # point-in-time universe, SPY regime, RS leaders, breakout/pullback
│   │                          # setups, sizing, nightly scan, backtest + validation gate
│   └── web/                   # Next.js 14 dashboard — 8 pages (legacy composite surfaces)
├── packages/shared-py/        # logging · db · auth · entitlements · metrics · schemas
├── infra/
│   ├── data/                  # universes.json (BLUEPRINT §4.3 built-in screener universes)
│   ├── docker/                # Postgres+Timescale+Redis
│   └── observability/         # Prometheus + Grafana + dashboard JSON + alerts
├── ml/                        # registry (legacy models), validation (swing pre-registrations + reports), notebooks, experiments
├── docs/                      # SYSTEM.md, BLUEPRINT.md, GAP_AUDIT.md, ADRs, runbooks
└── tests/                     # cross-package (1,115 passing, 9 skipped)
```

## What's next

The next step is the swing engine's product layer (Phase 2): a nightly job
with catch-up, forward tracking of every published idea, positions with
trailing-stop follow-up, dashboard pages and a Telegram digest. Before that
matters, run the validation commands above and commit the first
pre-registrations and reports. The rest is tracked in
[`docs/architecture/SYSTEM.md`'s deferral ledger](docs/architecture/SYSTEM.md#deferral-ledger--whats-deliberately-not-built):
real options / fundamentals adapters (synthetic fallbacks exist), Clerk/Auth0
wire-up (entitlement layer is built), OTLP distributed tracing, and Kubernetes
+ multi-region deployment once paying users justify the operational tax.

## License

Proprietary. © Steve Yeong.
