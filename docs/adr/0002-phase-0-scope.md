# ADR-0002: Phase 0 scope and deviations from the blueprint

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Steve Yeong

## Context

The blueprint (§4) describes a monorepo with **24 deployable services**, **5 storage systems** (Postgres, TimescaleDB, ClickHouse, Redis, Qdrant), a **message bus** (Kafka), and **multi-region Kubernetes**. That is the steady-state architecture, not the bootstrap architecture.

If we scaffold all 24 service directories now, we get a tree full of empty folders that obscures the small number of things that actually exist. If we provision all 5 databases, we pay infra cost and operational complexity for storage we don't write to.

## Decision

Phase 0 scaffolds **only what Phase 1 needs**, with deliberate deferrals.

### What we build now

| Package | Purpose | Phase 1 role |
|---|---|---|
| `apps/ingest-equities` | OHLCV ingestion → TimescaleDB | Pulls bars from Polygon/Alpaca |
| `apps/feature-engine` | 25 technical indicators per bar | Pure pandas-ta + Polars; runs as a scheduled job |
| `apps/quant-engine` | XGBoost trend model | One model, calibrated, trained offline |
| `apps/scoring-engine` | Sub-scores → composite | Hand-tuned weights, per blueprint §8 |
| `apps/signal-service` | FastAPI: `/v1/signals`, `/v1/scan` | The only HTTP-facing service in Phase 1 |
| `apps/web` | Next.js dashboard | Created later via `pnpm create next-app` |
| `packages/shared-py` | logging, db clients, Pydantic schemas | Imported by every service |

### What we defer

| Item | Defer to | Why |
|---|---|---|
| Kafka / Redpanda | Phase 2 | Phase 1 has no fan-out problem. Redis Streams + direct DB writes are sufficient. Adding Kafka now costs us 1–2 weeks of infra learning for zero day-1 value. |
| ClickHouse | Phase 2 | TimescaleDB alone is fine until we have multi-year backtests at scale. The cost of running both before that is operational, not financial. |
| Qdrant / vector DB | Phase 2 | No RAG until LLM explanations exist. |
| Neo4j graph DB | Phase 3 | Correlation graph is a nice-to-have, not a Phase 1 differentiator. |
| Kubernetes / Helm / ArgoCD | Phase 3 (or when first paying customer demands it) | docker-compose + a single VM gets us to ~100 paying users. K8s is a 1-FTE operational tax. |
| Terraform | Same as K8s | Pulumi-or-Terraform is a debate for when there's infra to manage. |
| Rust services | See ADR-0001 | Premature without profiling evidence. |
| Multi-agent LangGraph | Phase 3 | A single Explanation Writer agent is enough for Phase 1. The blueprint's 13-agent design is a research demo until it's earning revenue. |
| Mobile, desktop | Phase 4+ | Web first. |
| Forex, futures, options | Phase 3+ | US equities + crypto in Phase 1–2. Asset-class fanout is a distraction before alpha is proven. |

### Local infra (docker-compose)

Phase 0 provisions **three services** locally:

- **PostgreSQL 16** with **TimescaleDB extension** — OLTP and hot OHLCV in one engine. We split them later if/when contention shows up.
- **Redis 7** — cache, Streams for ephemeral pub/sub.

No Kafka, no ClickHouse, no Qdrant. Adding them is a one-file edit when their respective ADR ships.

### What "done" looks like for Phase 0

- `uv sync` succeeds at the repo root.
- `docker compose up -d` starts Postgres+Timescale+Redis.
- `uv run --package signal-service uvicorn signal_service.main:app` serves a healthz endpoint.
- CI runs `ruff`, `mypy`, and `pytest` on every push.
- ADRs 0001 and 0002 are merged.

## Consequences

### Positive

- The tree shows ~6 packages instead of 24. Anyone reading it can hold the whole thing in their head.
- We can ship Phase 1 features without first solving Kafka, ClickHouse, K8s, or RAG. Each of those is a multi-week distraction.
- We discover real bottlenecks against real load instead of guessing.

### Negative

- We will eventually re-do storage decisions and split TimescaleDB out from Postgres, add ClickHouse, etc. Migration tax is real but manageable.
- The "we'll add it when we need it" rule requires honest instrumentation. If we don't measure, we won't know.
- Contributors expecting the full 24-service tree from the blueprint will be momentarily confused; this ADR is the answer.

## Revisit when

- Any deferral starts blocking a customer commitment.
- Profiling shows a bottleneck the current stack cannot address.
- Hiring justifies the operational tax of a more complex deployment.
