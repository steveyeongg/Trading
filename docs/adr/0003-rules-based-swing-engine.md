# ADR-0003: A rules-based, validation-gated daily swing engine replaces the composite as the primary path

**Status:** Accepted
**Date:** 2026-10-19
**Deciders:** Steve Yeong

## Context

ATLAS was built to the BLUEPRINT: 1-minute bars feed 50 indicators, an XGBoost trend model and seven other sub-scores, which are summed into a weighted composite, gated, risk-planned and pushed over a WebSocket. Its user wants something narrower: after each US close, **which US stocks are worth buying for a hold of days to weeks, at what price, with what stop, and how many shares**. Their last experience was that the system "did nothing".

The 2026-10-18 audit, run on real Alpaca data, found why (full table in `docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md` §1):

| Finding | Evidence |
|---|---|
| Swing signals were computed from 1-minute bars | Routes, stream and screener default to `resolution="1m"`, `limit=500`, about 1.5 days of data. Stops were built from 1-minute ATR, so they sat about 0.1% from entry on 10-day trades. |
| The ML model has no edge: its labels leak | Triple-barrier labels were computed on the timestamp-interleaved multi-symbol series, so each row's "next 20 bars" were other tickers' prices and the model learned each ticker's price level. The same method scores AUC 0.93 on pure random walks. |
| "Confidence %" was not a probability | It was `\|p_up − 0.5\| × 2` from that leaky model, and 73% of readings cleared the gate. |
| 96% of published signals were shorts | A 3-week replay gave 242 short and 10 long. `s_risk`, a tradeability check, returns −60 on 1-minute bars and was summed into the directional composite. |
| Every symbol was vetoed outside market hours | The swing stale-data limit is 30 hours. On a Sunday every bar is about 39 hours old. |
| Volume indicators saw about 2.4% of the tape | Ingest used the IEX feed: AAPL 1.45M shares against 60.1M on SIP for the same day. |
| Daily bars were wrong | They were built with a UTC `time_bucket` over 1-minute IEX bars, extended hours included. |
| Nothing was running | The Docker daemon was off, so there was no database, Redis or API. |

The research run on 6 years of daily bars for 43 large caps found **no signal that ranked stocks with statistical significance**. This covered `s_tech`, 12-1 momentum and 52-week-high proximity, and all t-statistics were below 1. The sample is small and survivorship-biased, so it neither proves nor disproves an edge in a broader universe. The one robust finding was a trend filter: holding SPY or QQQ only while above its 200-day SMA cut max drawdown from −26% to −20% (SPY) and from −37% to −22% (QQQ) at a similar Sharpe.

Together these say that tuning or repairing the composite would be polishing a pipeline whose inputs (1-minute IEX bars), its only learned component (the leaky model) and its headline number (confidence) were all unsound, with no evidence that any signal in it works.

## Decision

**Build a separate rules-based daily swing engine (`apps/swing-engine`) and make it the primary path. Every rule is a hypothesis that must pass a pre-registered out-of-sample test before it is shown as Live.** The composite is retired from the default path; its code is kept.

### The engine

- **Daily bars only.** Alpaca `/v2/stocks/bars`, `timeframe=1Day`, `feed=sip`, `adjustment=all`, ending at least 16 minutes in the past. The free plan allows this and provides history from 2016, the active and delisted asset lists and the trading calendar. Daily rows are native `resolution='1d'` rows in the existing `bars` hypertable, and `ts` is the session date at 00:00 UTC.
- **Rules, not models.** US operating-company stocks only, long-only. Two setups, a volatility-contraction breakout and a pullback, each restricted to relative-strength leaders in a point-in-time universe, gated by a SPY trend regime. There is no ML, LLM, news, sentiment, macro or synthetic-options input to the trade decision.
- **Complete, honest ideas.** Each idea is a next-session buy-stop with a trigger, an initial stop, a chandelier trail and a share count from fixed-fractional sizing. No number is labelled probability or confidence unless it is a measured historical frequency.
- **No order placement anywhere.** `ATLAS_ENABLE_AUTO_EXECUTION` stays off by default.
- **One fill and exit model.** The scan, the backtest and the random-entry baseline all call the same `simulate_trade`, so a backtested fill cannot differ from what nightly tracking would do. It is pessimistic where a daily bar cannot say what happened first, and it never reads past the session being processed.
- **Plumbing stays as it was.** Pure Python, one process, TimescaleDB, no new infrastructure (ADR-0001, ADR-0002). The only schema change is migration `0009_swing_assets`.

### Validation gate

- In-sample 2016-01 to 2022-12, where a small grid declared in code is searched. Out-of-sample 2023-01 onward, run **once per setup version**.
- Before any out-of-sample run, the chosen parameters and gate are written to a committed, sha256-sealed pre-registration. The run refuses to start if the file is untracked, changed since commit, hand-edited, or its parameters are not the in-sample choice.
- At most **3 out-of-sample runs per setup family**; every run counts toward the deflated-Sharpe trial count.
- Ship gate, all out of sample: expectancy above 0R after costs, profit factor at least 1.2 (1.3 if fewer than 90% of delisted stocks have data), at least 100 trades, expectancy above the median of 100 random-entry seeds, and portfolio max drawdown no worse than SPY's.
- A setup is **Live** only if its latest report passed *and* its pre-registration is still the sealed file that run used (the report records the sha256). Anything else is **Research only**: still scanned, but every idea carries an UNPROVEN flag.
- If the SPY regime filter does not help out of sample, it is demoted from a gate to a warning. If RS ranking does not help, ideas are ranked by closeness to the trigger instead.

### The price floor uses raw bars

The universe requires a close of at least $10. `adjustment=all` rewrites every historical price on each split, so a split-adjusted close understates what a stock actually traded at. A stock that split many times since reads under $10 in years it traded well above it: NVDA, for example, over 2016-2020. A floor on adjusted prices would therefore wrongly exclude exactly the later-split winners a momentum scan most wants, in every backtest year. The engine also stores unadjusted bars (`adjustment=raw`, `resolution='1d_raw'`) and uses them **only** for the price floor. Dollar volume, relative strength, indicators and fills stay on adjusted prices. Raw history is never rewritten by a corporate action, so it is append-only and needs no overlap check.

### What happens to the composite

- The 8-engine composite, the XGBoost trend model and the 1-minute signal path are **legacy**. The code, its API and its dashboard stay, but the swing engine is the recommended path to a trade idea.
- The label leak is fixed in training: labels are computed per symbol, and each artifact records its `label_scheme`. The signal service **refuses to load** any model without the per-symbol scheme (the existing `v0` and `v1`) unless `ATLAS_ALLOW_LEGACY_MODEL=1`, which is not recommended. A refusal leaves `s_quant` null.
- The WebSocket broadcaster no longer recomputes 1-minute signals or evaluates alert rules on them unless `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1`. It is read once at process start from the real shell environment, not from `.env`.

## Consequences

### Positive

- The signal now rests on settled daily SIP bars, not partial IEX minutes. Weekends and holidays have a well-defined "last completed session", so the stale-veto failure mode is gone.
- A null result is a legitimate, visible outcome. If neither setup passes the gate, both stay UNPROVEN and the engine says so, instead of publishing numbers that look precise.
- The look-ahead, split-splice, delisting, ticker-reuse and gap-through-stop failure modes each have a regression test (`tests/test_swing_*.py`). A scan is a pure function of a panel truncated at the scan date.
- Survivorship is measured, not hidden: the backtest universe includes delisted symbols, and the report states coverage.
- The legacy leak is closed and cannot silently come back: old models are refused and the 1-minute recompute is opt-in.

### Negative

- **No setup is Live today.** Until `insample` and `oos` have been run and their pre-registration and report committed under `ml/validation/`, every idea is UNPROVEN. The harness is built, the evidence is not yet in.
- The out-of-sample window is finite and the budget is small (3 runs per family). A family that spends it stays Research only until at least 12 months of new data accrue. This is deliberate friction.
- The trend-filter evidence comes from a small, survivorship-biased sample, so the regime gate is itself under test and may be demoted.
- Free data has limits: a 15-minute SIP delay (irrelevant for after-close use) and about 200 requests per minute, which paces the first full-market backfill.
- Two systems now coexist in one repository. The dashboard, API and many docs still describe the legacy composite until Phase 2 adds swing pages, forward tracking and the Telegram digest.

## Revisit when

- Out-of-sample results exist for both families. Record the verdicts in `ml/validation/`, and write a follow-up ADR if both fail.
- An ML ranker is proposed. It must beat the rules out of sample under the same pre-registration protocol. Until then, "no ML in the trade decision" stands.
- Shorts, other asset classes or intraday are requested. Each needs its own validation and its own ADR.
- Paid data (for example Polygon) is acceptable. The IEX/SIP distinction and the free-plan delay would then be reconsidered.
- The legacy composite has been dormant long enough that deleting it, rather than keeping it, is cheaper than maintaining it.
