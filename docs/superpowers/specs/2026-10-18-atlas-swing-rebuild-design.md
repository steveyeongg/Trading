# ATLAS swing rebuild: design

**Date:** 2026-10-18
**Status:** draft, awaiting approval
**Owner:** Steve Yeong

## 1. Intent

ATLAS should tell its user, after each US market close, **which US stocks are worth buying for a hold of days to weeks, at what price, with what stop, and how many shares**. It should do this without the user curating a watchlist, and it should say honestly how well each kind of trade has worked in the past.

Today it does none of that. The user's last experience was that it "did nothing". The 2026-10-18 audit, measured on real Alpaca data, found the reasons:

| Finding | Evidence |
|---|---|
| Nothing was running | Docker daemon off; no DB, Redis or API |
| Every symbol vetoed outside market hours | Swing stale limit is 30h; on a Sunday every bar is ~39h old |
| Swing signals computed from 1-minute bars | Routes, stream and screener default to `resolution="1m"`, `limit=500` (≈1.5 days of data) |
| Stops ~0.1% from entry on 10-day trades | Stops are built from 1-minute ATR |
| 96% of published signals were shorts | 3-week replay: 242 short / 10 long. `s_risk` (a tradeability check) returns −60 on 1m bars and is summed into the directional composite |
| The ML model has no edge | Training labels are computed on the timestamp-interleaved multi-symbol series (`quant_engine/data/bars.py` + `train.py`). The same method scores AUC 0.93 on pure random walks |
| "Confidence %" is not a probability | `|p_up − 0.5| × 2` from the leaky model; 73% of readings clear the gate |
| Volume indicators see ~2.4% of the tape | Ingest uses the IEX feed: AAPL 1.45M shares vs 60.1M on SIP for the same day |
| Daily bars are wrong | Built by UTC `time_bucket` over 1m IEX bars, including extended hours |

The research run on 6 years of daily bars for 43 large caps found that no signal, including `s_tech`, 12-1 momentum and 52-week-high proximity, ranked stocks with statistical significance (all t < 1). This sample is survivorship-biased and small, so it neither proves nor disproves edge in a broader universe. A trend filter on SPY/QQQ (hold above the 200-day SMA) cut max drawdown from −26% to −20% (SPY) and from −37% to −22% (QQQ) at a similar Sharpe. This is why the design below puts **measurement first** and treats every rule as a hypothesis that has to pass an out-of-sample test before it's shown as live.

## 2. What the user decided

- **Market:** US stocks.
- **Holding period:** days to weeks (swing).
- **No manual watchlist.** ATLAS should show a list of stocks on its own.
- **More intuitive.**
- **Unproven setups stay visible.** Ideas from setups that haven't passed validation still appear nightly, clearly labelled unproven, but never in the Telegram digest.
- **Rebuild per the audit's suggestions and best practice.** The user delegated the design choices; everything below not listed in this section is the designer's call and is marked *(designer's call)* where it matters.

## 3. Goals

1. **Daily ranked trade list, automatically.** After every trading day's close, scan a liquid US-stock universe and publish a ranked list of trade ideas for the next session.
2. **Every idea is complete and actionable:** buy-stop trigger price, initial stop, reference target, share count and dollars at risk computed from the user's account settings, plus a one-paragraph plain-English "why".
3. **An honest track record per setup type:** win rate, average win and loss in R, expectancy, trade count and drawdown, in-sample vs out-of-sample. Plus a live forward record that builds up automatically from every published idea.
4. **A market regime banner** that says plainly whether conditions favour new long trades.
5. **Position follow-up:** the user records trades they actually took; ATLAS updates trailing stops nightly and says what to do ("raise stop to X", "stop hit, exit", "time limit reached").
6. **It works when opened.** One command starts everything. The UI always shows the data date, last scan and next scan, and explains any empty state. Weekends and holidays show the last valid scan, not a blank page.
7. **Telegram digest** after each scan *(designer's call; the user already has Telegram configured)*.

## 4. Non-goals

- Intraday or day trading, minute bars in the signal path.
- Short selling *(designer's call: the user holds for days to weeks; in the research, bearish `s_tech` readings were followed by **rises**, not declines; shorting adds margin and borrow complexity)*. In a downtrend regime the engine says "stand aside" instead of suggesting shorts.
- Options, crypto, forex, futures.
- Automatic order placement or broker integration for orders. The user places orders themselves. The existing opt-in monitor stays off.
- ML, LLM, news, sentiment, macro (FRED) or synthetic-options inputs to the trade decision. The engine is rules-based. An ML ranker may come back later only if it beats the rules out of sample.
- Multi-user, tiers, billing.
- A native mobile app (Telegram covers mobile).

## 5. Anti-goals: it fails if any of these are true, even if it "works"

- An empty screen without a stated reason.
- A number labelled probability or confidence that isn't a measured historical frequency.
- Parameters tuned on the same data the results are reported on, or out-of-sample data peeked at more than once per setup version.
- Levels that look precise but are noise (stops inside a day's normal range).
- Any order sent automatically.
- Language promising profit. The existing §10.4 `safety_repair` rules and disclaimers stay.
- A setup shown as live when it failed validation.

## 6. Constraints

- **Cost:** free data only. Alpaca Basic, already configured, provides SIP historical bars with a 15-minute delay, history from 2016, active and delisted asset lists, and the trading calendar (`/v2/calendar`, `/v2/clock`). All of this was verified on 2026-10-18 with the user's keys.
- **Runs on the user's Mac:** Docker (TimescaleDB + Redis), `uv` Python workspace, `pnpm` Next.js. Keep the existing stack and the ADR-0001/0002 philosophy: single process, no new infrastructure.
- **Keep the §22 non-negotiables:** no auto-execution by default, no fake confidence, no stale data presented as live, disclaimers.
- **Keep the test suite green;** add tests for every rule. The `tests/test_docs.py` and `tests/test_cli_entrypoints.py` guards apply (SYSTEM.md rows, links, `__main__.py` for documented CLIs).

## 7. Design

### 7.1 Data

- **Source:** Alpaca `/v2/stocks/bars`, `timeframe=1Day`, `feed=sip`, `adjustment=all`. Request multiple symbols per call. End the request ≥ 16 minutes before now (SIP delay on the free plan).
- **Storage:** native `resolution='1d'` rows in the existing `bars` hypertable. Daily rows stop being derived from 1m via `time_bucket`. Each daily bar's `ts` is the session date (builder picks the timestamp convention and documents it).
- **Assets:** a new `assets` table from `/v2/assets` (active **and** inactive) with symbol, name, exchange, flags and a first-seen/last-active date. Inactive assets give the backtest a survivorship-free universe.
  - Delisted symbols return SIP daily history through their last trading day. Verified 2026-10-18: SIVB to 2023-03-09, plus FRC, BBBY, TWTR, ATVI, PXD and SPLK.
  - The builder measures coverage across all inactive assets and states it in the validation report. Ticker reuse is handled by giving each asset row a date range.
  - **Survivorship is a disclosure, not a blocker** *(designer's call)*. If coverage of delisted names in the candidate set is below 90%, the report says so and the ship gate's profit-factor floor rises from 1.2 to 1.3.
- **Backfill:** 2016-01-01 → now for the candidate set (§7.2) on first run, with progress shown in the UI. After that, a nightly incremental update.
- **Calendar:** use Alpaca's trading calendar for "last completed session", half days and holidays. Cache it.

### 7.2 Universe (point-in-time, recomputed each session)

- US-listed common stocks and ADRs on NYSE, NASDAQ, AMEX (and NYSE Arca/BATS listings that are operating companies). Exclude OTC, ETFs/ETNs/funds, warrants, units, rights, preferreds, and pre-merger SPAC shells as far as names, symbols and exchange let us tell. The builder defines the heuristics and tests them against known examples.
- Close ≥ $10; 50-day median dollar volume ≥ $20M; ≥ 252 sessions of history *(designer's call; expected ~1,000–1,800 names, which the builder measures and reports)*.
- **Reference series (not tradeable ideas):** SPY for the market regime and relative strength. Sector ETFs are optional context.

### 7.3 Market regime (from SPY daily)

| Regime | Rule | Effect |
|---|---|---|
| **Uptrend** | close > SMA200 **and** SMA50 > SMA200 | new ideas at full risk |
| **Mixed** | anything else | new ideas at half risk |
| **Downtrend** | close < SMA200 **and** SMA50 < SMA200 | no new buy ideas; banner says "stand aside"; strongest names listed as "watch only" |

Also show breadth (% of universe above its 50-day SMA) as information only. The regime rule is itself validated (§7.8). If the filter does not improve risk-adjusted results out of sample, it becomes a warning instead of a gate.

### 7.4 Leader filter (both setups)

- Close > SMA50 > SMA200, and SMA200 above its value 20 sessions ago.
- **Relative strength (RS) percentile ≥ 70** within the universe. RS = 0.4·r63 + 0.2·r126 + 0.2·r189 + 0.2·r252, where rN is the N-session return *(designer's call; the IBD-style weighting is a predeclared default, not tuned)*.
- Close within 25% of the 52-week high.

### 7.5 Setups (defaults are predeclared hypotheses; §7.8 decides what ships)

All orders are **next-session buy-stops**, valid for one session only. The idea is generated after close on day *t*; the trigger lives on day *t+1*.

**A. Breakout from a tight base**
- Tonight: close within 2% of the 50-session closing high, and ATR10 / ATR50 ≤ 0.85 (volatility contraction).
- Trigger: 50-session high + 0.1·ATR14.
- Initial stop: trigger − 2·ATR14.

**B. Pullback in an uptrend**
- Tonight: RSI(2) ≤ 10, or close ≤ EMA20, while passing the leader filter.
- Trigger: today's high + 0.05·ATR14 (buyers must show up).
- Initial stop: lowest low of the last 5 sessions − 0.25·ATR14, clamped to 1–3 ATR14 below the trigger.

**Exits (both setups):**
- Initial stop.
- Chandelier trail: highest close since entry − 3·ATR14, never lower than the initial stop. Updated nightly.
- Maximum hold of 30 sessions.
- +2R is shown as a **reference target only**, not an exit rule, unless a partial-exit variant is validated.

**Fill model (backtest and forward tracking identical):**
- Fill if high(t+1) ≥ trigger, at max(open, trigger) plus 10 bps slippage.
- Skip if the open gaps above the trigger by > 1·ATR14 (don't chase).
- Stop exits at min(open, stop) minus 10 bps (realistic gap-through).
- If a day's range spans both the stop and a new high, assume the stop was hit first.
- Commission $0.

### 7.6 Ranking

Rank tonight's triggered ideas by RS percentile, descending. Show the top 20 by default; the full list is available with filters (setup type, price range, distance to trigger). The ranking itself must be validated: within each setup, top-RS-quintile ideas must show higher out-of-sample expectancy than bottom-quintile ones. If they don't, rank by a simpler measure the builder can justify, and document it.

### 7.7 Risk and sizing

Settings, editable in the UI and stored in the DB:
- Account equity (default $25,000 *(designer's call)*).
- Risk per trade (default 1% of equity).
- Max position value (default 20% of equity).
- Max open positions (default 8).
- Max total open risk (default 6% of equity).

Calculations:
- Shares = floor(equity × risk% × regime factor / (trigger − stop)), capped by max position value.
- Show dollars at risk and position value.
- **Overlap flag:** if an idea's 60-session daily-return correlation with any open position is > 0.8, mark it "overlaps with X". Sector data isn't free, and correlation needs none.

The backtest is a **portfolio simulation** with these same rules: max positions, a total risk cap, and entries filled by rank when capacity is short. It is not only a list of independent trades.

### 7.8 Validation protocol (the gate between "hypothesis" and "live")

- **Data:** SIP daily 2016-01 → latest, point-in-time universe including delisted symbols.
- **In-sample 2016-01 → 2022-12:** any tuning happens here, over a small grid declared in code before running (for example stop 1.5/2/2.5 ATR and trail 2.5/3/3.5 ATR). Record the number of variants tried.
- **Out-of-sample 2023-01 → latest:** the chosen variant is run **once per setup version**. Report the deflated Sharpe ratio (already in `backtest_service.metrics`) given the variant count.
- **Baselines:**
  - SPY buy-and-hold over the same period.
  - Random entry: same universe, same number of entries per day drawn from leader-filter-passing stocks (so the setup trigger is what's being tested), same exits and sizing, 100 seeds.
  - Regime filter on vs off.
- **Ship gate, all of which must hold out of sample *(designer's call)*:**
  - Expectancy > 0R after costs.
  - Profit factor ≥ 1.2.
  - ≥ 100 trades.
  - Expectancy above the random-entry median.
  - Portfolio maxDD no worse than SPY's over the same window.
  - A setup that fails, or hasn't been validated yet, is **Research only** *(user decided)*:
    - It is still scanned, sized and forward-tracked nightly.
    - Its ideas appear on Today in a separate section headed **"Unproven ideas: these setups have not passed testing"**, styled distinctly from validated ideas.
    - Its ideas are never included in the Telegram digest.
- **How the gate is computed** *(designer's call)*:
  - **Trade-level stats** (expectancy, profit factor, trade count, random-entry comparison) use **every triggered idea**, independent and uncapped. This is the same basis as the live forward record in §7.9, so the two sit side by side fairly.
  - **Portfolio stats** (CAGR, Sharpe, maxDD vs SPY) come from a capacity-limited simulation **per setup alone**, under the §7.7 caps.
  - A **combined** simulation (both setups sharing capacity, the way it runs live) is reported for information and is not part of the gate.
- **Pre-registration:** before any out-of-sample run, the chosen variant (parameters) and the gate thresholds are written to a committed file (for example `ml/validation/<setup>/<version>.prereg.json`). The out-of-sample run refuses to start without it, and checks that the file is committed and unchanged.
- **Out-of-sample budget** *(designer's call)*:
  - At most **3 out-of-sample runs per setup family**: version 1 plus up to 2 revisions.
  - Each revision needs a written hypothesis in its pre-registration file, drawn from in-sample analysis only.
  - Every out-of-sample run counts toward the deflated-Sharpe trial count.
  - Every version, failed ones included, appears on the Track record page.
  - Once the budget is spent, a family stays Research only until enough new data accrues for a fresh out-of-sample window (≥ 12 months after its last freeze).
- **Verdict lifecycle** *(designer's call)*:
  - Each version's out-of-sample window is **frozen** at its run: 2023-01-01 → the freeze date. The report is committed together with a data fingerprint (symbols, date range, row-count hash).
  - Data after the freeze date counts only toward the live forward record (§7.9).
  - A fresh install **reads the committed report** and does not re-run validation. A CLI re-runs it on demand.
  - Verdicts never change automatically.
  - Track record shows a **"live underperforming" warning** if a Live setup's forward expectancy is below 0R after ≥ 30 closed ideas.
  - It shows a **"validated with default universe filters; yours differ" note** when Settings changes the universe filters. Sizing settings don't change per-trade R stats, so they need no note.
- **Output:** a versioned JSON report per setup stored in the repo/DB. It holds IS and OOS stats, by-year breakdown, trade count, win rate, avg win R, avg loss R, expectancy R, profit factor, avg hold, portfolio CAGR/Sharpe/maxDD, and the baselines. One CLI command regenerates it reproducibly.
- **No-lookahead tests:** for any date *t*, a scan must be byte-identical when bars after *t* are deleted or randomised.

### 7.9 Forward tracking (the live track record)

Every published idea is stored, idempotent on (session, symbol, setup). Each night the same fill and exit rules are applied to the new bar, so every idea moves through *not triggered → open → closed (reason, R)*. The Track record page shows live numbers next to the backtest's OOS numbers. This is the honest answer to "does it work?", and it accumulates with no user effort.

**Live means published on time** *(designer's call)*:
- An idea counts toward the live record only if it was stored before 09:30 ET of the session it targets.
- Ideas rebuilt during catch-up (§7.11) are flagged `reconstructed`, tracked the same way, and reported in a separate column. They never count as live.
- Research-only setups are scanned and forward-tracked nightly like any other, so they also build a live record.

### 7.10 The user's real positions

The user records a position: symbol, fill date, price and shares, optionally linked to an idea, with the stop pre-filled from the idea. Each night ATLAS applies the trail and time rules and produces actions: *hold*, *raise stop to $X*, *stop hit, exit*, *max hold reached*. Closing a position records the exit price; the closed-trade history shows R-multiples. This replaces the current Portfolio and Journal pages for this purpose.

### 7.11 Nightly job and operations

- **Schedule:** runs inside the API process on trading days at 16:45 ET (after the SIP delay; half days adjusted from the calendar). It also runs on startup if the last completed session hasn't been scanned (catch-up), plus a manual "Run scan now" button and CLI. The builder picks the scheduling mechanism.
- **Steps:**
  1. Refresh assets (weekly).
  2. Incremental bar fetch.
  3. Universe.
  4. Regime and breadth.
  5. Indicators.
  6. Setups.
  7. Rank and size.
  8. Store the scan snapshot.
  9. Update forward-tracked ideas and user positions.
  10. Telegram digest.
- **Catch-up** *(designer's call)*: if the Mac was off or asleep, catch-up replays each missed session in order. This is safe because scans are point-in-time and lookahead-free. It updates ideas, forward tracking and position actions session by session, flags rebuilt ideas `reconstructed`, and sends **one** digest for the latest session that lists the missed sessions.
- **Failure handling:** each step logs and records status. A failed step leaves the previous scan visible, with a clear "last scan failed at step X: reason" banner.
- **Freshness is calendar-based:** data is current if it includes the last completed session. The fixed 30-hour veto does not apply to this path.
- **One command** (`scripts/atlas.sh up` or similar) checks that Docker is running and says how to start it if not, then runs compose up, migrations and first-run backfill (resumable), and starts the API and web. `down` and `status` subcommands too.
- **Stop the old background noise:** the WebSocket broadcaster's every-15s recomputation of 1m composite signals, and alert evaluation on them, is **disabled by default**. It would otherwise send nonsense Telegram alerts. The regime push can stay.

### 7.12 API (sketch; the builder finalises the shapes)

```
GET  /v1/today                 regime, breadth, freshness, ranked ideas, position actions
GET  /v1/stocks/{symbol}       daily bars + indicators, eligibility checklist (pass/fail per rule, with values), current idea if any
GET  /v1/track-record          per-setup validation report + live forward stats
GET  /v1/positions             POST  PATCH /{id}  POST /{id}/close
GET  /v1/settings              PUT
POST /v1/scan/run              manual trigger (idempotent per session)
GET  /v1/system/status         data as-of, last/next scan, backfill progress, step statuses, provider config
```

### 7.13 Dashboard

Keep the existing dark theme tokens (`bg`, `ink`, `bull`, `bear`, `accent`) and lightweight-charts. Use plain-English labels everywhere, tooltips for terms (R, ATR, RS), and no § references, sub-score jargon or tier badges in the primary views.

Navigation: **Today · Positions · Track record · Settings**, plus a header search box, "Check a stock".

**Today**
```
┌────────────────────────────────────────────────────────────────────────────┐
│ MARKET: UPTREND, new trades OK      Breadth 61% above 50-day               │
│ Data as of Fri 16 Oct close · ideas are for Mon 19 Oct · next scan Mon 4:45pm ET │
├────────────────────────────────────────────────────────────────────────────┤
│ YOUR POSITIONS: 2 actions                                                  │
│  MSFT  raise stop to 512.40 (trail)        NOW  hold, stop 128.10          │
├────────────────────────────────────────────────────────────────────────────┤
│ TRADE IDEAS FOR NEXT SESSION (14)        filter: [All setups ▾] [$10+ ▾]   │
│ #  Stock         Setup      Buy above  Stop    Shares  Risk $  Strength  Track record   │
│ 1  XYZ  Xyz Inc  Breakout   148.20     140.10  30      243     98        52% win · +0.4R · 312 trades │
│ 2  ABC  Abc Corp Pullback   72.15      69.30   87      248     95        48% win · +0.3R · 540 trades │
│ …  (row click → stock page; "overlaps with MSFT" / "earnings soon" badges)  │
├────────────────────────────────────────────────────────────────────────────┤
│ ⚠ UNPROVEN IDEAS: these setups have not passed testing (6)   [collapse ▴]  │
│ same columns, muted styling, "Unproven" tag on every row; track record     │
│ column shows "failed: profit factor 1.05 < 1.2" or "not yet tested"        │
└────────────────────────────────────────────────────────────────────────────┘
```
If no setup is Live, the validated section says "No setup has passed testing yet" and the Unproven section is expanded by default.
Empty states always say why, for example "Market in downtrend: no new buy ideas (stand aside). Strongest names below are watch-only." or "First-time setup: downloading 10 years of daily data (38%)".

**Stock page:** daily candles with SMA50/200 and EMA20; trigger, stop, 2R and trail lines; a plain-English "why this setup" and "what cancels it"; an eligibility checklist (✓/✗ per rule with the actual values); the setup's track record. For any stock that doesn't qualify, the checklist shows exactly which rules failed. This replaces "gate reasons".

**Positions:** open positions with the latest action, current R and the stop history; an "I bought this" button from any idea; closed trades with R and hold time.

**Track record:** per setup, IS vs OOS vs live forward tables, an equity curve versus SPY, the random-entry baseline distribution, and a validation verdict ("Live" or "Research only: failed X").

**Settings:** account equity, risk %, caps, universe filters (min price, min dollar volume), Telegram digest on/off with a "send test", and the system status panel.

### 7.14 Telegram digest

Sent after each scan:
- Regime line.
- Top 5 ideas from **Live setups only**: symbol, setup, buy-above, stop, shares, $ risk. If no setup is Live, the digest says "No validated setups yet; N unproven ideas on the dashboard" and lists none of them *(user decided)*.
- Position actions.
- Disclaimer.

Use the existing channel code, including `redact_secrets`. Send nothing on a failed scan except one failure notice.

### 7.15 What happens to the existing system

- **Reused:** `feature_engine` indicators (verify each needed indicator's formula on daily bars), `backtest_service.metrics` (Sharpe, deflated Sharpe, profit factor, expectancy), `alert_service` Telegram channel, `atlas_shared` infra, `ingest_equities` store patterns, the `PriceChart` component, the explanation engine's templated narration (deterministic text; DeepSeek optional to rephrase, never to decide).
- **Retired from the default UI and the nightly flow (code kept):** the 8-engine composite, sub-score bars, ML trend model, news/sentiment/macro/options scoring, scanner page, tier switcher, watchlist-driven sidebar, and the old 1m `/v1/signals` path. *(Designer's call: keep the code because the repo also serves as a portfolio piece; the user may prefer deletion, see §10.)*
- **Fixed even though retired**, so nobody re-trains or re-trusts it by accident: the quant label leak (label per symbol before stacking; add a regression test that a random-walk dataset scores AUC ≈ 0.5), and mark `ml/registry/trend/v1.joblib` invalid (not loaded by default).
- **Docs:** README quick start becomes the one command; SYSTEM.md gets the new flow and packages; CHANGELOG entry; ADR-0003 records the pivot (rules-based daily swing engine, validation-gated) and why the composite was retired.

### 7.16 Build order and checkpoint *(designer's call)*

1. **Phase 1, data and engine:** SIP daily ingest, assets, calendar, universe, regime, setups, sizing, fill and exit model, and the validation harness with in-sample results. Fix the old pipeline's leak and background noise.
2. **Checkpoint:** in-sample results and the pre-registration files go to the user before the first out-of-sample run. The user can veto a variant; otherwise the run proceeds. Out-of-sample results are reported as they come, pass or fail.
3. **Phase 2, product:** nightly job and catch-up, forward tracking, positions, API, the four dashboard pages, Telegram digest, one-command startup, docs.

Phase 2 is built whatever the validation outcome. Every outcome needs a UI, and how failed setups appear on it is decided in §10.

## 8. Success criteria

1. From a cold machine with Docker installed, one command leads to a populated Today page, or an explicit progress or failure state, without manual data commands.
2. On a weekend, Today shows Friday's scan labelled for Monday's session.
3. Every idea shows trigger, stop, shares and $ risk from the user's settings, and its setup's track record.
4. `track-record` regenerates reproducibly from one CLI command. Only setups that passed §7.8 show as Live.
5. Forward tracking and position actions update automatically after each scan.
6. A Telegram digest arrives after each trading day's scan.
7. Tests cover: universe filters, regime, each setup rule, the fill and exit model (gaps, same-day stop/high), sizing caps, no-lookahead, calendar freshness (weekend, holiday, half day), idempotent scans, and the leak regression. The existing suite stays green or changes deliberately.

## 9. Left to the builder

- Package layout. A new `apps/swing-engine` (or similar) is suggested so the old engines stay untouched.
- Table schemas and migration numbering (0009+).
- Scheduler mechanism.
- Asset-type heuristics.
- Timestamp convention for daily bars.
- Exact API shapes.
- Chart overlays.
- Test strategy details.
- Whether the API and web also run in Docker.
- Backfill batching and rate-limit handling (free plan ≈ 200 requests/min).
- Earnings dates: free sources need new keys. If the builder adds one, show an "earnings within 10 sessions" badge; without one, show nothing rather than a guess.

## 10. Open questions for the user

1. **If no setup passes validation, what should Today show?** *Decided 2026-10-18:* show Research-only ideas nightly, clearly labelled unproven, and keep them out of the Telegram digest. Applied in §2, §7.8, §7.13 and §7.14.

No open questions remain.

## 11. Designer's calls the user may want to override

- Long-only. No shorts in a downtrend; the engine says "stand aside" instead.
- Universe: price ≥ $10, 50-day median dollar volume ≥ $20M, ≥ 1 year of history.
- Default settings: $25,000 account, 1% risk per trade, max 20% in one position, max 8 positions, max 6% total open risk.
- Two starting setups (breakout from a tight base, pullback in an uptrend), exits by 3-ATR trailing stop and a 30-session maximum hold.
- Ship gate: out-of-sample expectancy > 0, profit factor ≥ 1.2, ≥ 100 trades, beats random entry, drawdown no worse than SPY. At most 3 out-of-sample attempts per setup family.
- Old engine code (8-engine composite, ML model, news/macro/options scoring) is hidden from the UI and nightly flow, not deleted, because the repo doubles as a portfolio piece.
- Telegram digest on by default.
