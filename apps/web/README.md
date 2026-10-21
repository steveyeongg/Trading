# @atlas/web

Next.js 14 (App Router) + TypeScript + Tailwind + TanStack Query dashboard for the ATLAS signal service.

## Run

From the repo root, `scripts/atlas.sh up` starts the API, this dashboard and the database, and opens the browser (`scripts/atlas.sh status`, `scripts/atlas.sh down`). By hand:

```bash
# 1. Backend (separate terminal, from repo root)
uv run --package signal-service uvicorn signal_service.main:app --reload

# 2. Frontend
cd apps/web
pnpm install            # or: npm install / yarn install
pnpm dev -H 127.0.0.1 -p 3000   # http://localhost:3000, this Mac only (Next listens on every interface by default; there is no login)
```

Set `ATLAS_API_URL` (defaults to `http://localhost:8000`) to point at a different backend. Requests go through `/api/*` so the browser never deals with CORS in dev.

Checks: `pnpm typecheck` and `pnpm build`. `pnpm lint` is broken at baseline (no ESLint config), and there is no JS test runner, so UI behaviour is checked in the browser.

## What's there

**The swing pages (primary; the nav bar):**

- **Today** (`/`) — the market line, ranked ideas from setups that passed testing, a muted "Unproven ideas: these setups have not passed testing" section, watch-only names in a downtrend, the nightly action for each open position, last-scan status, first-run download progress, and **Run scan now**.
- **Check a stock** (`/stocks/[symbol]`) — the search box on every page leads here: daily chart with the levels, the rule checklist and "I bought this".
- **Positions** (`/positions`) — record trades you placed yourself, see what to do next, move the stop, close at your price. ATLAS never places an order.
- **Track record** (`/track-record`) — per setup: in-sample, out-of-sample (each failed condition) and live record.
- **Settings** (`/settings`) — account and risk settings, universe floors, the Telegram digest switch and test, and the system status panel.

Data comes from `/v1/today`, `/v1/stocks/{symbol}`, `/v1/positions`, `/v1/track-record`, `/v1/settings`, `/v1/scan/run` and `/v1/system/status`; types are hand-mirrored in `src/lib/swing-types.ts` and the page copy is in `src/lib/copy.ts`. Every page shows a stated reason instead of an empty area.

**Legacy composite pages** (kept, reachable by address, not in the nav):

- **Symbol detail** (`/symbols/[symbol]`) — candlestick chart (lightweight-charts) with entry/stop/target price lines overlaid, full signal card with sub-score bars, trade plan, macro snapshot, sentiment snapshot, rationale.
- **Scanner** (`/scanner`), **Alerts** (`/alerts`).
- **Backtest** (`/backtest`) — run synthetic backtests (trend-follower / atlas), equity-curve chart, full metrics grid, cost-sensitivity sweep.
- **Portfolio** (`/portfolio`) — holdings table marked to latest bar, unrealized P&L, weight %, equity / VaR / sector-exposure strip. Seed demo positions with `uv run python -m portfolio_service.seed`.
- **Journal** (`/journal`) — auto-logged closed trades with R-multiple, hold time, exit reason; attribution strip (hit rate, avg win/loss R, expectancy) + exit-reason and per-symbol breakdowns. Seed with `uv run python -m journal_service.seed`.
- **Legacy settings** (`/settings/legacy`) — edit the server-side watchlist (persisted to Postgres), the providers status panel and the tier switcher.

The legacy watchlist is server-driven: `GET /v1/watchlist` (falls back to a default list if Postgres is down), edited via `PUT /v1/watchlist`. Live updates arrive over the WebSocket (`/v1/stream`) — the header shows a live/connecting/offline dot.

## What's not yet there

- Auth (Phase 3+).
- Mobile-optimized layout.
- Backtest job queue — currently synchronous synthetic only via the API.
- Live broker fills feeding the journal — currently backtest-sourced.

## Conventions

- All API traffic uses `/api/*` rewrites so the browser is CORS-free.
- Tailwind tokens (`bg`, `ink`, `bull`, `bear`, `accent`) defined in `tailwind.config.ts`.
- Pages keep data-fetching local via `useQuery` — no SSR/server components for data so the polling cadence is consistent.
