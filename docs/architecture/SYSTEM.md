# ATLAS — System map

Single-page tour of what's built, how it fits together, and the things that
were intentionally left out. Complements `BLUEPRINT.md` (the design target)
with the actual delivered shape.

> **Status (0.25.0):** the **rules-based daily swing engine**
> (`apps/swing-engine`, with its product layer in `apps/signal-service` and the
> four dashboard pages in `apps/web`) is the primary path. Both of its setups
> (`breakout-v1`, `pullback-v1`) **failed** their single out-of-sample test, so
> both are Research only and every idea is shown as Unproven. The 8-engine
> composite described in BLUEPRINT v2, the XGBoost trend model and the
> 1-minute signal path are **legacy**: the code is kept and still serves the
> older dashboard pages, but it is no longer the default recommendation path
> ([ADR-0003](../adr/0003-rules-based-swing-engine.md)). All 30 GAP_AUDIT items
> for the BLUEPRINT v2 alignment are closed. The swing engine is built to the
> §22 non-negotiables; the legacy composite is known to fall short of "no fake
> confidence" (see the §22 table below). 1,691 backend tests passing with a local
> Postgres up (6 skipped: they need a DeepSeek key, seeded data or a machine
> with no Postgres; without Postgres the database tests skip instead),
> dashboard typecheck clean.

## Swing engine (rules-based daily)

The primary path. Daily bars only, US common stocks, long-only, no ML, no
order placement. Every rule is a hypothesis that has to pass an out-of-sample
test before it is shown as Live (spec
`docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md` §7).

```
   Alpaca free plan: /v2/assets · /v2/calendar · /v2/stocks/bars
   (timeframe=1Day, feed=sip, 15-min delay; every request ends >= 16 min ago)
                       │
                       ▼
   swing_engine backfill (resumable; --refresh-assets re-fetches /v2/assets)  /  update (nightly, split-aware)
      · ends at TradingCalendar.last_completed_session (no partial bars)
      · adjustment=all  ─► bars(resolution='1d')      split/dividend adjusted
      · adjustment=raw  ─► bars(resolution='1d_raw')  as printed; price floor only
      · /v2/assets      ─► assets (active AND delisted; `kind` classified)
      · adjustment trap: overlap day moved ⇒ refetch that symbol's full history
                       │
                       ▼
   swing_engine scan   (reads stored bars only; no Alpaca keys)
      store.liquid_prefilter (at the universe's $ volume floor)
          ─► build_panel (wide Panel on SPY's sessions, cut at the scan session)
          │
          ▼  every step below is a pure function of that truncated Panel
      indicators      SMA/EMA/Wilder ATR/RSI per contiguous segment (gap > 10 sessions restarts)
      universe        point-in-time: bar today · raw close >= $10 · 50-bar median $ volume
                      >= $20M · >= 252 sessions in segment · ATR14 >= 0.5% of close
                      (drops price-pinned buyout targets) · operating-company stock
      regime          SPY uptrend (close > SMA200 and SMA50 > SMA200) / downtrend
                      (close < SMA200 and SMA50 < SMA200) / mixed (anything else)
                      + breadth
      leaders / RS    close > SMA50 > SMA200, SMA200 rising, RS percentile >= 70,
                      within 25% of the 52-week high
      setups          breakout-v1 (leader within 2% of its 50-session closing high,
                      ATR10/ATR50 <= 0.85; buy above the 50-session high + 0.1 ATR)
                      pullback-v1 (leader with RSI(2) <= 10 or close <= EMA20;
                      buy above today's high + 0.05 ATR)
      fill / exit     next-session buy-stop, no chase above trigger + 1 ATR,
                      initial stop, chandelier trail, 30-session time stop
      sizing          fixed-fractional (1% risk, 20% position cap, 8 positions,
                      6% open risk) x regime factor (1.0 / 0.5 / 0.0)
      rank            by RS percentile (or by closeness to the trigger if a Live
                      family's report found RS ranking did not help)
          │
          ▼
   regime banner · book usage · ranked ideas (buy above, stop, shares, $ risk,
   RS, flags) · watch-only list in a downtrend · UNPROVEN flag on every idea
   from a setup with no passing out-of-sample report

   swing_engine insample / oos   (the gate between "hypothesis" and "Live")
      load_validation_data ─► only the panel's columns, 500 symbols per batch
      backtest.trades ─► the SAME fill/exit model as the scan
      backtest.stats (n and n_distinct) / baseline (100-seed random entry)
          / portfolio (capacity-limited sim + SPY)
      validation.protocol ─► IS 2016-2022 grid · OOS 2023+ once per version
                             · budget 3 OOS runs per family · ship gate
                             (trade floor on distinct trades) · protocol block
      validation.prereg   ─► committed, sha256-sealed pre-registration: params,
                             gate, windows AND the protocol block (leader, universe,
                             execution, risk, regime factor, grid); no identical revisions
      ml/validation/<family>/<version>.{insample,prereg,report}.json   (committed;
                             the report records code_commit / code_dirty)
      Live = latest report passed AND the sealed prereg sha matches the report's
             AND its protocol block equals the current config
```

### Product layer (0.25.0)

The engine above is a library plus a CLI. The product layer runs it every night
inside the one API process and puts it on the dashboard.

```
   signal_service.main lifespan
      swing_engine.scheduler      16:45 ET (close + 45 min; 13:45 on a half day), catch-up at
                                  start when a due session is missing, retry every 15 min while a
                                  catch-up or the nightly fails (until the next scan is due); off
                                  with ATLAS_DISABLE_SCHEDULER=1 or under pytest
          │            (the same job: "Run scan now" = POST /v1/scan/run, and
          │             `python -m swing_engine job`)
          ▼
   swing_engine.job.run_job       one job at a time per process; every step recorded in swing_job_runs
      assets    weekly asset-list refresh
      bars      first run: resumable backfill (progress done/total); later: split-aware update
                (backfill again after an asset refresh, a stale SPY, or last run's transient
                failures)
      scan      every missed session in order (loader.py -> scan.py); ideas stored with their
                narration (narrate.py); reconstructed = saved after its session opened;
                coverage < 95% of tonight's bars = saved, flagged partial
      tracking  tracking.py: re-simulate pending/open ideas with execution.simulate_trade
                (an idea it cannot track keeps its stored state)
      positions positions.py: nightly action for each recorded position, with the
                pre-registered trail and time limit of the idea it came from
      digest    digest.py: one Telegram message per session, validated ideas only; never
                from a partial scan
      a cancelled run closes its row as failed; API start-up fails any row left running
          │
          ▼
   Postgres (migration 0010)  swing_settings · swing_scans · swing_ideas · swing_positions ·
                              swing_job_runs · swing_digests
          │
          ▼
   signal_service.swing_routes (/v1/today, /v1/stocks, /v1/positions, /v1/track-record,
      /v1/settings, /v1/scan/run, /v1/system/status)
      reads the stored scan and job rows, the committed ml/validation reports, and the
      swing_context cache (the job's target-session ScanContext, for stock and position pages;
      built in the background, and no request waits on it for long: positions list 0 s,
      position writes 5 s, a stock page 20 s then 503 "Loading market data...")
      writes need X-ATLAS-Client: web; the API answers only ATLAS_ALLOWED_HOSTS
          │
          ▼
   apps/web: Today · Positions · Track record · Settings · stock page ("Check a stock")
```

Module map (all under `apps/swing-engine/src/swing_engine/` unless noted):
`settings.py` (validated account settings), `product_store.py` (the six tables),
`tracking.py` (forward tracking and the live/reconstructed rule), `positions.py`
(nightly actions, split rescale, stop ratchet), `narrate.py` (plain-English
reason and what cancels an idea), `checklist.py` (the stock page's rule
checklist), `digest.py` (Telegram text and delivery), `loader.py` (stored bars
to a `Panel`), `job.py`, `scheduler.py`. In `apps/signal-service/src/signal_service/`:
`swing_routes.py` (the routes), `swing_context.py` (the context cache),
`swing_track_record.py` (the track-record reader over `ml/validation` and the
stored ideas), `swing_deps.py` (the job's real dependencies, shared by the
scheduler and "Run scan now").

Invariants the product layer keeps: it places no orders (`auto_execution`
reports the API's startup flag, off by default), it shows a stated reason
instead of an empty area, every idea from a setup that has not passed testing is
tagged Unproven and left out of the digest, one set of ideas exists per
(session, symbol, setup) with the first `published_at` kept, and a position in a
stock with no bar on the scan day gets an explicit *no price data* action
instead of a stale stop. Single user: the swing tables have no `user_id`.

## Legacy composite pipeline (lifecycle, end-to-end)

Kept for the existing dashboard and API. Not the default path to a trade idea:
it computes swing signals from 1-minute IEX bars and its `p_up` came from a
model trained on leaked labels (ADR-0003).

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

- **broadcaster** (5s) — push `regime.global`. The legacy 1-minute signal
  recompute (every 3rd tick, for subscribed symbols) and the alert-rule
  evaluation on it (§12.2 event derivation) are **off by default**; set
  `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` in the real shell environment before
  the process starts (it is read at import time, not from `.env`).
- **swing scheduler** — on by default; runs the nightly job at the close plus
  45 minutes and catches up missed sessions at start. `ATLAS_DISABLE_SCHEDULER=1`
  turns it off; it never starts under pytest. It also warms the swing context
  cache in the background. It places no orders.
- **monitor** (10s) — **disabled by default**; enable with
  `ATLAS_ENABLE_AUTO_EXECUTION=1`. Sweeps open positions, persists trailing
  state, exits triggered positions via the execution engine. §22 #6
  non-negotiable.

## Packages (18 Python workspace members + 1 Next.js)

| Package | Concern |
|---|---|
| `packages/shared-py` (`atlas_shared`) | logging · config · DB · auth · entitlements · metrics · jsonable · **`ExplanationPayload` shared schema** |
| `apps/ingest-equities` | Polygon · Alpaca · synthetic GBM · CLI `--source polygon\|alpaca\|synthetic` |
| `apps/feature-engine` | **50 indicators** (§5.2): EMA / SMA / RSI / MACD / BB / ATR / ADX / VWAP / OBV / Stoch / Ichimoku / **Supertrend / Donchian / Keltner / Pivots / Fibonacci / Relative Volume / 52-week distance / Gap %** / SMC BOS / divergences / realised vol |
| `apps/quant-engine` | **Legacy.** XGBoost trend · triple-barrier labels (now computed per symbol; each artifact is stamped with its `label_scheme`) · walk-forward purged CV · isotonic calibration · joblib registry · **`predict_full` → §6.2 schema including `feature_health`**. Models trained before the per-symbol fix (`v0`, `v1`) learned each ticker's price level from leaked labels and are refused by the signal service |
| `apps/scoring-engine` | **Legacy composite.** **§5.3 s_tech weighted blocks** · `s_news` · `s_risk` · `s_liq` · `s_quant` · **§8.2 composite weights** · **§8.5 confirmation gate** · `GateResult` carrying `no_signal_reason` |
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
| `apps/signal-service` | FastAPI surface — routes / WS / lifespan loops / middleware · `/metrics` · `screener.py` · `providers.py` · **swing product routes** (`swing_routes.py`, `swing_context.py`, `swing_track_record.py`, `swing_deps.py`) and the **nightly scheduler** started in the lifespan |
| `apps/swing-engine` | **Primary path.** Rules-based daily swing engine (US common stocks, long-only, no order placement). **Data:** Alpaca SIP daily bars (`adjustment=all`, stored as `1d`) plus unadjusted raw bars (`1d_raw`, price floor only), resumable `backfill` (`--refresh-assets` picks up new listings and delistings; a stored asset list is otherwise reused) and split-aware `update` (overlap check, full refetch when the adjustment moved) · **Assets:** active and delisted Alpaca assets classified into operating-company stock vs ETF / fund / SPAC / warrant / unit / right / preferred / OTC · **Calendar:** `/v2/calendar` sessions, half days, 16-minute SIP delay · **Panel + indicators:** wide `Panel`, strict per-segment SMA / EMA / Wilder ATR / RSI (a gap of more than 10 sessions restarts a symbol) · **Universe:** point-in-time price / dollar-volume / history / minimum-volatility (ATR14 at least 0.5% of close, which drops price-pinned buyout targets) / asset-kind screen · **Regime:** SPY uptrend / mixed / downtrend, plus breadth · **Leaders / RS:** weighted 63/126/189/252-session return percentile and the leader gate · **Setups:** `breakout-v1`, `pullback-v1` (versioned frozen params) · **Fill / exit model:** one `simulate_trade` used by scan, backtest and baseline (next-session buy-stop, no chase, gap-through stops, chandelier trail, time stop, `delisted` exit) · **Sizing:** fixed-fractional with position / count / open-risk caps and correlation overlap flags · **Scan:** look-ahead-free nightly scan, ranked ideas, watch-only in a downtrend · **Backtest:** trade list, stats, 100-seed random-entry baseline, capacity-limited portfolio sim vs SPY buy-and-hold · **Validation:** pre-registered in-sample grid, one-shot out-of-sample run, ship gate, Live / Research-only verdicts in `ml/validation/` · **Product layer (0.25.0):** `settings.py`, `product_store.py` (migration 0010 tables), `tracking.py` (split-safe forward tracking, live vs reconstructed), `positions.py` (nightly action per position), `narrate.py`, `checklist.py`, `digest.py` (Telegram, validated ideas only), `loader.py`, `job.py` (the nightly job with catch-up), `scheduler.py` (16:45 ET) · CLI `python -m swing_engine backfill\|update\|scan\|job\|insample\|oos` · synthetic test helpers |
| `apps/web` | Next.js 14 dashboard — **4 swing pages** (Today, Positions, Track record, Settings) plus the stock page `/stocks/[symbol]`, and the **7 legacy composite pages** (scanner, symbols, alerts, backtest, portfolio, journal, `settings/legacy`) kept at their old addresses; TanStack Query, lightweight-charts |

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
WS     /v1/stream                             — regime push (signals only with ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1)

# Swing product (0.25.0) — signal_service/swing_routes.py; single user, no auth dependency
GET    /v1/today                              — latest scan: validated / unproven / watch-only ideas, position actions, capacity
GET    /v1/stocks/{symbol}                    — chart bars, tonight's levels, eligibility checklist
GET    /v1/positions            POST          — the user's open and closed trades (record a trade)
PATCH  /v1/positions/{id}                     — move the stop
POST   /v1/positions/{id}/close               — close at the price the user got (result in R)
GET    /v1/track-record                       — per setup: in-sample, out-of-sample (every failed condition), live record
GET    /v1/settings             PUT           — account and risk settings, universe floors, digest switch
POST   /v1/settings/telegram-test             — send one fixed test message
POST   /v1/scan/run                           — "Run scan now": 202 started, 409 already running, 503 cannot start
GET    /v1/system/status                      — data as of, last scan + steps, next scan, backfill progress,
                                                keys configured, scheduler running/off and why

# Legacy Phase 2 — Screener (BLUEPRINT §13.2)
GET    /v1/screener/universes                 — built-in universes + metadata
POST   /v1/screener/run                       — ranked candidates with gate reasons

# Phase 4 — Structured explanation (BLUEPRINT §10.3)
POST   /v1/explain/signal                     — full ExplanationPayload (cached)

# Phase 5 — Provider observability (BLUEPRINT §13.1)
GET    /v1/providers/status                   — 12 deps × configured / available / fallback
GET    /v1/data/freshness                     — per-symbol last-bar age + macro snapshot age
```

## Dashboard pages

Browser calls go to `/api/*`, which `next.config.js` rewrites to the API. Data
fetching is client-side TanStack Query. API types for the swing pages are
hand-mirrored in `apps/web/src/lib/swing-types.ts`, the legacy ones in
`types.ts`; update them when a response shape changes.

**Swing pages (primary; the nav bar):**

| Page | Surface |
|---|---|
| `/` Today | Market line (`UPTREND, new trades OK` / `MIXED, half-size trades` / `DOWNTREND, stand aside: no new buy ideas`), ranked validated ideas, the muted `Unproven ideas: these setups have not passed testing` section, watch-only names in a downtrend, nightly actions for open positions, last-scan status and failure copy (a failure before the scan was saved, or after it), the partial-data warning, the first-run notice by state (running step, download progress, waiting and why scans are off), **Run scan now**, disclaimer. |
| `/stocks/[symbol]` | Reached from "Check a stock" (the search box on every page) or an idea: daily chart with levels, the eligibility checklist, "I bought this". |
| `/positions` | Record trades placed with the user's broker, the nightly action, stop edit, close at the user's price, closed trades with R and hold time. |
| `/track-record` | Per setup: in-sample, out-of-sample (with each failed condition), live record, verdict. |
| `/settings` | Account, risk and universe settings, Telegram digest switch and test, system status panel; links to `/settings/legacy`. |

**Legacy composite pages (kept, reachable by address, not in the nav; the legacy
HTTP surface above serves them):**

| Page | Surface |
|---|---|
| `/scanner` | Universe dropdown, manual symbol append, horizon / min-composite / top-N / explain controls. Full §11.3 column set. Collapsible no-signal-reasons + skipped-symbols. |
| `/symbols/{symbol}` | Price chart (entry/stop/T1-T3 lines) · SignalCard · **§10.3 ExplanationPanel** (renders the structured payload inline — or the no-signal reason when gated) · macro + sentiment snapshots. |
| `/alerts` | Alert rules CRUD + deliveries audit. |
| `/settings/legacy` | Watchlist editor · **ProvidersStatusPanel** (12 deps × config / fallback) · TierSwitcher. |
| `/backtest` | Walk-forward synthetic backtest with cost sweep + Sharpe CI. |
| `/portfolio` | Holdings · sector exposure · VaR strip. |
| `/journal` | Closed-trade log + attribution. |

## Data stores

- **TimescaleDB** (Postgres + extensions) — `bars` hypertable + OLTP rows.
  The swing engine writes daily bars as `resolution='1d'` (adjusted) and
  `'1d_raw'` (unadjusted), `ts` = session date at 00:00 UTC, plus the `assets`
  table. Legacy 1-minute bars share the hypertable; every swing read filters
  on its two resolutions.
- **Swing product tables** (migration `0010`, `DOUBLE PRECISION` / `JSONB`,
  no `user_id`) — `swing_settings` (one row), `swing_scans` (one per scanned
  session, payload JSON, `reconstructed`), `swing_ideas` (one per session ×
  symbol × setup, with forward-tracking columns and `known_date`),
  `swing_positions` (what the user says they hold), `swing_job_runs` (per-step
  log), `swing_digests` (one row per session and kind, so a digest goes out once).
- **Redis** — macro snapshot (read by the API every signal request).
- **joblib** — legacy trend model registry (`ml/registry/trend/<version>.joblib`).
  `load_trend_model` refuses artifacts without the per-symbol label scheme
  unless `ATLAS_ALLOW_LEGACY_MODEL=1`.
- **Committed JSON** — `ml/validation/<family>/<version>.{insample,prereg,report}.json`,
  the swing engine's pre-registrations and out-of-sample verdicts. A fresh
  clone reads these; it does not re-run validation.
- **JSON file** — `infra/data/universes.json` (screener universe registry,
  env-overridable via `ATLAS_UNIVERSES_PATH`).
- **In-memory LRU+TTL** — explanation cache (§10.5; default 256 entries × 15
  min, env-tunable).

## Migrations

`0001_initial` (bars + signals) · `0002_news` · `0003_portfolio` · `0004_journal`
· `0005_watchlist` · `0006_alerts` · `0007_user_scoping` · `0008_orders`
· `0009_swing_assets` (the swing engine's `assets` table and a `bars (resolution, ts)` index)
· `0010_swing_product` (the six swing product tables above)
· `0011_alert_rule_symbols` (legacy alert rules: the old `ANY` default becomes NULL, meaning any ticker).

## §22 non-negotiables status

The rows describe the legacy composite as aligned to BLUEPRINT v2, with the
swing engine noted where it differs. The audit behind ADR-0003 showed the
composite does **not** meet "no fake confidence".

| Rule | Status |
|---|---|
| No LLM-only trading signals | ✅ Tech + quant dominate the §8.2 composite |
| No expensive provider dependency | ✅ Synthetic fallback all the way (yfinance + GBM) |
| No hidden black-box score | ✅ `/v1/signals/{symbol}/debug` + `no_signal_reason` |
| No signal without risk plan | ✅ Risk engine wired |
| No alert without invalidation | ✅ §12.3 format includes invalidation |
| No auto-execution by default | ✅ Monitor loop opt-in via `ATLAS_ENABLE_AUTO_EXECUTION=1` |
| No enterprise architecture until real usage | ✅ docker-compose + single process |
| No fake confidence | Swing engine: met. It shows no probability or confidence figure, only measured per-setup statistics. Legacy composite: **not met**. Its `p_up` and "confidence" came from a model trained on leaked labels (ADR-0003); that model is now refused and `s_quant` is null without a retrained one. |
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
| **Proper ESLint setup for `apps/web`** | `pnpm lint` is broken at baseline (no ESLint config; eslint 9 against `eslint-config-next` 14 crashes), and fixing it would lint every legacy page. Phase 2 verified its files with `pnpm typecheck`, `pnpm build` and a one-off ESLint run over the files each task touched. | A separate change: eslint 8, or a compatible flat config, then clean the legacy pages. |
| **Postgres advisory lock for cross-process jobs** | The job's lock is an in-process `asyncio.Lock`, so `python -m swing_engine job` can overlap the API's scheduler. A once-per-session digest record limits the damage to a narrow duplicate-digest race. Single user, one machine. | A second process routinely runs jobs (cron plus API), or a duplicate digest is ever seen. |
| **Stop-history storage** | A position keeps its current stop; the Positions page shows the latest action but not every earlier stop. The spec's "stop history" needs a table or column. | When the history is wanted on the Positions page. |
| **Earnings-date source** | Free sources need a new key, and a guess is worse than nothing. Without one the "earnings within 10 sessions" badge is simply not shown. | When a free, reliable earnings calendar is added. |
| **`ref_close` per idea (exact split rescale)** | Forward tracking rescales an idea's published levels after a split from the ATR ratio, which is sensitive to a revised signal-day bar and ignores dividend-sized (under 0.5%) restatements. Storing the signal-day close with each idea (a new migration) makes it exact. Rare and small in effect. | A mis-tracked fill near the trigger is seen, or the live record is relied on for a split-heavy stretch. |
| **`avg_hold` in the live record** | `sessions_held` is not stored on a tracked idea, so the live statistics' average hold is empty (the validation reports have it). Needs migration 0011. | When live hold time is wanted next to the tested figure. |
| **Live record per setup version** | The live and reconstructed records on Track record pool every forward-tracked idea of a family, whatever its `version`. Today each family has one version (`breakout-v1`, `pullback-v1`), so nothing is mixed yet. | Before a family's v2 is published: split the live record (and its underperformance warning) by version. |
| **ML ranker for the swing engine** | The audit found no edge in the legacy model; the engine is rules-based and every rule has to pass out-of-sample testing first (ADR-0003). | Only if a model beats the rules out of sample under the same pre-registration protocol. |
| **Real social sentiment (Twitter / Reddit)** | `s_sent` formula reserves 30% weight for social; today wired from news data only. | When a social-data adapter exists and the lift on signal quality justifies it. |

See `docs/adr/` for the *why* behind irreversible architectural choices;
[ADR-0003](../adr/0003-rules-based-swing-engine.md) records the pivot to the
rules-based swing engine.
GAP_AUDIT.md tracks every item closed during the BLUEPRINT v2 alignment.
