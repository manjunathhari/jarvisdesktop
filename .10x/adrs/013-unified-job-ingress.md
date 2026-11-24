# ADR-013: Unified job ingress — every piece of work is a Postgres job

**Status:** Proposed
**Date:** 2026-11-22
**Feature:** phase-2-core-and-nodes
**Author:** 10x-Team (Architect + DBA)

## Context
Phase 2 is reactive, but the architecture must already support proactive and autonomous work (levels B–D) by adding triggers, not by rebuilding (design §1, F2). Phase 0 built a Postgres job queue (D5, ADR-006/007) with `FOR UPDATE SKIP LOCKED` claiming.

## Decision
- **One way in for all work:** node requests (`conv.turn`, `approval.decision`, `action.receipt`), scheduled triggers (`approval.expire`, `outbox.expire`, `brain.probe`) and internal events (`system.event`) are all rows in `ops.job`, handled by core.
- The gateway can enqueue only fixed inbound kinds, through a SECURITY DEFINER function.
- **Every handler is idempotent:** re-running a job after a crash changes nothing the second time.
- Internal facts are written first to an append-only event history (`comm.event`); deterministic notification rules turn events into outbox items.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Direct calls for node requests, queue only for background work | Slightly lower latency | Two code paths; server-initiated work becomes a special case | Fails F2's "same way in" |
| External broker (Redis, RabbitMQ) | Mature tooling | Another service and another store to back up | D5 settled on Postgres |

## Consequences
### Positive
- Phase 3 briefings and Phase 4 anticipation are new job kinds, not new architecture.
- Work survives restarts; crash recovery is the existing lease expiry.
### Negative
- Every turn pays a queue hop (milliseconds, with NOTIFY wake-up).
### Risks
- A non-idempotent handler would duplicate work on retry; each handler gets a retry test.
