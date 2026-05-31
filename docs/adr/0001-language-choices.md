# ADR-0001: Language choices for Phase 0–1

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Steve Yeong

## Context

The blueprint (§3, §4, §14, closing notes) prescribes a polyglot stack:

- **Rust** for ingestion, bar builder, and the indicator hot path.
- **Python** for ML, agents, scoring, services.
- **Go** for high-throughput stateless fanout (alerts).
- **TypeScript** (Next.js + React Native) for clients.

The case for Rust is real at scale: ~10M msgs/sec aggregate during US market open, microsecond budgets in §2's latency table. But Phase 1 will operate on **the top 500 US equities at 1m resolution**, which is well within Python's reach (Polars + numpy on a single box handles this without breaking sweat).

## Decision

**Phase 0 and Phase 1 are pure Python.** TypeScript is added when the web frontend is built.

We defer Rust and Go to Phase 2+ and gate their introduction on **measured profiling evidence**, not on the aspirational latency budgets in the blueprint. Specifically, we will not write a single line of Rust until at least one of:

1. The Python feature-engine cannot keep up with 1m-bar generation across 500 symbols on a 4-core box (i.e., > 60s wall-clock to compute one bar's worth of features for the universe).
2. We are ingesting tick-level data and `bar-builder` is provably the bottleneck.
3. A latency-sensitive paying customer's SLA cannot be met in Python.

We also defer Go. Alert fanout will start as a Python service. We add Go only if Python's GIL or memory footprint becomes a real production problem for that specific service.

## Consequences

### Positive

- One language to hire for and debug. A 1-person or 4-person team cannot afford the cognitive load of three backend languages while still finding product-market fit.
- Faster iteration on the indicator and scoring logic — those are the parts that determine whether we have alpha. Python makes that iteration cheap.
- The blueprint's "12 services" can collapse to ~5 Python packages in Phase 1, since we don't need separate Rust/Python deployments for the same logic.
- ML pipelines, training, and serving are first-class in Python with no FFI gymnastics.

### Negative

- We will hit a wall *somewhere* on the hot path. We must instrument early (OpenTelemetry, py-spy in CI) so we *see* the wall before it falls on us.
- When we do introduce Rust, we eat a 2-week tax for PyO3 setup, build infra (maturin), and CI changes. We accept this debt as deliberately deferred.
- Some hires will expect Rust; we accept that filter.

## Out-of-scope (revisit later)

- **Polars vs pandas.** Default to Polars for indicator pipelines (lazy execution, better perf on wide frames). Use pandas only when interfacing with libraries that demand it (e.g., `pandas-ta`).
- **Async runtime.** asyncio + uvloop for I/O-bound services (ingest, API). No Trio.
- **Numba JIT for scoring.** Allowed where profiling shows >10x speedup over numpy. Otherwise default to plain numpy/Polars.

## Revisit when

- We have paying customers with sub-second SLA.
- The feature engine cannot keep up with the universe at our target resolution.
- We add tick-level data feeds (Databento, direct exchange WS at full firehose).
