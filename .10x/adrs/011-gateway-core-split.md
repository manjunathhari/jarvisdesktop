# ADR-011: A separate gateway process, with Postgres between gateway and core

**Status:** Proposed
**Date:** 2026-11-22
**Feature:** phase-2-core-and-nodes
**Author:** 10x-Team (Architect + Security Engineer + Staff Engineer)

## Context
Phase 2 brings nodes (desktop now, phone in 2b) to the server. JARVIS SERVER is the product: it must keep its state, memory, audit, reminders and queued work whatever the nodes, Claude or a specialist do (Manj's governing principle; design §1). Architecture v2 §14 names five processes, including a separate gateway. The gateway is the only part reachable from devices.

## Decision
- **The gateway is its own process** (FastAPI + uvicorn). It authenticates nodes, writes each incoming message as a row, enqueues a job, relays reply chunks and outbox items, and records delivery and acknowledgements.
- **Core never talks to nodes; the gateway never decides.** They communicate only through Postgres tables. `NOTIFY` is a wake-up signal only. Stream content is stored as `conv.stream_chunk` rows with sequence numbers, so a restart on either side resumes from the last chunk.
- **Security boundary:** the gateway has no Claude key, no memory volume, no outbound internet, and a dedicated database role (`jarvis_gateway`) limited to its own tables and a few SECURITY DEFINER functions. Core signs actions and approvals; the gateway can only relay those signatures.
- The gateway reaches the tailnet through its own Tailscale sidecar (`jarvis-gw`), not the host's Tailscale socket (design K2-4).

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Gateway as a module inside core | Simplest; direct streaming | Network-facing code shares a process with the Claude key and memory; a gateway crash stops the brain; must be split later under load | Boundary would depend on care, not structure |
| Separate gateway calling core over internal HTTP | Familiar | Two network APIs to secure; durability depends on core writing rows | No gain over Postgres-in-between |

## Consequences
### Positive
- Durability (F3) and the security boundary are structural: P2-E02, P2-E03, P2-E12 and P2-S1 hold by construction.
- Matches v2 §14; Phase 3 workers fit the same pattern.
### Negative
- Streaming passes through Postgres (a few milliseconds per hop).
- One more container, plus the Tailscale sidecar.
### Risks
- Latency against the 3 s first-sentence target; mitigated by per-hop timing logs.
