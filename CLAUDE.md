# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ATLAS: a stock screener and trading signal engine. It is a Python `uv` workspace (17 packages under `apps/` plus `packages/shared-py`, 18 workspace members) and a Next.js 14 dashboard in `apps/web`. **The primary path is the rules-based daily swing engine, `apps/swing-engine`** (ADR-0003). The 8-engine composite, the ML trend model and the 1-minute signal path are **legacy**: kept, still behind the dashboard, but not the default way to get a trade idea. `AGENTS.md` (when present in your checkout) holds the original product brief, and `docs/architecture/BLUEPRINT.md` is the design target. The delivered system **deliberately deviates** from both, and `docs/adr/0001`, `0002` and `0003` explain why: pure Python, no Rust/Go, no Kafka/ClickHouse/Qdrant/K8s, no multi-agent LangGraph, and a validation-gated swing engine in place of the composite. Don't scaffold those pieces without the profiling evidence the ADRs require. `docs/architecture/SYSTEM.md` is the map of what actually exists, including a deferral ledger. The swing design is in `docs/superpowers/specs/2026-10-18-atlas-swing-rebuild-design.md` (plans beside it in `docs/superpowers/plans/`).

Code comments and docs cite BLUEPRINT sections as `§N.N` (for example `§8.5` confirmation gate, `§10.3` explanation JSON, `§22` non-negotiables). Look the section up in BLUEPRINT.md when you need the intent behind a rule.

## Commands

```bash
uv sync --dev                                   # install the whole workspace + dev tools
uv run pytest -q                                # full backend suite (~1 min, no Postgres/Redis needed; DB tests skip)
uv run pytest tests/test_composite.py -q        # one file
uv run pytest tests/test_risk.py::test_name -q  # one test
uv run ruff check <paths>                       # lint (ruff is not clean repo-wide yet; check what you touch)
uv run mypy <paths>                             # strict mode is configured in root pyproject.toml

# Local infra + schema (only needed to run the app, not the tests)
docker compose -f infra/docker/docker-compose.yml up -d   # TimescaleDB (pg16) + Redis
uv run python -m atlas_shared.migrate up                  # also: `status`

# Swing engine (primary path). Run from the repo root; backfill/update need ALPACA_API_KEY + ALPACA_API_SECRET
uv run python -m swing_engine backfill          # first run, resumable (SIP daily bars from 2016, active + delisted)
uv run python -m swing_engine backfill --refresh-assets  # periodic: re-fetch /v2/assets for new listings + delistings
uv run python -m swing_engine update            # nightly after the close; refetches a symbol whose adjustment moved
uv run python -m swing_engine scan [--as-of D]  # stored bars only: regime + ranked ideas
uv run python -m swing_engine insample --family breakout                       # grid on 2016-2022, drafts the prereg
uv run python -m swing_engine oos --family breakout --version breakout-v1     # the one out-of-sample test

# Legacy API on :8000. The old v0/v1 trend models are refused (leaked labels), so s_quant is null unless
# ATLAS_TREND_MODEL points at a model retrained with per-symbol labels (quant_engine.train --version v2)
uv run --package signal-service uvicorn signal_service.main:app --reload

# Dashboard on :3000 (also the `atlas-web` preview config in .claude/launch.json)
cd apps/web && pnpm install && pnpm dev
cd apps/web && pnpm typecheck      # tsc --noEmit; also `pnpm lint`, `pnpm build`
```

Data seeding CLIs (`swing_engine`, `ingest_equities backfill|synthetic`, `macro_engine.refresh`, `news_ingest.refresh`, `quant_engine.train`, `portfolio_service.seed`, `journal_service.seed`) and their flags are listed in README.md "Quick start" / "Swing engine" and `docs/runbooks/OPERATIONS.md`.

## Architecture

**One process, many libraries.** Despite the `*-service` / `*-engine` names, the apps are libraries. They are not deployed separately. `signal-service` (FastAPI) imports all of them and is the only HTTP or long-running process. The other packages expose functions plus `python -m` CLIs for batch jobs (ingest, refresh, train, seed).

**Swing engine (primary).** `apps/swing-engine/src/swing_engine/`, a library plus the `python -m swing_engine` CLI (no HTTP routes or dashboard pages yet; that is Phase 2). The flow, one module per step:

`data/alpaca.py` (SIP daily bars, assets, calendar) → `ingest.py` (resumable `backfill`, split-aware `incremental_update`, `sync_raw`) → `store.py` (`bars` rows with `resolution` `1d` adjusted and `1d_raw` unadjusted, plus the `assets` table from migration 0009) → `panel.py` (wide `Panel`) → `indicators.py` → `universe.py` (point-in-time eligibility) → `regime.py` (SPY uptrend/mixed/downtrend, breadth) → `leaders.py` (RS percentile, leader gate) → `setups.py` (`breakout-v1`, `pullback-v1` candidates) → `execution.py` (`simulate_trade`, the one fill/exit model) → `sizing.py` → `scan.py` (`build_context`, `run_scan`). `backtest/` (trades, stats, random-entry baseline, capacity-limited portfolio sim) and `validation/` (`protocol.py` windows, grid and ship gate; `prereg.py`; `run.py` in-sample/out-of-sample and the Live/Research-only verdicts under `ml/validation/`) build on the same functions. Frozen, versioned parameters live in `config.py`; change one and the setup gets a new `version`. Tests are flat `tests/test_swing_<area>.py`. DB tests carry `pytestmark = pytest.mark.skipif(not postgres_reachable(), ...)` with `postgres_reachable` from `swing_engine.testing`.

**Swing invariants (don't break these):**
- **Pure functions over a Panel truncated at the scan date.** `build_context` cuts the panel at the session before computing anything, so nothing after the scan date can reach an idea. A scan must be identical if later bars are deleted (`test_scan_is_lookahead_free`).
- **Indicators are per contiguous segment.** A gap of more than 10 sessions (`indicators.MAX_GAP`) starts a new segment, which handles ticker reuse and long halts. A missing day is never filled and yesterday's price is never carried into today's eligibility.
- **Raw closes only for the price floor.** `adjustment=all` rewrites history on every split, so the $10 floor reads `1d_raw` bars; dollar volume, RS, indicators and fills stay on adjusted prices. Incremental updates must refetch a symbol's full history when the overlap day moved, never append across two price scales.
- **One fill/exit model.** Scan, backtest and baseline all call `execution.simulate_trade`. Don't add a second path.
- **The validation gate.** Every setup version is pre-registered (a committed, sha256-sealed `ml/validation/<family>/<version>.prereg.json`) before its out-of-sample run, which happens once per version with a budget of 3 per family. The prereg seals the setup params **and** a `protocol` block (`validation.protocol.protocol_block`: `LeaderParams`, `UniverseFilter`, `ExecutionParams`, the gate's `RiskSettings`, `REGIME_FACTOR`, the family's `GRIDS` entry). A family is **Live only if its latest report passed, the sealed prereg sha equals the report's `prereg_sha256`, and the prereg's protocol block equals the current config**; anything else is Research only and every idea is flagged `UNPROVEN`. So changing any of those defaults in `config.py` demotes a Live family until a new version is validated. A revision identical to an earlier version (params without `version`, and protocol) is refused. The gate's 100-trade floor reads `n_distinct` (trades entered while no earlier trade on the symbol was open); expectancy and profit factor use every triggered idea. In-sample tuning uses the declared grid only; the out-of-sample window is never peeked at, and the combined portfolio includes the other family only after its own out-of-sample report.
- **Honest labels.** Long-only, no order placement, no ML/LLM in the trade decision. Never call a number "confidence" or "probability" unless it is a measured historical frequency, and no profit-promising language.

**Legacy signal pipeline.** This is the 8-engine composite, kept for the dashboard and API; the swing engine replaced it as the default path. It lives in `apps/signal-service/src/signal_service/pipeline.py` (`run_pipeline`). It is a pure function with no DB writes. Callers (`routes.py`, `screener.py`, `stream.py`) gather its inputs first: bars from TimescaleDB via `ingest_equities.store`, the macro snapshot from Redis via `macro_engine.refresh.load_snapshot`, and news/sentiment features from Postgres via `news_ingest.retrieval`. The flow is:

bars → `feature_engine.compute_features` (50 indicators) → `quant_engine.TrendModel.predict_full` (XGBoost + isotonic calibration; `p_up` and `feature_health`) → `scoring_engine.generate_signal` (sub-scores, §8.2 weighted composite, §8.5 confirmation gate) → `risk_engine.RiskEngine.build_plan` (stops, sizing, news veto) → `explanation_engine` (DeepSeek JSON, or a templated fallback with the same schema).

`PipelineResult` has exactly three terminal states: a published `signal`, a risk `veto`, or `signal=None` with a `no_signal_reason`. **Every non-publish path must carry a human-readable reason.** `/v1/signals/{symbol}/debug` and the dashboard surface it. Gated candidates still carry `composite_score`, `direction_implied` and `sub_scores` so that alert rules keyed on raw composite can fire.

**Legacy guards.** `signal_service.state.load_trend_model` refuses any trend artifact whose `label_scheme` is not `per_symbol_v2` (the old `v0`/`v1` were trained on cross-ticker leaked labels, AUC 0.93 on random walks) and leaves `s_quant` null, unless `ATLAS_ALLOW_LEGACY_MODEL=1` (read per call, not recommended). The WS broadcaster does not recompute 1m signals or evaluate alert rules unless `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1`, which is read once at import from the real shell environment, not `.env`. Tests: `tests/test_legacy_guards.py`.

**Composite weights** are in `scoring_engine/composite.py` `DEFAULT_WEIGHTS` (tech .30, quant .25, news/sent/macro .10 each, opt/liq/risk .05 each). They are **renormalised over the active engines**, so a missing input (no model, no news) drops out instead of counting as zero.

**Background loops** run in the `signal-service` lifespan (`main.py`):
- `stream.broadcaster`: pushes the regime over `/v1/stream`. Only with `ATLAS_ENABLE_LEGACY_SIGNAL_STREAM=1` does it also, every 3rd tick, recompute signals for subscribed symbols (offloaded to a thread) and run `alert_service.AlertEngine` on every candidate, gated or not.
- The position monitor (`execution_service.run_monitor_once`) auto-closes positions. It **only starts when `ATLAS_ENABLE_AUTO_EXECUTION=1`**. Keeping it off by default is a §22 non-negotiable, so never change that default.

**Shared package** `atlas_shared` provides settings (`get_settings`, lru-cached), the async SQLAlchemy engine (`db.session_scope`), auth (`ATLAS_AUTH_MODE=dev` trusts the `X-Dev-User`/`X-Dev-Tier` headers; `jwt` uses JWKS), tier entitlements (`free|pro|elite|quant|enterprise`, which gate routes like `/v1/execute` and watchlist size), Prometheus metrics, and the shared `ExplanationPayload` schema.

**Storage:** TimescaleDB (`bars` hypertable plus OLTP tables, with rows scoped by `user_id`; `resolution` is `1m` for the legacy path, `1d`/`1d_raw` for the swing engine, whose daily `ts` is the session date at 00:00 UTC), Redis (macro snapshot), joblib model files in `ml/registry/trend/` (legacy), committed swing validation files in `ml/validation/`, and `infra/data/universes.json` (screener universes). Migrations are plain numbered SQL files in `packages/shared-py/migrations/NNNN_name.sql`, applied once in lexical order by `atlas_shared.migrate`. There is no Alembic. Add a new file for each change; never edit an applied one.

**Dashboard:** browser calls go to `/api/*`, which `next.config.js` rewrites to `ATLAS_API_URL` (default `http://localhost:8000`). Data fetching is client-side TanStack Query (no server components for data), and live updates come over the WebSocket in `src/lib/stream.tsx`. API types are hand-mirrored in `src/lib/types.ts`, so update them when a response shape changes.

## Conventions and invariants

- **Fail-soft everywhere.** Every external provider key is optional. Each one has a documented fallback chain: bars go Polygon → Alpaca → yfinance → synthetic GBM, FRED falls back to synthetic on *any* error, and DeepSeek falls back to the template. New integrations must degrade instead of raising, and must register in `signal_service/providers.py` (`/v1/providers/status`). `test_pipeline_runs_with_no_provider_keys` pins this behaviour.
- **`.env` loading is an entrypoint concern.** `atlas_shared.load_env()` copies `.env` into `os.environ` and is called only from the lifespan and the CLI `main`s, never at import time. A real `.env` sits at the repo root, and tests depend on a clean environment to exercise the "no key" paths, so keep tests from leaking `os.environ` changes (use `monkeypatch`).
- **Some env vars are read at import time** (`pipeline._MAX_BAR_AGE_HOURS`, `main._AUTO_EXEC_ENABLED`, `state._DEFAULT_PATH`, `stream._LEGACY_SIGNAL_STREAM`). Setting the env var inside a test won't affect them, so patch the module attribute instead.
- **LLM output never drives the signal.** The explanation engine only narrates. All LLM text goes through `safety_repair` (§10.4), which rewrites promises of profit, and the alert format must always include invalidation and a disclaimer.
- Postgres `NUMERIC` columns come back as `Decimal`. Pass route responses through `atlas_shared.to_jsonable`, or the dashboard receives strings and breaks.
- Alert-channel error messages must go through `alert_service.channels.base.redact_secrets` before they're logged or persisted. The Telegram bot token appears in httpx error URLs.
- Each `python -m <pkg>` needs a `__main__.py`. `tests/test_cli_entrypoints.py` checks every command referenced in README.md and OPERATIONS.md.
- **Docs drift tests** (`tests/test_docs.py`): SYSTEM.md must have an `` `apps/<name>` `` row for every app package, and relative links in README.md, SYSTEM.md and docs/README.md must resolve. Update these docs when you add a package or move a file. CHANGELOG.md is newest-first with `## X.Y.Z` headings.
- Bug fixes come with a regression test that explains the incident in its module docstring (see `tests/test_quant_label_leak.py`, `tests/test_swing_ingest.py`).
- Ruff: line length 100, py312. `N806` is allowed in the quant/macro engines (`X`/`Xtr` matrices) and `B008` in signal-service (FastAPI `Depends`/`Query` defaults).

## Gitignore gotchas

`.gitignore` excludes `data/` (which also matches `infra/data/`), `*.joblib`, and `docs/architecture/GAP_AUDIT.md`. A fresh clone therefore lacks `infra/data/universes.json`, the trained models in `ml/registry/trend/`, and GAP_AUDIT.md, even though the docs link to them. If you change `universes.json`, force-add it or fix the ignore rule.
