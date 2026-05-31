# ATLAS docs

| Where | What |
|---|---|
| [`architecture/BLUEPRINT.md`](architecture/BLUEPRINT.md) | The 1447-line design target — the system this codebase aims to be. |
| [`architecture/SYSTEM.md`](architecture/SYSTEM.md) | The actual delivered shape — packages, data flow, HTTP surface, and the **deferral ledger** (what's deliberately *not* built and when to revisit). |
| [`adr/`](adr/) | Architectural Decision Records. Read these before changing structural things — they document why something *isn't* the way the blueprint describes. |
| [`runbooks/OPERATIONS.md`](runbooks/OPERATIONS.md) | Bring-up, cron jobs, env toggles to go live, Prometheus alert triage, kill switches. |
| `../CHANGELOG.md` | Chronological build log — every shipped feature, mapped to phase numbers. |
| `../infra/observability/README.md` | Prometheus/Grafana stack — how to run it, what each panel shows. |
| `../apps/web/README.md` | Next.js dashboard — pages and where the data comes from. |

## Reading order if you're new

1. [`architecture/SYSTEM.md`](architecture/SYSTEM.md) — the one-page tour.
2. [`adr/0001-language-choices.md`](adr/0001-language-choices.md) and
   [`adr/0002-phase-0-scope.md`](adr/0002-phase-0-scope.md) — the two intentional
   pushbacks against the blueprint that shape everything else.
3. `../CHANGELOG.md` from the bottom up — the build, in the order it happened.
4. [`runbooks/OPERATIONS.md`](runbooks/OPERATIONS.md) — when you need to make it go.

## Reading order if you're operating it

1. [`runbooks/OPERATIONS.md`](runbooks/OPERATIONS.md) bring-up.
2. The Grafana **ATLAS — Engine Overview** dashboard.
3. The Prometheus `/alerts` tab.
4. `architecture/SYSTEM.md` only when you need to know *why* something is wired the way it is.
