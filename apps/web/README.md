# @atlas/web

Next.js 14 (App Router) + TypeScript + Tailwind + TanStack Query dashboard for the ATLAS signal service.

## Run

```bash
# 1. Backend (separate terminal, from repo root)
uv run --package signal-service uvicorn signal_service.main:app --reload

# 2. Frontend
cd apps/web
pnpm install            # or: npm install / yarn install
pnpm dev                # http://localhost:3000
```

Set `ATLAS_API_URL` (defaults to `http://localhost:8000`) to point at a different backend. Requests go through `/api/*` so the browser never deals with CORS in dev.

## What's there

- **Dashboard** (`/`) — regime bar + watchlist + intro panel.
- **Symbol detail** (`/symbols/[symbol]`) — candlestick chart (lightweight-charts) with entry/stop/target price lines overlaid, full signal card with sub-score bars, trade plan, macro snapshot, sentiment snapshot, rationale.
- **Backtest** (`/backtest`) — run synthetic backtests (trend-follower / atlas), equity-curve chart, full metrics grid, cost-sensitivity sweep.
- **Portfolio** (`/portfolio`) — holdings table marked to latest bar, unrealized P&L, weight %, equity / VaR / sector-exposure strip. Seed demo positions with `uv run python -m portfolio_service.seed`.
- **Journal** (`/journal`) — auto-logged closed trades with R-multiple, hold time, exit reason; attribution strip (hit rate, avg win/loss R, expectancy) + exit-reason and per-symbol breakdowns. Seed with `uv run python -m journal_service.seed`.
- **Settings** (`/settings`) — edit the server-side watchlist (add/remove symbols, persisted to Postgres). The sidebar reads it live. Alert rules / broker / auth flagged coming soon.

The watchlist is server-driven: `GET /v1/watchlist` (falls back to a default list if Postgres is down), edited via `PUT /v1/watchlist`. Live updates arrive over the WebSocket (`/v1/stream`) — the header shows a live/connecting/offline dot.

## What's not yet there

- Auth (Phase 3+).
- Mobile-optimized layout.
- Backtest job queue — currently synchronous synthetic only via the API.
- Live broker fills feeding the journal — currently backtest-sourced.

## Conventions

- All API traffic uses `/api/*` rewrites so the browser is CORS-free.
- Tailwind tokens (`bg`, `ink`, `bull`, `bear`, `accent`) defined in `tailwind.config.ts`.
- Pages keep data-fetching local via `useQuery` — no SSR/server components for data so the polling cadence is consistent.
