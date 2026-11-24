# ADR-012: One persistent, signed WebSocket per node

**Status:** Proposed
**Date:** 2026-11-22
**Feature:** phase-2-core-and-nodes
**Author:** 10x-Team (Architect + Security Engineer)

## Context
The server must start conversations itself (F2) and deliver durable messages to nodes that come and go (F3). Nodes authenticate with Tailscale plus a per-node signing key (D9). The phone (Phase 2b) must not poll.

## Decision
- **Each node keeps one WebSocket** to the gateway, over the tailnet only. It carries both directions: utterances, decisions and receipts up; reply chunks, outbox items and actions down.
- **Handshake:** Tailscale `whois` must match the registered node; the node signs a server nonce with its Ed25519 key; revoked nodes are refused.
- **Every message** uses the node envelope (`contracts/node-envelope.schema.json`, made final in Phase 2) with a per-connection `seq` and an Ed25519 signature. Replays and bad signatures close the connection.
- **Server-side signing:** core signs actions and approval items when it writes them; the gateway signs only transport messages. Nodes refuse an action not signed by core.
- **Enrolment:** a one-time, 15-minute token created on the server; the node registers its public key through one SECURITY DEFINER function.
- Heartbeat every 30 s; 3 missed = offline (v2 §13.3).

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| HTTP requests + polling | Simple, stateless | Server can't push; polling drains the phone; latency | Fails F2 |
| HTTP + server-sent events | Simple push | Two channels to secure and correlate | WebSocket does both in one |
| Tailscale identity alone | No keys to manage | Any device on the tailnet counts as a node; can't revoke one node | Rejected in D9 |

## Consequences
### Positive
- Push, acknowledgement and resume on one connection; revoking one node touches nothing else.
### Negative
- Connection management and reconnect logic on every node; key storage on the node (DPAPI on Windows).
### Risks
- Canonical JSON for signatures must match exactly between Python and Rust; covered by shared contract tests.
