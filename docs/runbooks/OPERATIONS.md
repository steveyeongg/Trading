# Operations runbook

How to bring the system up, refresh data, and respond to the alert rules.

The **swing engine** (`apps/swing-engine`, served by the API and shown on the
dashboard's Today, Positions, Track record and Settings pages) is the primary
path to a trade idea; everything under "Legacy composite" below (1-minute bars,
the XGBoost model, the composite signal API and the older dashboard pages) is
kept but is no longer the default path. See
`docs/adr/0003-rules-based-swing-engine.md`.

## Start, stop, check (the normal way)

```bash
scripts/atlas.sh up       # Docker (Postgres + Redis) -> migrations -> API :8000 -> dashboard :3000 -> opens the browser
scripts/atlas.sh status   # Docker, Postgres, API, web, then the nightly scan's state
scripts/atlas.sh down     # stops the API and dashboard (and their child processes), then `docker compose stop`
```

`up` is idempotent and does these in order: it checks that `uv` and `pnpm`
exist and that `.env` has the Alpaca keys (a warning, never a value); runs
`docker info` (if Docker Desktop is not running it prints *"Docker isn't
running. Start Docker Desktop, then run: scripts/atlas.sh up"* and exits 1);
refuses to start if another program holds port 8000 or 3000 (it never kills a
process it did not start); `docker compose up -d`; waits for `pg_isready` in
`atlas-postgres`; `atlas_shared.migrate up`; starts the API
(`uvicorn signal_service.main:app --host 127.0.0.1 --port 8000`, no `--reload`)
and the dashboard (`pnpm dev -H 127.0.0.1 -p 3000`, after
`pnpm install --frozen-lockfile` when `apps/web/node_modules` is missing or older
than the lockfile) with `nohup`, logs in `logs/api.log` and `logs/web.log`, pids
in `.atlas/api.pid` and `.atlas/web.pid`; waits for `/healthz` (90 s) and the
dashboard (120 s), both measured on the wall clock and checked at `127.0.0.1`;
prints `http://localhost:3000` and, on a Mac, opens it. A pid file records the
process's start time as well (read in UTC, whatever the time zone or locale), so
a pid reused after a reboot is never mistaken for ATLAS's.

**The dashboard and the API listen only on this Mac.** ATLAS has no login, and
`pnpm dev` alone would listen on every network interface (Next's default is
`0.0.0.0`), so both servers are bound to `127.0.0.1` and the dashboard's port is
pinned to 3000 (otherwise Next would move to 3001 when 3000 is taken, and the
script would wait on the wrong port). `pnpm dev` itself is `next dev -H 127.0.0.1`
too, so a dashboard started by hand stays local (the script's own `-H 127.0.0.1`
repeats the same value, which is harmless). Postgres and Redis are published on
`127.0.0.1:5432` and `127.0.0.1:6379` only. Another device on your network cannot
open any of them. Don't change that without putting a login in front first.

If your containers were created before this binding (they published `5432` and
`6379` on every interface), run `scripts/atlas.sh up` (or
`docker compose -f infra/docker/docker-compose.yml up -d`) once: Compose sees the
changed ports and recreates both containers. Your data is kept: it lives in the
bind-mounted folders `infra/docker/data/postgres` and `infra/docker/data/redis`,
not in the containers.

**Two more guards on the API.** It answers only requests whose `Host` is
`localhost` or `127.0.0.1` (`ATLAS_ALLOWED_HOSTS`, below; anything else gets
400, which stops a DNS-rebinding page from reading it through a name it
controls). And every write to the swing routes (`POST`/`PUT`/`PATCH` under
`/v1/positions`, `/v1/settings` and `/v1/scan/run`) needs the header
`X-ATLAS-Client: web`, which the dashboard sends and a cross-site form cannot;
without it the API answers 403 *"missing X-ATLAS-Client header"*. To call a
write route with curl, add `-H 'X-ATLAS-Client: web'`. `scripts/atlas.sh status`
only reads.

| Flag | Effect |
|---|---|
| `ATLAS_DISABLE_SCHEDULER=1 scripts/atlas.sh up` | API without the automatic nightly job. |
| `ATLAS_NO_OPEN=1` | Don't open the browser. |
| `ATLAS_API_WAIT_S`, `ATLAS_WEB_WAIT_S` | Seconds `up` waits for the API (90) and dashboard (120). |

If the API or dashboard does not answer in time, `up` prints *"The API didn't
start; see logs/api.log"* (or the web equivalent) with the last 20 log lines and
exits 1; what it started keeps running (`down` stops it, a second `up` waits for
it again). `status` never fails: it reports each piece as running or not, then
summarises `GET /v1/system/status` (data as of, last scan and its status, next
scan, whether the scheduler is running or off and why, first-download progress,
Alpaca and Telegram configured yes/no).

## Bring-up by hand (full local stack)

```bash
# 1. Toolchains
brew install uv
npm install -g pnpm   # or use corepack: `corepack enable pnpm`

# 2. Python workspace
uv sync --dev

# 3. App data plane (Postgres + Redis)
docker compose -f infra/docker/docker-compose.yml up -d
uv run python -m atlas_shared.migrate up   # applies migrations 0001–0011

# 4. Swing engine (primary path). Needs ALPACA_API_KEY + ALPACA_API_SECRET in .env
#    (the free plan is enough: SIP daily bars, 15-min delay). The API's scheduler does this
#    on its own at start-up (see "The nightly job"); by hand:
uv run python -m swing_engine backfill     # first run: loads 2016-01-01 -> last completed session
                                           # for every stock + SPY. Resumable: if it exits 1 or is
                                           # interrupted, run the same command again. Later runs
                                           # reuse the stored asset list; add --refresh-assets to
                                           # pick up new listings and delistings.
uv run python -m swing_engine scan         # tonight's regime + ranked ideas (stored bars only)
uv run python -m swing_engine job          # or: the whole nightly job once (all six steps)

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

cd apps/web && pnpm install && pnpm dev -H 127.0.0.1 -p 3000                  # :3000, this Mac only

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
curl http://localhost:8000/v1/system/status | jq                    # data as of, last/next scan, scheduler, keys
curl http://localhost:8000/v1/providers/status | jq '.summary'      # which deps are wired
curl http://localhost:8000/v1/data/freshness | jq '.symbols'        # bar age per default symbol
curl http://localhost:8000/v1/screener/universes | jq '.universes'  # built-in universes
```

Then open the dashboard at <http://localhost:3000>. **Today** should show the market line and either ideas, an empty-state sentence that says why there are none, or the first-run download progress; **Settings** has the system status panel. The older pages (legacy composite):

- `/scanner` → run the default universe, confirm rows with composite + entry/stop/T1-T3.
- `/symbols/AAPL` → confirm the **ExplanationPanel** renders the §10.3 sections (templated when no `DEEPSEEK_API_KEY`).
- `/settings/legacy` → confirm the **ProvidersStatusPanel** shows which deps are configured.

## The nightly job

The swing engine runs itself while the API is running. `signal_service.main`
starts `swing_engine.scheduler` in its lifespan; the scheduler runs
`swing_engine.job.run_job` once per trading session. You do not need cron for
the swing engine.

**When.** At the close plus 45 minutes, in New York time: **16:45 ET** on a
normal day, **13:45 ET** on a half day (the day after Thanksgiving, for
example), and still 16:45 local New York time after the DST switch (21:45 UTC in
winter). Alpaca's free plan serves a day's bar 16 minutes after the close; the
extra margin lets the bars settle, so the digest is never built on a bar that is
still moving. A run before that time (a manual run at 16:20, say) targets the
previous session, not tonight.

**Steps**, in order, each recorded as it goes (Settings shows them):

| Step | What it does |
|---|---|
| `assets` | Re-fetches Alpaca's active and inactive asset lists when the last refresh is over 7 days old (new listings, delistings). |
| `bars` | The first run (no SPY bars), a run whose `assets` step just refreshed the list, or a database more than 30 sessions behind: the resumable backfill from 2016-01-01, saving `done/total` about every 5% (that is the progress Today shows). Otherwise the split-aware incremental update. Symbols the provider cannot serve are listed in the step; they are not failures. |
| `scan` | Every session after the last stored scan, through the target, oldest first. Each session's ideas are stored with a plain-English reason and what cancels them. Scans are upserts: re-running a session keeps the first `published_at`. It records the target session's `coverage` (of the active common stocks with a bar the session before, the share that also has one tonight). Under 95% the scan is still saved but flagged `partial`: Today warns *"Prices were missing for N of M stocks on DAY; tonight's ideas may be incomplete."* and the digest waits (below). |
| `tracking` | Re-simulates every pending or open published idea (watch-only ones too) to the target session with the same fill and exit model as the tests. Every tracked idea's stock is loaded whatever its liquidity; an idea that still cannot be tracked keeps its stored entry and exit (counted as `untracked`). |
| `positions` | The nightly action for each open position you recorded: hold, raise the stop, stop hit, time limit, or *no price data for SYMBOL since DATE* (a delisted, halted or mistyped symbol). The trailing stop and time limit are the pre-registered ones of the idea the trade came from (both v1 setups: 3.5 ATR, 30 sessions), or the breakout setup's for a trade recorded without an idea. A session already advised keeps its stored action. |
| `digest` | One Telegram digest per session, when it is switched on (below). Skipped with reason *partial data* for a partial scan, without marking the session sent, so a later complete run (the 15-minute retry or **Run scan now**) still sends it. |

The `bars` step uses the resumable backfill instead of the update on a first
run, after the weekly asset refresh, when SPY is more than 30 sessions behind,
and when the previous run failed to download symbols that already have stored
bars (`transient_failed` in the step; symbols that never had a bar are the
provider's permanently unservable ones and do not count).

A step that fails records the reason (first line, secrets removed), the steps
after it do not run and the job is marked failed. When it is `assets`, `bars`,
`scan` or a pre-step (the calendar, the settings), tonight's scan was not saved
and the previous one stays on screen: Today says *"Last scan failed at step X:
reason. Showing the previous scan."* When it is `tracking`, `positions` or
`digest`, tonight's scan was saved and is shown, with the line *"Tonight's scan
finished, but the X step failed: reason."* Re-running is always safe.

**A run is never left "running".** If the API is stopped during a run (quit,
`scripts/atlas.sh down`, the Mac shut down), the job closes its row as failed
with *"interrupted"* on its way out, and the next API start closes any row still
marked running as failed (*"interrupted: the API stopped during the run"*), so
**Run scan now** is never locked out.

**Catch-up.** If the Mac was asleep or off at 16:45 (or the API was down), the
next API start checks whether the latest scanned session is earlier than the
last session whose scan time has passed, and if so runs the job as a
`catchup`: it scans every missed session in order and sends **one** digest that
lists the missed days. Ideas from a session that had already opened when they
were saved are marked *reconstructed*; they appear and are tracked but never
count toward the live record. Starting between a close plus 16 minutes and its
scan time is not a catch-up: the nightly run handles it. A catch-up that fails
(the Mac often boots before Docker or Postgres is ready) is retried every 15
minutes until it works or the next scheduled scan is due. A nightly run that
fails (it raised, or a step failed) is retried the same way: every 15 minutes
until one succeeds or the next scheduled scan is due. A nightly that finds
another scan running (**Run scan now**) checks again 15 minutes later and runs
only if that session is still not scanned.

The scheduler sleeps in chunks of at most 5 minutes and re-reads the wall clock
after each, so a Mac that wakes after the scan time runs the job within one
chunk. It never stops the API from starting: its errors are logged
(`swing.scheduler.*`, in `logs/api.log`) and it carries on. The one case that
ends it is a missing Alpaca key, which it records and Settings shows.

**Run scan now** (Today and Settings, `POST /v1/scan/run`) starts the same job
in the background and answers 202. While a job runs it answers 409 (*"a scan is
already running"*); with no Alpaca keys it answers 503 with the reason. The same
job from a terminal:

```bash
uv run python -m swing_engine job   # prints each step's status; exits 1 if a step failed
```

Only one job runs at a time **within one process**. The CLI `job` is a separate
process with its own lock, so running it while the API's scheduler happens to be
mid-job is not prevented (a Postgres advisory lock is on the deferral ledger).
The digest's once-per-session record limits the damage to a narrow race.

**Not running the API all day.** The scheduler only works while the API is up.
If you shut the lid and open it next morning, start (or leave running) the API
and it catches up on its own. Without the API, use the manual rows below.

### Telegram digest

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see `.env.example`), then check
it with **Settings, Telegram, Send test message**. The digest:

- lists **validated ideas only**: an idea whose setup is Live (passed out-of-sample
  testing). Unproven ideas are never listed, and neither are watch-only names.
  With both setups currently failed (Research only), a digest therefore says
  that nothing validated is waiting and how many unproven ideas are on the
  dashboard;
- goes out **once per session**, with at most five ideas, never a stock you
  already hold, never an idea that sizes to zero shares, and nothing to buy in a
  downtrend. A catch-up lists the missed days (up to ten) in that one message;
- includes the market line, the nightly action for each of your positions, and
  ends with *Informational only, not financial advice. You place every order
  yourself.*;
- is followed, if the scan fails, by **one failure notice per session**
  (*"ATLAS scan failed at step X: reason"*). A send that does not go through (no
  bot configured, Telegram down) leaves the digest unsent so a later run retries
  it; the scan itself is not marked failed.

To turn it off: **Settings, Telegram digest, off** (this also stops the failure
notice). To remove the channel entirely, unset the two Telegram variables and
restart the API.

### Doing it by hand (no API running)

Only needed if you want to run the swing engine from cron instead of the API's
scheduler, or to repair data. Use either the scheduler or cron, not both.

| Job | Cadence | Command |
|---|---|---|
| **Swing first load** | once, before anything else | `uv run python -m swing_engine backfill` (resumable: re-run until it exits 0) |
| **Whole nightly job** | every trading day, 17:00 ET or later | `uv run python -m swing_engine job` (all six steps, catch-up included) |
| Swing daily update | every trading day after the close (allow 16+ minutes for the SIP delay) | `uv run python -m swing_engine update` |
| Swing asset refresh + catch-up | weekly | `uv run python -m swing_engine backfill --refresh-assets` (re-fetches the active and inactive asset lists, loads new listings, flags delistings, and catches up any symbol more than 30 sessions behind; a plain `backfill` reuses the stored list and `update` never discovers new listings) |
| Swing scan (CLI only) | right after the update succeeds | `uv run python -m swing_engine scan` (stored bars only, default account, no tracking, positions or digest) |

`update` and `scan` are safe to run on weekends and holidays: both work from
the last completed session. `backfill` and `update` exit with status 1 when
some symbols failed, so chain the scan on success (`update && scan`) and
re-run `update` to resume. Run `update` before `scan`; the scan never fetches.

Validation is **not** a scheduled job. `insample` and `oos` are run by hand, with a
git commit of the pre-registration in between (see the README's Swing engine
section). `oos` spends one of a family's 3 out-of-sample runs. Both v1 setups
have spent one and failed (see the README's "Where the setups stand").

### Legacy refreshes (cron)

| Job | Cadence | Command |
|---|---|---|
| Bar backfill (REST, legacy 1m) | 1x daily off-hours | `uv run python -m ingest_equities backfill --symbols … --days 1` |
| Macro snapshot (legacy) | every 1h | `uv run python -m macro_engine.refresh` |
| News pull (legacy) | every 5m | `uv run python -m news_ingest.refresh --source rss` |
| Model retrain (legacy, optional) | weekly | `uv run python -m quant_engine.train --symbols … --version vN` (use a **new** `N`; point `ATLAS_TREND_MODEL` at it) |

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
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | (a) The swing engine's nightly digest and failure notice (see "Telegram digest"; the on/off switch is in Settings); (b) legacy: alerts can use the `telegram` channel — body rendered in BLUEPRINT §12.3 layout (🚨 header, trade plan, invalidation, disclaimer) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO` | Alerts can use the `email` channel |
| **`ATLAS_ENABLE_AUTO_EXECUTION=1`** | **Starts the 10s position-monitor loop** that auto-closes positions on stop / target / time. **Off by default — BLUEPRINT §22 #6 non-negotiable.** The log line `monitor.disabled` at startup confirms it's off; `monitor.auto_execution_enabled` (warning) confirms it's on. |

### Process switches

| Env | Default | Effect |
|---|---|---|
| `ATLAS_DISABLE_SCHEDULER=1` | scheduler **on** | The API starts without the after-close scheduler and its start-up catch-up. **Run scan now** and `python -m swing_engine job` still work, and Settings says *"Automatic scans are off: ATLAS_DISABLE_SCHEDULER=1 is set."* Evaluated when the API starts, after `.env` is loaded, so it may live in `.env` or in front of `scripts/atlas.sh up`. The scheduler never starts under pytest. |
| `ATLAS_ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` | Comma-separated `Host` names the API answers; any other gets 400. Read from the shell environment when the app is built (at import, before `.env` is loaded), so set it in front of `scripts/atlas.sh up` if you need another name. |
| `ATLAS_ENABLE_AUTO_EXECUTION=1` | **off** | The legacy position monitor (see the row above). A BLUEPRINT §22 non-negotiable: leave it off. The swing engine never places or changes an order whatever this is set to. |
| `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` | **off** | See below. |
| `ATLAS_ALLOW_LEGACY_MODEL=1` | **off** | See below. |

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
| Stop the automatic nightly scan | Start the API with `ATLAS_DISABLE_SCHEDULER=1` (`ATLAS_DISABLE_SCHEDULER=1 scripts/atlas.sh up`, after `scripts/atlas.sh down`). Settings then shows the scheduler as off and why; **Run scan now** still works. |
| Stop the Telegram digest and failure notices | **Settings, Telegram digest, off** (no restart). To remove the channel, unset `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` and restart. |
| Stop everything | `scripts/atlas.sh down` (API, dashboard, containers; data is kept). The swing engine places no orders, so there is nothing to cancel at a broker. |
| Stop a scan that is running | Not supported from the UI. `scripts/atlas.sh down` ends it; the next `up` catches up and re-runs the session safely (scans are upserts, one digest per session). |
| Stop firing new alerts (legacy) | `UPDATE alert_rules SET enabled = FALSE WHERE …` (or DELETE in `/alerts`). |
| Stop auto-executing trades | **Unset `ATLAS_ENABLE_AUTO_EXECUTION` and bounce the API.** The monitor loop is now opt-in (BLUEPRINT §22 #6) so this is also the *default* state. |
| Lower the dev tier below Elite | Via `/settings/legacy` (production: revoke `broker_autotrade` on the user). Blocks the `/v1/execute` route. |
| Pause the position monitor (without restart) | Not currently supported — bouncing the API is the path. The loop cancels cleanly in lifespan teardown. |
| Stop WS live push | Drop the Prometheus alert `SignalServiceDown` cascading, or firewall `:8000`. |
| Force every signal through the templated rationale | Unset `DEEPSEEK_API_KEY` and bounce. The §10.3 payload shape stays identical — only `payload.source` changes from `deepseek-chat` to `templated`. |

## Common one-off recoveries

| Symptom | Fix |
|---|---|
| Today says *"Can't reach the ATLAS API. Start it with scripts/atlas.sh up."* | The API is not running, or Postgres is down. `scripts/atlas.sh status` shows which; `scripts/atlas.sh up` starts both. |
| `scripts/atlas.sh up` says *"Docker isn't running"* | Start Docker Desktop, wait for it to say it is running, and run `scripts/atlas.sh up` again. |
| `scripts/atlas.sh up` says a port is *already in use* | Another program holds port 8000 or 3000 (often an API or `pnpm dev` started by hand). The script never stops a process it did not start: stop that one yourself (`lsof -ti tcp:8000 -sTCP:LISTEN`), then run `up` again. |
| `scripts/atlas.sh up`: *"The API didn't start; see logs/api.log"* | Read the 20 lines it printed. Common causes: the database is not migrated (`uv run python -m atlas_shared.migrate up`), a bad `.env` value, or a first start that is just slow (run `up` again; it waits for the process it already started). |
| Today says *"First-time setup: downloading 10 years of daily data (N%)"* for a long time | Expected on the first run: the free Alpaca plan's rate limit sets the pace. If the percentage stops moving, check `logs/api.log` and that the Alpaca keys are in `.env` (`scripts/atlas.sh status` reports them as yes or no, and shows the download as *Downloading prices*). It resumes after a restart. Before the download starts Today says *"First scan running: …"* (the step it is in), and with no run at all *"Waiting for the first scan to start."*, plus why automatic scans are off when they are. |
| A stock page says *"ATLAS is still loading market data…"* | The API builds its market-data cache in the background after it starts (about a minute on a full universe); the page asks again every 10 seconds. Meanwhile Positions shows your positions without their latest close and R, and a position you record or edit is checked by the next nightly scan. |
| Today warns *"Prices were missing for N of M stocks …"* | Tonight's bars arrived incomplete (an update stopped half way, or the provider had an outage). The scan was saved but the digest waits. The failed nightly is retried every 15 minutes; or press **Run scan now** once the data is back. |
| Today says *"Last scan failed at step X: …. Showing the previous scan."* | Settings shows the steps and the reason. Fix the cause (Alpaca keys or rate limit for `assets` / `bars`, the database for the rest), then press **Run scan now**. Re-running is safe. |
| Settings says automatic scans are off | `ATLAS_DISABLE_SCHEDULER=1` is set (the panel says so), or the Alpaca keys are missing so the scheduler could not start. Fix it and restart the API. |
| **Run scan now** answers *"a scan is already running"* | A job is in progress (the scheduler or an earlier click). Wait for it; Settings shows its steps. |
| No Telegram digest arrived | In order: Settings, Telegram digest is on; `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set (status shows Telegram yes); Send test message works; the digest step of the last job shows `sent` (a failed send leaves it for the next run to retry). Also expected: with no Live setup the digest only says nothing validated is waiting. |
| Every idea on Today is under *"Unproven ideas: these setups have not passed testing"* | Expected at present: both v1 setups failed their out-of-sample test (Track record shows the numbers). It changes only when a new setup version passes its single out-of-sample run. |
| Dashboard sidebar empty (legacy) | `GET /v1/watchlist` returned the hardcoded fallback (AAPL/MSFT/NVDA/TSLA/SPY/QQQ — equities-only since 0.23). Apply migration `0005`, hit `/settings/legacy` → Save. |
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
| `backfill` / `update` exit 1 with *"failed, re-run to resume"* | Some chunks still failed after the client's retries (rate limits, 5xx, dropped connections and timeouts are all retried), or Alpaca rejected a symbol (a chunk rejected with 400/404/422 is split until only the bad symbols fail; those stay listed). Re-run the same command: it only fetches what is missing, and stored chunks stay stored. |
| Any `swing_engine` command ends with a `hint:` line | An environment problem, not a bug. *"is the database running?"*: start Postgres (`docker compose -f infra/docker/docker-compose.yml up -d`) and check `POSTGRES_DSN`. *"the database schema is missing or behind"*: `uv run python -m atlas_shared.migrate up`. *"could not reach Alpaca"*: check the network and re-run; the job resumes. |
| `update` prints *"refetched full history (adjustment moved)"* | A split or dividend rewrote that symbol's adjusted prices. Expected: the whole history was refetched so two price scales are never spliced. |
| `swing_engine oos` refuses to start | The pre-registration is missing, uncommitted, changed since commit or hand-edited; it has no protocol block (written by an older engine) or seals other rules than `config.py` has now; it repeats an earlier version's parameters and rules; the version is not the in-sample choice; a revision has no `--hypothesis`; or the family's 3 out-of-sample runs are spent. Fix the cause it names (for example re-run `insample`, review and commit the draft) and try again. A spent budget cannot be retried until 12 months of new data accrue. |
| `swing_engine oos` says *"the data looks incomplete on <date>"* | The last stored session has bars for fewer than 95% of the symbols the session before had, so an update stopped half way. Run `uv run python -m swing_engine backfill --refresh-assets && uv run python -m swing_engine update`, then retry; nothing was spent. |
| `swing_engine insample` says *"... is identical to ...; a revision must change the rules"* | A revision must change the parameters or the sealed rules (for example the declared grid in `validation/protocol.py`); re-running the same rules would just be another look at the out-of-sample window. Nothing was written. |
| Every scan idea is flagged `UNPROVEN` | Expected until a family's latest report under `ml/validation/` passed, its pre-registration still matches the one that run used, and the rules it sealed are still the code's (a report whose failures say *"rules changed since its run"* needs a new `insample` / `oos` version). |
| API log shows `trend_model.legacy_refused`, `s_quant` is null | The model on disk predates the per-symbol label fix. Retrain with a new version and point `ATLAS_TREND_MODEL` at it (see Bring-up), or accept the refusal. |
| `/v1/stream` only ever pushes `regime.global` | By design: the 1-minute signal recompute is off. See "Legacy composite switches". |
| Telegram message looks plain / no emoji | Pre-0.23 format. Confirm the alert-service is running 0.23+ — the §12.3 layout includes the 🚨 header, trade plan, invalidation, and disclaimer automatically via `channels/base.py format_alert`. |
