# JARVIS server — Phase 2 (Jarvis core and nodes) design

**Status:** **Revision 7, awaiting Manj's review.** Matching plan: `.10x/plans/2026-11-22-phase-2-server.md`, revision 7. Revision 7 adds R7-1 to R7-6 (§20): how audited refusals work (the function audits and returns a structured refusal, and the caller commits before raising); audited enrolment creation; the gateway's function surface; `last_ping_at`; the fenced-connection error code; and the 0008 rollback rule (in the plan).

**Approval history:**
- **Revision 4:** approved by Manj on 2026-11-22.
- **Revision 5** (R5-1 offline-node re-enrolment, R5-2 validated resume): approved by Manj with plan revision 4 on 2026-11-22.
- **Revision 6** (R6-1 to R6-8): approved by Manj with plan revision 6 (corrected) on 2026-11-22.

Authorises the implementation plan, not code. ADR-011 to 014 remain Proposed.
**Date:** 2026-11-22
**Feature slug:** `phase-2-core-and-nodes`
**Inputs:** Architecture v2 §5–§9, §13–§15 (project doc `Jarvis_Architecture_v2.md`), decisions D1–D8, `phase-1-handover.md`, Phase 0 and Phase 1 designs, the Phase 2 kick-off conversation (22 Nov 2026).
**Decision records:** ADR-011 (gateway and core split), ADR-012 (node transport), ADR-013 (unified job ingress), ADR-014 (Atlas foundation). All Proposed.

## 1. Intent

**JARVIS SERVER is the product.** The desktop and the phone are ways of talking to it. Phase 2 moves the brain off the desktop and onto the server: core holds identity, conversations, the Claude brain, memory, policy, decisions and audit, and the nodes only listen, show and speak.

Phase 2 delivers a **reactive** Jarvis (it answers when asked) on an architecture that can already become **proactive** (Phase 3) and an **autonomous coordinator** (Phase 4) by adding capabilities, never by replacing parts. The destination is level D: a full personal operations system (v2 §1).

**Manj's governing principle:** even if the desktop is off, the phone is unavailable, Claude is down or one specialist fails, the server keeps its identity, state, memory, audit history, reminders, verification and queued work. It never depends on a node being online.

### 1.1 Three foundational principles (requirements, not enhancements)

| # | Principle | In practice |
|---|---|---|
| F1 | **Server-first execution** | Nodes are clients, never owners of Jarvis state. A replacement node loses nothing. |
| F2 | **Event-driven core** | Every meaningful operation can start from a node request *or* an internal server event. Both arrive the same way (ADR-013). |
| F3 | **Durable communication** | Everything the server says is written to Postgres before it is sent. Messages and delivery state survive node disconnection, restart and replacement. |

### 1.2 Manj's priority order

1. Jarvis core: the conversation lifecycle.
2. Gateway: secure communication, authentication, node identity.
3. Brain router: Claude only, cost controls, failure handling.
4. Policy engine: deterministic authorisation and approvals.
5. Memory integration: authenticated retrieval and actionable-memory checks.
6. Atlas foundation: the interface for approved work.
7. Node integration: the desktop now; the native phone app in Phase 2b.

When scope has to give, it gives from the bottom of this list.

## 2. Decisions (kick-off, 22 Nov 2026)

| # | Decision | Notes |
|---|---|---|
| K2-1 | **Approve on the desktop in Phase 2.** The phone app is Phase 2b, straight after. | Approvals go through a channel interface. Phase 2b adds the phone without changing core. |
| K2-2 | **Phase 2 is reactive, persistent and server-owned.** It speaks unprompted only about its own state: approvals expiring, queued work done, Claude down or back, the cost cap near. | Briefings and watching the outside world are Phase 3. |
| K2-3 | **The approval test deletes a disposable file on the PC**, in an isolated test folder, never a real file. | It is also the first piece of the Computer specialist. |
| D9 | **Node authentication: Tailscale plus a per-node signing key.** | Tailscale is the network boundary. Each node has its own key pair. Revoking one node touches no other. Node identity never comes from request data. |
| D10 | **Conversation persistence:** dedicated Postgres tables. The server owns all conversation state, and none of it goes into the memory engine. Kept indefinitely; tiering (hot, warm, cold) is Phase 5. | |
| D11 | **Dedicated server before Phase 2 is complete.** Development and testing use the shared server with fictional people only. | Encryption at rest, off-server backups and a verified restore stay prerequisites for real memories. |
| K2-4 | **The gateway reaches the tailnet through its own Tailscale sidecar** (`jarvis-gw`), not the host's Tailscale socket. | §4.2. Peer identity comes from the sidecar; no host port; its own ACL tag. |
| K2-5 | **Requests held by the cost cap or the daily ceiling expire after 1 hour, with a notification.** They never run days later. | §10. Claude-down holds still run when Claude is back. |
| ADR-011 | **A separate gateway process, with Postgres in between.** | §4. |
| ADR-012 | **One persistent WebSocket per node.** | §7. |
| ADR-013 | **Unified job ingress.** Everything becomes a job on the existing Postgres queue (D5). | §5.4. |
| ADR-014 | **Atlas is an interface inside core: approved plan → ticket.** No workers process until Phase 3. | §11. |

Still settled from earlier phases (not reopened): D1–D8 (Claude only behind the router; Rs 5,000/month cap; Postgres job queue; Python server, Rust desktop), K1–K5, the Phase 1 memory decisions, and the standing rules (personal and family scope only; code is the system of record; receipts verified at source; append-only audit; public repo, so no secrets, addresses or server IDs in it).

## 3. Goals, non-goals, anti-goals

**Goals:** §1.2, in that order, and the acceptance tests in §14.

**Not in Phase 2:**
- The phone app (Phase 2b).
- Gmail and Calendar, intake, the morning briefing and the context engine (Phase 3).
- Atlas dispatch, the verifier as a separate component, and the workers process (Phase 3).
- Any provider other than Claude (D4).
- Records tiering (Phase 5).
- Real people or real memories while on the shared server (D11).
- The desktop backlog: barge-in, the wake word, a global hotkey and auto-update (separate tweak work).

**Anti-goals.** These count as failures even if every test passes:
- Any Jarvis state that lives only on a node.
- The gateway being able to call Claude, read memory or change the ledger beyond its own tables.
- A model deciding anything that code should decide: tiers, approval validity, state changes, notification priority.
- An approval acted on after it expired, or acted on twice.
- A message silently lost or delivered twice as two different messages.
- Spending past the cap.

## 4. Processes and boundaries (ADR-011)

```
desktop / phone ──WebSocket──► gateway ──rows + NOTIFY──► Postgres ◄── core
                 (Tailscale only)                                       scheduler
```

| Process | Does | Never |
|---|---|---|
| **gateway** (new, FastAPI + uvicorn) | Accepts WebSocket connections; checks Tailscale identity and the node's signature; writes each incoming message as a row and enqueues a job; streams reply chunks and delivers outbox items; records delivery and acknowledgement; tracks connection state. | Holds the Claude key; mounts the memory volume; writes ledger, conversation or audit rows other than through its grants (§4.1); talks to core directly. |
| **core** (existing, extended) | Claims jobs; runs the conversation turn; calls Claude through the router; applies policy; reads and writes memory; writes events, replies, stream chunks and outbox items; creates approvals. | Talks to a node. |
| **scheduler** (existing, extended) | Heartbeats; enqueues time-based jobs: approval expiry, outbox expiry, Claude health probes, the delivery sweep. | Any decision logic (it only enqueues). |
| **init**, **postgres**, **backup** | As now. init gains the new migrations and smoke rows. | |

`workers` stays a placeholder (ADR-014).

### 4.1 Database roles and grants

A new role, **`jarvis_gateway`**, gets only what the gateway needs. Everything not listed is not granted. Table grants:

| Object | Grant |
|---|---|
| `gw.node` | SELECT (columns `id`, `person_id`, `kind`, `public_key`, `tailscale_identity`, `revoked_at`) |
| `conv.stream_chunk` | SELECT |
| `comm.outbox` | SELECT |
| `comm.delivery` | SELECT |
| everything in `ledger`, `audit`, `ops` (other than the functions below), `conv.message`, `conv.session`, `gw.enrolment`, memory | **nothing** |

The gateway changes state **only** through these functions, and holds EXECUTE on nothing else:

| Function | Does |
|---|---|
| `gw.enrol_node(token, public_key, tailscale_identity)` | One-time node registration (§7.2) |
| `gw.open_connection(node_id)` / `gw.close_connection(conn_id, reason)` | Connection rows; opening fences any older connection of the node |
| `gw.touch_connection(conn_id)` | Heartbeat: `gw.node.last_seen_at` and `gw.node_connection.last_ping_at` |
| `gw.current_node(conn_id)` | The node of an open, non-revoked connection, or the fenced-connection error. Used inside the other functions, and by the gateway's 60 s revocation re-check |
| `conv.record_inbound(conn_id, message_id, kind, payload)` | Writes `conv.inbound` (person filled from the node row inside the function) and enqueues the matching fixed job kind |
| `comm.record_sent(conn_id, outbox_id)` / `comm.record_received(conn_id, outbox_id)` / `comm.acknowledge(conn_id, outbox_id)` / `conv.record_chunk_received(conn_id, reply_id, seq)` | Delivery and stream-cursor transitions (§8.2, §6.2) |

The five functions in the `gw` schema are the gateway's whole node and connection surface. Every function used on an established connection takes the connection ID and gets the node through `gw.current_node`. Only `gw.enrol_node` (before any connection exists) and `gw.open_connection` (which creates it) don't.

**Rules for every SECURITY DEFINER function** (gateway-facing and core-facing alike):
- `SET search_path = pg_catalog, pg_temp`, and every object reference is schema-qualified.
- Owned by `jarvis_owner` (not a superuser). `REVOKE ALL ON FUNCTION … FROM PUBLIC`, then `GRANT EXECUTE` to the one role that needs it.
- Arguments are checked inside the function. Node and person IDs are looked up from `gw.node`, never taken from a payload.
- **Refusals (R7-1).** A function treats three kinds of failure differently, because in PostgreSQL an error rolls back the whole transaction, including any audit row written before it:

  | Failure | The function | Audit |
  |---|---|---|
  | **A refused request:** a decision the database makes against a well-formed call (a bad or used token, a key already in use, an unknown inbound kind, revoking an unknown or already revoked node) | writes the REJECTED audit row and **returns a structured refusal**: a defined reason code, never a bare NULL. It does not raise. | Yes, in the same transaction as the decision |
  | **A fenced connection** (§7.1, R7-5): the connection is closed, superseded or its node revoked | raises the dedicated error `connection_fenced` (SQLSTATE `JF001`), distinct from ordinary permission denial | No row of its own: the operation that caused the fencing (opening a newer connection, closing it, or revoking the node) is already audited |
  | **A malformed call** (a missing or invalid argument: a programming error) | raises `invalid_parameter_value` | No |

- **The caller commits before it raises (R7-1).** The Python wrapper for an audited function runs the call in **its own transaction**, which it commits, and only then raises the matching application exception (for example `EnrolmentRefused(reason)`). It must never call an audited function inside a transaction it might roll back, because the refusal's audit row would disappear with it. The shared helper refuses to run unless the connection is idle, with no transaction open. Tests go through this Python boundary: after the exception, a **separate** connection must still see the audit row.
- No second call writes an audit row afterwards: the function records the refusal atomically with its decision.

Its password is a new secret file, `pg_gateway_password`. Privilege tests: P2-S1 and P2-S6 (§14).

### 4.2 Network

**The gateway has no published host port. It is reachable only through its Tailscale sidecar (K2-4).**

- **Sidecar:** `gateway-ts` (official `tailscale/tailscale` image, pinned by digest) runs `tailscaled` in kernel mode (`/dev/net/tun`, `NET_ADMIN`). It's a tailnet device named `jarvis-gw` with ACL tag `tag:jarvis-gateway`. It joins two Docker networks: `jarvis_internal` (to reach Postgres) and `jarvis_ts_egress` (internet, for Tailscale coordination and DERP relays only). Its state is kept in the volume `gateway_ts_state`.
- **Shared namespace:** the gateway runs with `network_mode: service:gateway-ts`, so it has the sidecar's interfaces: `tailscale0`, the internal network and the egress network.
- **Listening:** at startup the gateway asks the sidecar's local API for its tailnet IPv4 address and binds **only to that address** (port 8443). It refuses to start if the address is unknown, and never binds `0.0.0.0`. Other containers on `jarvis_internal` can't reach that address, because they have no route to the tailnet range.
- **No host port:** compose has no `ports:` entry for any service. A CI check fails the build if one appears, and P0-8 keeps passing unchanged.
- **Gateway egress is blocked:** an entrypoint step in `gateway-ts` installs `iptables` OUTPUT rules in the shared namespace. Traffic from the gateway's UID (10001) may leave only on `tailscale0` (replies to nodes) or to the `jarvis_internal` subnet (Postgres), and all other traffic from that UID is dropped. `tailscaled` runs as root, so it is unaffected. P2-S5 tests this from inside the gateway container.
- **Peer identity:** `whois` on the sidecar's local API socket, which is shared only between the sidecar and the gateway (a volume mounted into those two). The host's Tailscale socket is never mounted.
- **Two checks:** Manj's Tailscale ACL decides which of his devices may reach `tag:jarvis-gateway:8443`; the node keys decide which of those are Jarvis nodes.
- **Core egress (clarification, accepted mitigation):** core does **not** get an internet route. A small `egress-proxy` container (pinned by digest) on `jarvis_internal` and a new `jarvis_egress` network allows only `CONNECT api.anthropic.com:443`, and refuses everything else. Core reaches Claude through it (`HTTPS_PROXY`). The residual risk is limited to data sent to that one host, which is the brain provider we already trust. P2-S7 checks core can't reach any other host.

### 4.3 Secrets (files under `/etc/jarvis/secrets`, as now)

| New secret | Used by |
|---|---|
| `tailscale_gateway_authkey` | gateway-ts only. Manj creates it once in the Tailscale admin, tagged, not reusable. |
| `pg_gateway_password` | gateway, init |
| `gateway_signing_key` (Ed25519) | gateway only: transport messages (§7.1) |
| `core_signing_key` (Ed25519, with a key ID) | **core only**: authorisation signatures on actions and approval cards (§7.1). The gateway can't read it. |
| `anthropic_api_key` | core only |

## 5. Data model (new migrations, from 0008)

Three new schemas keep the boundaries visible: `gw` (nodes), `conv` (conversations), `comm` (events, outbox, delivery). Approvals go in `ledger`, next to tasks.

### 5.1 Nodes (`gw`)

| Table | Key columns |
|---|---|
| `gw.node` | `id`, `person_id` → `ledger.person`, `kind` (DESKTOP, PHONE), `name`, `public_key` (Ed25519), `tailscale_identity`, `registered_at`, `revoked_at`, `last_seen_at` |
| `gw.node_connection` | `id`, `node_id`, `opened_at`, `last_ping_at`, `closed_at`, `close_reason` |
| `gw.enrolment` | `id`, `token_hash`, `person_id`, `kind`, `name`, `expires_at`, `used_at`, `node_id` (§7.2). Rows are created only through the audited core function `gw.create_enrolment(…)` (R7-2); no role inserts them directly, and the gateway has no grant on this table. |

A revoked node (`revoked_at` set) is refused at the handshake, and its open connection is closed within one sweep (P2-E10).

### 5.2 Conversations (`conv`)

| Table | Key columns |
|---|---|
| `conv.session` | `id`, `person_id`, `started_at`, `last_activity_at`, `closed_at` |
| `conv.inbound` | `id` = the node's `message_id` (unique, so a resend is a no-op), `node_id`, `person_id` (filled from the node row, never from the message), `session_id`, `kind`, `payload`, `received_at` |
| `conv.message` | `id`, `session_id`, `seq`, `role` (PERSON, JARVIS, SYSTEM), `person_id`, `node_id` (null for JARVIS), `text`, `inbound_id`, `created_at` |
| `conv.stream_chunk` | `reply_id`, `seq`, `type` (SPEECH, ACTION), `text` or `authz` (jsonb, core-signed, for ACTION; §7.1), `final` (bool), `target_node_id` and `valid_until` (for ACTION), `created_at`. Primary key `(reply_id, seq)` |
| `conv.session_ticket` | `session_id`, `task_id` (v2 §8.4: sessions link to the tickets they create) |

**Session rule (my call):** a person has at most one open session. A message after 30 minutes of inactivity starts a new one. Any node of that person joins the open session, which is how a conversation resumes on a different node (P2-E09).

### 5.3 Events, outbox and delivery (`comm`)

| Table | Key columns |
|---|---|
| `comm.event` | `id`, `kind`, `subject_type`, `subject_id`, `person_id`, `details` (codes and IDs, no free text), `occurred_at`. **Append-only**: the app roles have INSERT only, and a trigger refuses UPDATE and DELETE, as in `audit.event`. |
| `comm.outbox` | `id` (stable; P2-E04), `person_id`, `event_id`, `priority` (URGENT, TODAY, DIGEST, SILENT), `kind`, `body`, `authz` (core's signed authorisation, for approval cards; §7.1), `group_key`, `supersedes_id`, `expires_at`, `state`, `state_changed_at`, `created_at` |
| `comm.delivery` | `outbox_id`, `node_id`, `attempts`, `first_sent_at`, `last_sent_at`, `received_at`. Primary key `(outbox_id, node_id)`: one row per item per node, updated on each retry. |

The multi-node state rules are in §8.2. No content goes in `comm.event` or `audit.event`. Content lives in `comm.outbox.body` and `conv.message` (retention: §5.7).

### 5.4 Jobs (ADR-013)

There is one way in: `ops.job`. New job kinds:

| Kind | Enqueued by | Core does |
|---|---|---|
| `conv.turn` | gateway (an utterance arrives) | Runs a conversation turn (§6.2) |
| `approval.decision` | gateway (approve or deny arrives) | Applies the decision (§9.3) |
| `action.receipt` | gateway (a node reports an action result) | Verifies it and closes the task (§9.4) |
| `approval.expire` | scheduler (at `expires_at`) | Marks the approval EXPIRED; emits an event |
| `outbox.expire` | scheduler | Expires outbox items past `expires_at` |
| `brain.probe` | scheduler (while Claude is marked down) | Health check; emits CLAUDE_BACK and re-queues held turns |
| `system.event` | anyone | Turns an internal event into outbox items (§8) |
| `brain.reservation_sweep` | scheduler (every minute) | Lapses ACTIVE reservations past their lease (§10.1) |
| `outbox.resend_sweep` | scheduler (every 60 s) | Wakes the gateway to re-send unacknowledged items (§8.2) |
| `retention.sweep` | scheduler (daily) | Deletes past-retention rows (§5.7) |

Jobs use the existing `claim` with `FOR UPDATE SKIP LOCKED`. Every job handler must be idempotent: re-running a job after a crash changes nothing the second time.

### 5.5 Approvals (`ledger.approval`)

Columns: `id`, `task_id`, `requested_from_person_id`, `requesting_node_id`, `tier`, `action_kind`, `action_args` (for the delete test: file name, size, SHA-256 and Windows file ID, all from the listing; §9.3), `summary`, `card_authz` (the core-signed `approval.card` authorisation exactly as sent; §7.1), `expires_at`, `decision` (PENDING, APPROVED, DENIED, EXPIRED), `decided_at`, `decided_by_node_id`, `consumed_at`, `execution_key` (unique, null until consumed).

Guards in the database, so no code path can skip them:
- `decision` changes only from PENDING. APPROVED or DENIED are accepted only while `now() < expires_at` (P2-E11).
- `consumed_at` and `execution_key` are set exactly once, only when `decision = APPROVED` and `now() < expires_at`, through `ledger.consume_approval(approval_id)` (§9.4).

### 5.6 Cost meter and budget reservations (`ops`)

| Table | Key columns |
|---|---|
| `ops.brain_budget` | One row per month: `month`, `cap_inr`, `settled_inr`, `reserved_inr`. All changes happen under a row lock (§10.1). |
| `ops.brain_reservation` | `id`, `job_id`, `turn_id` (the `conv.inbound` id; null for probes), `month`, `day`, `reserved_inr`, `state` (ACTIVE, SETTLED, RELEASED, LAPSED), `actual_inr`, `created_at`, `lease_until` |
| `ops.brain_usage` | `id`, `reservation_id`, `model`, `input_tokens`, `output_tokens`, `cost_inr`, `at` |

Core only.

### 5.7 Retention (clarification)

| Data | Kept | Why |
|---|---|---|
| `conv.session`, `conv.message` | Indefinitely (D10) | The conversation itself |
| `comm.outbox` (incl. `body`) | Indefinitely | It's part of conversation history |
| `comm.event`, `audit.event` | Indefinitely | Codes only; the evidence |
| `conv.stream_chunk` | 24 h after its reply's final chunk | The final text is in `conv.message`; action chunks are recorded as codes in audit |
| `conv.inbound` | 30 days | Duplicates `conv.message`; kept for debugging and replay |
| `comm.delivery`, `gw.node_connection` | 90 days | Operational detail |
| `ops.brain_usage`, `ops.brain_reservation` | 13 months | Budget history |

**Operational implications:**
- Text only, so growth is small: tens of MB a year at personal use. The daily dumps include it, and on the dedicated server it is covered by encryption at rest.
- Phase 2 has no "delete this conversation" path. A person's request to forget a conversation is handled by hand until Phase 5's records work.
- **What Manj says to the desktop during Phase 2 testing is real speech stored on the shared server.** Live testing therefore uses scripted test phrases only. The conversation and communication tables (`conv.*`, `comm.*`) are emptied before the move to the dedicated server (a D11 criterion), so nothing said during testing becomes permanent history.
- A scheduler job, `retention.sweep`, runs daily and deletes past-retention rows, writing one audit row per table per run with the count.

## 6. Conversation lifecycle

### 6.1 A node connects

1. **Open:** the WebSocket opens on `jarvis-gw`'s tailnet address. The gateway reads the peer's Tailscale identity from the sidecar's local API (`whois`). An unknown peer is closed.
2. **Challenge (R6-1):** immediately after accepting the WebSocket, and before reading anything from the node, the gateway sends `challenge`, signed `gw-transport`.
   - **The nonce:** 32 bytes from the operating system's cryptographic random source, base64url (43 characters). It is new for every connection and held only in that connection's memory, so it dies with the connection or a gateway restart. A nonce is never derived from, or reused across, anything else.
   - **The payload:** `nonce`, `issued_at` (gateway clock, RFC 3339 with offset), `expires_at` (= `issued_at` + 30 s), and the gateway's current `key_id` and `public_key`.
3. **The node checks the challenge before replying:**
   - it must be the first message on the connection;
   - its `key_id` and `public_key` must match a pinned gateway key. During enrolment, which has no pinned key yet, `fingerprint(public_key)` must equal the gateway fingerprint in the enrolment code;
   - the `gw-transport` signature must verify with that key.

   Anything else (a missing, malformed or unverifiable challenge, or any other message first) → the node closes the connection and sends nothing.
4. **Clock skew is advisory only.** The node computes `skew = issued_at − its own clock`. If `|skew|` > 30 s, the node puts `clock_skew_ms` in `hello`, shows "This device's clock is off", and the gateway records event `node.clock_skew` (codes only). The server's time never replaces the node's own clock for validity checks, and never substitutes for nonce or signature checks.
5. **The node replies** with `hello` (`nonce`, `resume`, optional `clock_skew_ms`), signed `node-msg` with its key. A new device sends `enrol` instead (token, public key, `nonce`), signed with its new key (§7.2).
6. **The gateway checks the reply** (R6-7 splits this into two paths). Any failure closes the connection with code 4401 and writes an audit row with the failing check as a code (`bad_signature`, `bad_nonce`, `challenge_expired`, `not_first`, `bad_seq`, `identity_mismatch`, `revoked`, `bad_token`, `key_in_use`).

   **Checks common to both paths:**
   - it is the **first** node message on this connection, with `seq` = 1 (R6-8);
   - its `nonce` equals **this connection's** nonce, compared in constant time;
   - it arrived before `expires_at` on the gateway's clock.

   **Path A, an existing node (`hello`)**, all of these as well, **unchanged by enrolment**:
   - the envelope `node_id` names a registered node (not the nil UUID);
   - the `node-msg` signature verifies with **that node's registered** `public_key`;
   - the node isn't revoked;
   - the connection's Tailscale identity (from `whois`) equals the node row's `tailscale_identity`;
   - the node's `key_state` decides what happens next: CURRENT → step 7; STALE → R6-6.

   **Path B, a new enrolment (`enrol`, envelope `node_id` = nil UUID)**, all of these as well:
   - the `node-msg` signature verifies with the **public key carried in the `enrol` message**, which proves the device holds that key;
   - that public key isn't already registered to any node;
   - `gw.enrol_node(token, public_key, tailscale_identity)` succeeds, with `tailscale_identity` taken from **this connection's** `whois`, never from the message. The function enforces the token being valid, unused and unexpired, single use under concurrency, insert-only, and the unique active Tailscale identity (§7.2).

   An `enrol` can never authenticate as an existing node, and a `hello` can never create one.

   **The nonce is consumed on the first reply, whatever the outcome.** A connection gets one attempt. A nonce from one connection can't authenticate another: each connection only ever compares against its own nonce, and an old nonce is gone once its connection closes.
7. **Welcome:** the gateway opens a `gw.node_connection` row and sends `welcome`, with the open session, if any, and the reconnect summary (§8.3). After a successful **enrolment** (path B), `welcome` also carries an `enrolment` block, which the node must verify before it accepts anything core-authorised (§7.2 step 5, R6-6).
   - **The block:** `node_id`, `core_key_id`, `core_public_key`.
   - **The node's check:** `fingerprint(core_public_key)` equals the core fingerprint in the enrolment code. Then it pins `core_key_id → core_public_key` as a CORE key, stores `node_id`, and from then on uses `node_id` as its envelope `node_id`.

   Until that check passes, the node treats the connection as unauthorised. It shows nothing from the summary, and refuses every `node.action`, approval card and `keyset`.

**Envelope `node_id` before the node is known:** `challenge`, and an `enrol` from a device without a node ID, carry the nil UUID `00000000-0000-0000-0000-000000000000`. Every other message carries the real node ID: the sender for node messages, the recipient for gateway messages. The gateway refuses the nil UUID on any other kind.

### 6.2 A conversation turn (reactive)

1. The node sends `utterance`: what was heard, plus a short screen summary, which is what the desktop's `BrainRequest` carries today.
2. The gateway writes `conv.inbound` (a duplicate `message_id` is a no-op) and enqueues `conv.turn`. Then it NOTIFYs `jarvis_jobs`.
3. Core claims the job and works out the person from `conv.inbound.person_id`, which came from the node row. It finds or opens the session and appends the PERSON message.
4. Core builds the brain request from:
   - the last 20 messages of the session;
   - `memory.retrieve_context(person, utterance, viewer=person)`;
   - the tool menu: the desktop modules (news, live TV, screen actions) plus `pc.delete_test_file`. `pc.list_test_folder` is called by core itself (§9.3), not offered to Claude.
5. Core calls the router (§10). As speech arrives, core writes `conv.stream_chunk` rows (`seq` 1, 2, …) and NOTIFYs `jarvis_stream` with only the `reply_id`.
6. The gateway, woken by NOTIFY, reads the new chunks from its last `seq` and sends them to every connected node in the session. **NOTIFY is only a wake-up; the chunks are the durable stream.** If either process restarts mid-reply, the gateway resumes from the node's durable acknowledged cursor (`conv.stream_cursor`). A `resume` position the node reports is used only when it is within the stored range; otherwise the cursor is used (P2-E03; R5-2).
7. When the reply is complete, core writes the final chunk (`final = true`) and the JARVIS `conv.message`.
8. **Tool calls go to policy (§9), never straight to a node.** A READ_ONLY module call, such as "show the news", is part of the live reply: core writes it as a `conv.stream_chunk` of type ACTION with a core-signed `authz` (§7.1) and `valid_until` = now + 2 minutes, and the gateway sends it as `node.action` to the node that spoke. The gateway, the node and core each enforce `valid_until` (§9.4); a node that reconnects later never replays it. It does **not** go in the outbox. An IRREVERSIBLE call becomes an approval, which does go in the outbox (§9.3), and the action it leads to is sent only after approval.
9. The node acknowledges what it rendered. The audit trail records the turn as codes only.

**Latency target:** the first spoken sentence within 3 seconds of the end of speech, measured on the desktop, as today. Postgres hops add a few milliseconds each. If the target is missed, the timing logs show where (§15).

### 6.3 A server-initiated operation (F2)

Something happens inside the server: an approval reaches its warning time, queued work finishes, Claude comes back, or the cap reaches 80%.

1. The handler writes `comm.event`.
2. It enqueues `system.event`.
3. The notification rules (§8.1) create outbox items.
4. The gateway delivers them to whichever of the person's nodes are online, or holds them.

No node request is involved (P2-E01).

## 7. Node protocol (ADR-012)

### 7.1 Envelope, signatures and trust

Every message uses `contracts/node-envelope.schema.json`, made final in Phase 2. It adds two fields, `seq` and `sig`.

**`seq` counts per sender, per connection (R6-8).** Each side keeps its own counter for the messages **it sends** on a connection. The counter starts at 1 on the first message (`challenge` for the gateway; `hello` or `enrol` for the node) and goes up by exactly 1 per message. Each side checks the **other side's** counter independently: the next message from the peer must carry the previous peer `seq` + 1. A gap, a repeat or a decrease is a protocol error, so the receiving side closes the connection: the gateway with 4401 and audit `bad_seq`; the node without replying. The two directions never share or compare counters. A new connection starts both counters again at 1.

**Canonical form and signature input (exact):**
- **Serialisation:** RFC 8785, the JSON Canonicalization Scheme (JCS). Python uses the `rfc8785` package and Rust uses `serde_jcs`. Numbers in signed objects are integers only (amounts in paise, times as RFC 3339 strings), so JCS's number rules never come into play.
- **Timestamps (R6-4).** Every timestamp in a protocol message or signed object is an RFC 3339 string **with an explicit offset**, such as `2026-11-22T10:02:00+05:30` or `…Z`. A timestamp without an offset is malformed and rejected. Database records, such as `ledger.approval`'s own columns, use Postgres `timestamptz` and are not wire formats. When a database value goes into a message or signed object (an approval card's `valid_until`, for example), it is written in this RFC 3339 form.
- **Signed bytes:** `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`.
- **Signature:** Ed25519 over those bytes, base64url without padding.
- **Shared test vectors:** `contracts/signing-vectors/` holds objects, keys, purposes and expected signatures. The vectors are checked by both the Python and the Rust test suites in CI, so the two can't drift.

**Three signing purposes, three keys:**

| Purpose | Signed by | Covers | Accepted by |
|---|---|---|---|
| `node-msg` | the node's own key | every envelope a node sends (all fields except `sig`) | gateway, checked against `gw.node.public_key` |
| `gw-transport` | `gateway_signing_key` | every envelope the gateway sends (all fields except `sig`) | nodes, for transport only: proves the message came from the gateway, and **never authorises an action** |
| `core-authz` | `core_signing_key` | the `authz` object inside `node.action` and approval `outbox.item` payloads | nodes, for authorisation |

The purpose string is inside the signed bytes, so a signature made for one purpose never verifies as another, even if the same key were misused.

**The core authorisation object (`authz.object`), all fields signed:**
`v` (1), `kind` (`node.action` or `approval.card`), `action_kind`, `args` (for the delete: name, size, SHA-256, file ID), `target_node_id`, `person_id`, `approval_id` (null for read-only actions), `execution_key` (null on a card), `reply_id` (live actions), `summary` (cards), `issued_at`, `valid_until`.

`authz` = `{object, key_id, sig}`. Core writes it when it creates the stream chunk or the outbox row, and the gateway passes it through unchanged inside its own `gw-transport` envelope.

**A node runs an action only if all of these hold:**
1. A valid `gw-transport` envelope arrived on its authenticated connection.
2. A valid `core-authz` signature from a trusted core key.
3. `target_node_id` is itself.
4. `now ≤ valid_until`, with 30 s of clock tolerance.
5. For IRREVERSIBLE actions: an `approval_id` and an `execution_key` it hasn't used (§9.4).

A valid gateway signature alone is never enough.

**Trust distribution and rotation:**
- **Pinning at enrolment:** the enrolment code Manj types into the desktop (§7.2) contains the fingerprints of the current core and gateway public keys. During enrolment the node receives the keys and pins them only if they match the fingerprints, so the gateway can't substitute its own key for core's.
- **Rotation:** core announces a new key in a `keyset` message whose `authz` object (kind `keyset`) is signed by the **current** core key. Nodes add the new `key_id`, accept both keys for a 24 h overlap, then drop the old one. The gateway key rotates the same way, but its announcement is signed by core.
- **Key announcements expire (R6-2).** The announcement's validity is the `issued_at` and `valid_until` fields **inside the signed `authz.object`**. Core sets `valid_until` = `issued_at` + 24 h, the length of the overlap. A node rejects a `keyset` when `now > valid_until + 30 s`, the same tolerance as actions, so an old but correctly signed announcement can't be replayed later. A node that missed the window re-enrols (R5-1). The node also rejects a `keyset` whose payload differs from `authz.object.args`.
- **Gateway and core keys are never interchangeable (R6-3).** A signature made with the gateway's key is never accepted as a core signature, even under the core key's `key_id`. The node verifies a `core-authz` signature only with the public key pinned for that `key_id` as a CORE key. The shared rule cases (`contracts/signing-vectors/authz-cases.json`) include this case, and both the Python reference checker and the Rust node must reject it.
- **A node that missed the overlap re-enrols** (Revision 5, R5-1):
  - Activation waits for every active node to acknowledge, or 24 h.
  - Nodes that hadn't acknowledged are marked STALE.
  - **Refusing a stale node (R6-6):** every connection, a stale node's included, first receives the pre-authentication `challenge` (§6.1 step 2). The challenge authorises nothing and carries no Jarvis content: a nonce, times and the gateway's key. After a STALE node's `hello` passes path A's checks, the gateway closes with code **4409** (`reenrol_required`) **instead of** `welcome`. It sends no welcome, summary, outbox item, reply chunk or action. If the gateway key itself was rotated, the stale node can't verify the challenge (an unpinned key) and closes before sending `hello` (§6.1 step 3).
  - A node that sees 4409, or any signature with an unknown `key_id`, runs nothing and asks to be re-enrolled.
  - `jarvis-core node reenrol <node_id>` revokes the old node and issues a new enrolment code carrying the current fingerprints.
  - A node never accepts a key from the gateway alone.
- **Compromise:** if the core key leaks, every node is re-enrolled (there's a runbook step). The keys are kept in the secret files, so a leak needs server access.

**Message kinds:**

| Kind | Direction | Payload |
|---|---|---|
| `challenge` | ← | `nonce`, `issued_at`, `expires_at`, `key_id`, `public_key` (gateway's); first message on every connection (§6.1, R6-1) |
| `enrol` | node → | token, public key, `nonce` (§7.2) |
| `hello` / `welcome` | node → / ← server | handshake (§6.1); `hello` carries `nonce`, `resume` (each `last_seq` any integer; validated by the gateway per §6.2 step 6 and R5-2, so a negative value means 0), optional `clock_skew_ms`; `welcome` carries the open session, the reconnect summary and, after an enrolment only, the `enrolment` block (`node_id`, `core_key_id`, `core_public_key`; §6.1 step 7) |
| `utterance` | → | `text`, `screen_summary` |
| `reply.chunk` | ← | `reply_id`, `seq`, `type` (SPEECH or ACTION), `text` or `authz` (ACTION), `final` |
| `outbox.item` | ← | `outbox_id`, `priority`, `kind`, `body`, `expires_at`, `authz` (approval cards) |
| `outbox.cleared` | ← | `outbox_id`, `reason` (ACKNOWLEDGED, SUPERSEDED, EXPIRED) |
| `received` / `ack` | → | `outbox_id` or `reply_id` (§8.2) |
| `approval.decision` | → | `approval_id`, `decision`, `card_sig` |
| `node.action` | ← | `authz` (§7.1) |
| `action.receipt` | → | `execution_key` (or `reply_id` + `seq` for live actions), `result`, `evidence` |
| `keyset` | ← | new role, `key_id`, public key, and `authz` (kind `keyset`) signed by the current core key; the payload must equal `authz.object.args` |
| `ping` / `pong` | both | heartbeat, every 30 s; 3 missed = offline (v2 §13.3) |

Payload schemas go in `contracts/` with examples and contract tests, before feature code (v2 §15).

### 7.2 Registering a node (my call; hardened per review)

1. **Manj creates an enrolment.** He runs `jarvis-core node enrol --person manj --kind desktop --name "study PC"` on the server over Tailscale SSH (`docker compose exec core …`). It calls **`gw.create_enrolment(token_hash, person_id, kind, name)`**, a SECURITY DEFINER function that only `jarvis_app` may execute (R7-2). It writes a row in **`gw.enrolment`** (`id`, `token_hash` (SHA-256 of a 128-bit random token), `person_id`, `kind`, `name`, `expires_at` = now + 15 min set by the database, `used_at`, `node_id`) and an `ENROL_CREATE` audit row, in one transaction. It prints an **enrolment code**: the token plus the core and gateway key fingerprints. Nothing in the repo or the build workspace sees it.
2. **The desktop connects and checks the gateway.** It generates its key pair and stores the private key with Windows DPAPI. It connects; the first message is the gateway's `challenge` (§6.1 step 2). The device verifies it with the challenge's `public_key` **only if** `fingerprint(public_key)` equals the gateway fingerprint in the enrolment code, and if so pins that gateway key. Otherwise it closes and sends nothing.
3. **The desktop sends `enrol`:** `{token, public_key, nonce}`, envelope `node_id` = the nil UUID, `seq` = 1, signed `node-msg` with the new key, which proves it holds that key.
4. **The gateway registers it** (§6.1 step 6, path B). The gateway reads the peer's Tailscale identity from `whois` **for this connection**, never from the message, and calls `gw.enrol_node(token, public_key, tailscale_identity)`. In one transaction the function:
   - runs `UPDATE gw.enrolment SET used_at = now() WHERE token_hash = sha256(token) AND used_at IS NULL AND expires_at > now() RETURNING …`. Zero rows → write the REJECTED audit row and return the refusal `bad_token` (no raise, R7-1), so two concurrent uses of one token can't both succeed and every failed attempt stays on the audit trail;
   - **inserts** a new `gw.node` (new UUID, `person_id` and `kind` from the enrolment row, not from the caller), with unique constraints on `public_key` and on active `tailscale_identity`. A clash returns the refusal `key_in_use` or `identity_in_use`, audited, and the token stays unused because nothing in the transaction applied. It never updates an existing node, so it can't change another node's key or identity;
   - records `node_id` on the enrolment and writes an audit row.
5. **The core key is pinned from the code, not from the gateway.** The gateway sends `welcome` with the `enrolment` block (`node_id`, `core_key_id`, `core_public_key`), as in §6.1 step 7.
   - **Match:** if `fingerprint(core_public_key)` equals the core fingerprint in the enrolment code, the device pins the key as a CORE key and stores `node_id`. The connection is then the authenticated connection of that new node. No second handshake is needed: the token, the proof of key possession and the Tailscale binding were all checked in step 4.
   - **Mismatch:** the device discards its new key pair, stores nothing, closes the connection, and shows "Enrolment failed: the server's key does not match the code." The node row created in step 4 is orphaned; its token is used up. Manj revokes it (`jarvis-core node revoke`) and issues a new code.

   A gateway can't substitute a different core key, because only a key matching the out-of-band fingerprint is ever pinned.
6. **Later connections** use the normal path: `challenge` → `hello` (path A) → `welcome`.

No job is involved, so enrolment works with core down.

**Accepted limitation:** the database trusts the gateway's `tailscale_identity` argument, because only the gateway can see the connection. A compromised gateway could enrol a node only with a valid, unused, unexpired token, and the token is what limits it.

**Revoking a node:** `jarvis-core node revoke <node>` calls `gw.revoke_node(node_id)` (a core role call; the gateway has no grant). It returns an explicit result: applied, or refused (`unknown_node`, `already_revoked`), audited either way, and the CLI exits non-zero on a refusal after the transaction commits. The gateway re-checks `revoked_at` on every handshake and every 60 s for open connections (P2-E10).

## 8. Notifications, delivery and reconnection

### 8.1 Notification rules (deterministic, in code, with tests)

| Event | Priority | Outbox behaviour |
|---|---|---|
| Approval requested | URGENT if it expires within 1 hour, otherwise TODAY. **In Phase 2 every approval expires after 10 minutes, so every approval is URGENT. This is intended**: a short-lived approval needs attention now. The TODAY branch is there for longer-lived approvals in Phase 3. | Expires with the approval; superseded when decided (on any node) |
| Approval 2 minutes from expiry, still pending | URGENT | Re-surfaces the card; superseded by the decision or the expiry |
| Approval expired | TODAY | "Expired, not done" |
| Queued turn completed after Claude came back | TODAY | The reply itself, in its session |
| Claude down | DIGEST (TODAY if a turn is held) | One item, updated in place through `supersedes_id` |
| Claude back | DIGEST | Supersedes "down" |
| Cost cap at 80% / 100% | TODAY / URGENT | |
| Held turn expired (cap) | TODAY | In its session; supersedes the "held" item |
| Node action succeeded (READ_ONLY) | SILENT | History only |

### 8.2 Delivery to several nodes (exact rules)

**Who it goes to:** every non-revoked node of `comm.outbox.person_id`, decided at send time, so a node enrolled later also gets items that are still open.

**Two node messages:**
- `received`: sent automatically when an item arrives on the node. It confirms the transport.
- `ack`: sent when the person has seen the item (shown on screen, or spoken) or acted on it (an approval decision counts as `ack`).

**Item states (`comm.outbox.state`) and what they mean:**

| State | Meaning |
|---|---|
| PENDING | No node has been sent it yet |
| DELIVERING | Sent to at least one node; no node has confirmed receipt |
| DELIVERED | At least one node confirmed receipt (`received`); nobody has seen it yet |
| ACKNOWLEDGED | **Some node** acknowledged it. Terminal for delivery. |
| SUPERSEDED / EXPIRED | Terminal, from any non-terminal state |

States only move forward (PENDING < DELIVERING < DELIVERED < ACKNOWLEDGED), and terminal states never change.

**Per node (`comm.delivery`):** `record_sent` creates or updates the `(outbox_id, node_id)` row (attempts + 1); `record_received` sets `received_at`. These rows say which nodes have it. The item state says whether the person has dealt with it.

**Acknowledgement on one node while another is offline:** the item becomes ACKNOWLEDGED, and the gateway sends `outbox.cleared` (with the reason) to every other connected node that has it, which removes it from screen. A node that was offline doesn't get it as a live item when it reconnects: it isn't in the reconnect summary, and it appears only in history. That's P2-E05.

**Retries keep the ID:** the sweep (every 60 s) re-sends items in DELIVERING or DELIVERED whose `last_sent_at` on a connected node is more than 2 minutes old. It's always the same `outbox_id`. Each node keeps a durable seen-list of `outbox_id`s (in its local journal, §13) and shows an ID only once, even across restarts. That's P2-E04.

**Serialisation:**
- Every transition function locks the item row (`SELECT … FOR UPDATE`) and checks the current state before changing it.
- `record_sent` refuses (returns "don't send") if the item is already terminal, so a send racing an acknowledgement either completes before the acknowledgement, and the node then receives `outbox.cleared`, or is refused.
- A late `received` or `ack` for a terminal item is recorded on the delivery row and changes nothing else.
- Supersede and expire use the same lock, and also send `outbox.cleared`.

**Tests:** P2-E04 and P2-E05, plus P2-E13: two fake nodes, with concurrent acknowledgement, re-send and supersede in random order over 1,000 runs. The final state must always be consistent, with no item shown twice on one node.

### 8.3 Reconnect summary (no Claude needed; P2-E07, P2-E08)

Built by code from the outbox:

| Priority | On reconnect |
|---|---|
| URGENT | First, one at a time |
| TODAY | Grouped by `group_key` |
| DIGEST | One summary line per group, from fixed templates |
| SILENT | History only |

An approval always shows its current server state (P2-E11). Nodes never decide from a cached copy: tapping "approve" on a stale card sends the decision, and the server's answer is what the node shows.

**Known Phase 2 gap:** with the desktop off and no phone yet, an URGENT item can only wait. Phase 2b is what makes URGENT reach Manj straight away.

## 9. Policy engine and approvals

### 9.1 Tiers (deterministic table, in code)

| Action kind | Tier | Why |
|---|---|---|
| `module.news`, `module.live_tv`, `screen.*` | READ_ONLY | Shows things |
| `memory.write` (STATED, by the speaking person) | REVERSIBLE | Shown in history |
| `pc.list_test_folder` | READ_ONLY | Returns file names, sizes and SHA-256 hashes in the test folder |
| `pc.delete_test_file` | IRREVERSIBLE | v2 §12.2: deleting files needs approval |
| anything not in the table | **refused** | Default deny |

### 9.2 Checks before any action

1. **Identity:** the person comes from authentication.
2. **Scope:** the action kind is in the table, and the node kind can run it.
3. **Purpose:** the action belongs to the current session.
4. **Approval:** for IRREVERSIBLE, an APPROVED approval that is still in date and not yet used.
5. **Memory:** if the decision used memory entries, `memory.assert_actionable(ids)` must pass (Phase 1 M3).

A failure is refused, audited and explained in the reply.

### 9.3 Approval flow (P2-A1)

1. Claude proposes `pc.delete_test_file(name)`.
2. **Before an approval is created**, core sends `pc.list_test_folder` to the requesting desktop as a live action and waits for its receipt (timeout 10 s). For each file the listing returns the name, size, SHA-256 and **Windows file ID** (volume serial number + 128-bit file ID, from `GetFileInformationByHandleEx(FileIdInfo)`). If the named file isn't there, Jarvis says so and nothing is proposed. Otherwise those four values are stored in the approval's `action_args`, so the person approves deleting **that exact file object**.
3. Policy finds IRREVERSIBLE. Core creates a ledger task through the Atlas interface (§11) and a `ledger.approval` with a 10-minute expiry and the summary "Delete test file `x.txt`?". Both happen in one transaction.
4. Core writes the approval card as an URGENT outbox item, with a signed `authz` (kind `approval.card`), and Jarvis says "That needs your approval."
5. The desktop shows the card. Manj approves by tap or by voice ("approve"). The node sends `approval.decision` (signed `node-msg`), including the card's `authz.sig`, so the decision is tied to exactly that card.
6. Core handles the `approval.decision` job. In **one transaction** it:
   - **verifies the decision before anything else** (all must hold, checked under `SELECT … FOR UPDATE` on the approval row):
     - the supplied `card_sig` is byte-for-byte equal to `ledger.approval.card_authz.sig`, and that stored signature verifies over the stored card object with a trusted core key. The decision therefore refers to exactly the card core signed and the person saw;
     - the approval's `requested_from_person_id` equals the person of the authenticated node that sent the decision, taken from `conv.inbound.person_id` (filled from `gw.node`, §4.1), never from the payload;
     - the sending node is not revoked, and the card's `person_id` matches as well;
     - `decision = PENDING` and `now() < expires_at`.

     If any check fails, nothing is decided or consumed. An audit row (REJECTED, with the failing check as a code) is written, and the node is told the decision was refused and shown the approval's real server state;
   - sets the decision through the guarded trigger;
   - calls `ledger.consume_approval()`;
   - writes the signed `node.action` (kind `node.action`, the four file values, the `execution_key`, `target_node_id`, and `valid_until` = the earlier of now + 2 min and the approval expiry) as an outbox item addressed to the requesting node only.

   If any step fails, nothing is issued.

### 9.4 Execution, receipt and verification

**Issued once, atomically (server):**
```sql
UPDATE ledger.approval
   SET consumed_at = now(), execution_key = gen_random_uuid()
 WHERE id = $1 AND decision = 'APPROVED' AND consumed_at IS NULL AND expires_at > now()
RETURNING execution_key;
```
- **Zero rows → nothing is issued.** It was denied, expired or already consumed. Two concurrent decision jobs for one approval can't both issue an action, because the row lock serialises them and the second sees `consumed_at` set.
- A retried job, after a crash, finds the approval already consumed and the action already in the outbox, and does nothing more (ADR-013 idempotency).
- Receipts are accepted only for an issued `execution_key`, once.

**Path safety (desktop):**
- The test folder is set in desktop config, as an absolute path on a local NTFS volume. At startup and before every action the desktop opens the folder itself with `FILE_FLAG_OPEN_REPARSE_POINT` and refuses to act if the folder is a reparse point (symlink, junction, mount point).
- `name` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`, so it can't contain separators, `..`, a drive letter, an alternate data stream (`:`), or a device name (CON, NUL, … are refused explicitly).

**Same file object (desktop), with no gap between check and use:**
1. Open the file relative to the folder with `CreateFileW(..., GENERIC_READ | DELETE, share mode 0 (exclusive), FILE_FLAG_OPEN_REPARSE_POINT)`. If the file is a reparse point, or isn't a regular file, refuse.
2. Through **that handle**, check that:
   - the final path (`GetFinalPathNameByHandle`) is directly inside the test folder;
   - the file ID equals the approved file ID;
   - size and SHA-256 (read through the handle) equal the approved values.

   Any mismatch → refuse, with receipt `result = MISMATCH`.
3. Still through the same handle, rename it into the staging subfolder `<test folder>\.jarvis-pending\<execution_key>` (`SetFileInformationByHandle(FileRenameInfoEx)`, same volume, atomic). Then close the handle. The exclusive handle means no other process could change or replace the file between steps 1 and 3.
4. **Recycle from protected staging (fail closed).** `IFileOperation` works on paths, so the staged path is protected and re-verified immediately before use:
   - **The staging folder is the desktop's own.** At startup the desktop creates `.jarvis-pending` itself if it is missing, opens it with `FILE_FLAG_OPEN_REPARSE_POINT`, and refuses to use it if it is a reparse point. It sets a protected DACL (inheritance off) granting access only to the signed-in user and SYSTEM, and reads the DACL back to confirm. Each execution uses a fresh subfolder, `<execution_key>`, created with `CreateDirectoryW`, which fails if the name already exists, and given the same checks.
   - **Threat model:** another process running as the same signed-in user with intent to race the desktop is out of scope. It could already change anything that user owns. In scope are accidental changes, other users and services, and link-based tricks. The checks above close those off.
   - **Verify, then recycle, keeping hold of the file:**
     - immediately before recycling, re-check that the staging subfolder is not a reparse point and its DACL is unchanged;
     - re-open the staged file with `FILE_FLAG_OPEN_REPARSE_POINT` and share mode `FILE_SHARE_DELETE` only, so nothing else can open it for read or write while the handle is held;
     - confirm through that handle that the file ID equals the approved ID and the final path is `<staging>\<execution_key>\<name>`;
     - with the handle still open, call `IFileOperation` (`FOFX_RECYCLEONDELETE`) on that path. It's never a permanent delete;
     - **after the call**, confirm through the still-open handle that the object now lives in the Recycle Bin (its final path is under `$Recycle.Bin`). Then close the handle.
   - **Fail closed:** if any precondition can't be established, `IFileOperation` is **not** called. That covers the folder being created or checked, the DACL being set or confirmed, the file ID matching, and the final path matching. The file stays in staging (still recoverable), and the receipt is `REFUSED` with the failing check. If the post-check shows the handle's object did **not** move to the Recycle Bin, the receipt is `ERROR` with what was found, and the task is never marked done. Anything that was moved is in the Recycle Bin, so nothing is permanently lost.

**Durable idempotency on the desktop (survives restarts):**
- An append-only journal file in the app's data folder (fsync after every write) records `execution_key → STARTED`, before step 1, and then `→ DONE + receipt`.
- **On restart:**
  - a DONE key → re-send its stored receipt, never execute again;
  - a STARTED key → work out where it got to: file still in the folder (not moved), in `.jarvis-pending\<key>` (moved, not recycled → redo step 4 in full, with every check), or gone (done). Then send the matching receipt.
- A `node.action` whose key is already in the journal is never executed again, and its stored receipt is re-sent.
- The journal also holds the outbox seen-list (§8.2).

**Receipt and verification at source:**
- The desktop returns `action.receipt` with the `execution_key`, `result` (RECYCLED, MISMATCH, NOT_FOUND, REFUSED, ERROR) and evidence: file ID, before-hash, and the time it was recycled.
- Core accepts only one receipt per key and checks that file ID and hash match the approval. Then it sends a fresh `pc.list_test_folder` and checks the file ID is absent. Only then does it close the task with a verified `ledger.outcome`.
- If any check fails, the task is FAILED and Jarvis says so. It is never marked done (v2 §13.3).
- **Phase 2 limit:** a node reporting on itself is the most Phase 2 can do. The independent verifier comes in Phase 3.

**Live (read-only) actions also expire on both sides:** the gateway won't send an ACTION chunk past its signed `valid_until`; the node refuses one past `valid_until`; core refuses receipts for expired actions.

**Negative cases:**

| Test | Case | Expected |
|---|---|---|
| P2-A2 | No approval | Refused |
| P2-A3 | Expired approval | Refused; approval EXPIRED; nothing issued |
| P2-A4 | Revoked node | Can't connect, so can't execute |
| P2-A5 | Duplicate `node.action`, or two concurrent decision jobs | One execution only |
| P2-A6 | File swapped (same name, different content or file ID) after approval | MISMATCH; nothing recycled |
| P2-A7 | Symlink or junction as the file or the folder, or a traversal name | Refused |
| P2-A8 | Desktop killed between STARTED and DONE | On restart it finishes or reports correctly, never executes twice |
| P2-A9 | Decision with a wrong or altered `card_sig`, from another person's node, after expiry, or for an already-decided approval | Refused and audited; nothing decided or consumed |
| P2-A10 | Staging folder or staged file changed before recycling (reparse point, permissions changed, file ID differs) | Recycle not called; file left in staging; receipt REFUSED |

## 10. Brain router (Claude only, D4, D8)

```python
class Brain(Protocol):
    def think(self, request: BrainRequest) -> Iterator[BrainEvent]: ...   # Speech | ToolCall
```

- **Provider:** Claude through the Anthropic API, streaming, with tool use, reached via the egress proxy (§4.2). The model name is in config. Prompts and outputs are provider-neutral types, so a second provider can sit behind the same interface later.

### 10.1 Budget reservation (safe under concurrency)

Every Claude call reserves its worst-case cost **before** it starts, under a row lock. No two calls can both spend the last of the budget.

1. **Estimate a ceiling.** `reserve_inr` = (input tokens of the request, counted locally) × input price + `max_tokens` × output price. Prices come from the config price table and are stored in paise. `max_tokens` is always set on the request, so the actual cost can't exceed the reservation.
2. **Reserve, in one transaction:**
   - lock the month's row in `ops.brain_budget` (`SELECT … FOR UPDATE`; the row is created for the month if missing);
   - allow the call only if both of these hold, otherwise reserve nothing:
     - `settled_inr + reserved_inr + reserve_inr ≤ cap_inr`;
     - today's `settled + reserved + reserve_inr ≤ daily ceiling`, where the daily ceiling = (cap − spent at the start of today) ÷ days left in the month, including today;
   - add `reserve_inr` to `reserved_inr`;
   - insert an ACTIVE `ops.brain_reservation`, with `lease_until` = the job's lease.
3. **Settle** after the call, in one transaction: compute the actual cost from the API's reported usage; move the reservation to SETTLED with `actual_inr`; move the amount from `reserved_inr` to `settled_inr`; write `ops.brain_usage`.
4. **Release only when no request reached Claude.** The router runs every call in two phases, so the boundary is in the code, not guessed from error types:
   - **Connect phase:** open the connection through the egress proxy (`CONNECT`), then finish the TLS handshake with `api.anthropic.com`. No request bytes have been written yet.
   - **Send phase:** begins the moment the first byte of the HTTP request is written to the established TLS connection.

   **RELEASED** (the reservation is given back, and the turn may be tried again) only for failures in the connect phase:
   - proxy unreachable;
   - the proxy refusing `CONNECT`, which is our own proxy, so the request never went upstream;
   - DNS failure or TCP connect timeout;
   - a TLS handshake failure.

   **Any outcome after the send phase began that is not a complete, successful response is indeterminate** (step 6): every HTTP status (4xx, 429, 5xx, 529), any timeout, and any cut stream. The reservation becomes LAPSED, counted at full value. A response arriving doesn't prove the provider didn't process the request.

   An exception the router can't place in one phase or the other is treated as send phase (fail closed).
5. **Lapse after a crash:** a scheduler sweep finds ACTIVE reservations past `lease_until`. Each becomes LAPSED and its **full reserved amount counts as settled**, because tokens may have been billed and we can't tell. This is conservative: a crash can only make the meter over-count, never under-count.
6. **A lapsed call is indeterminate and is never retried as a new call.** Each reservation records the turn it serves (`turn_id` = the `conv.inbound` id). A LAPSED reservation means Claude may already have processed that turn.
   - **One owner per turn, enforced by the database:**
     - a partial unique index, `UNIQUE (turn_id) WHERE state IN ('ACTIVE', 'SETTLED', 'LAPSED')`, allows at most one live or spent reservation per turn. RELEASED rows are left out, so a turn whose call never reached Claude can reserve again;
     - the reservation transaction also locks the turn's `conv.inbound` row (`SELECT … FOR UPDATE`) **before** the month's budget row. That fixed lock order (turn, then budget) is used everywhere, so there are no deadlocks. It then checks the turn's existing reservations and inserts the new one in the same transaction;
     - two jobs for the same turn therefore serialise on the turn row. If they still race, the unique index refuses the second insert. A unique-violation error means "another job owns this turn".
   - **What a job does, by the turn's existing reservation:**

     | Existing reservation for this `turn_id` | The job |
     |---|---|
     | None, or only RELEASED | Reserves and may call Claude (it is now the owner) |
     | ACTIVE, lease **not** expired | **Does not call Claude.** Completes at once without changing anything: the owning job is still working and will finish the turn. |
     | ACTIVE, lease expired, or LAPSED | **Does not call Claude.** Lapses the reservation if not already done, then follows the indeterminate path below. |
     | SETTLED | **Does not call Claude.** The turn was answered; it completes without changing anything. |
     | Unique violation on insert | Same as "ACTIVE, lease not expired" |

   - **If the owning job crashes**, nothing waits on the second job. The reservation sweep lapses the reservation and, in the same transaction, marks the turn indeterminate and sends the notification. So the indeterminate path runs exactly once, from whichever comes first: the sweep or a later job. It is guarded by the same row lock and by `conv.inbound.outcome` being empty.
   - **The indeterminate path:** mark the turn `INDETERMINATE` (a new column, `conv.inbound.outcome`) and write an event. A TODAY notification goes to the session: "I may not have finished answering that. Please ask again if you still need it." It then completes the job. Any chunks already streamed stay in history, with no final chunk.
   - **The person asking again** is a new turn with a new reservation, which is the only way that turn's work runs again.
   - **A provider-side check could change this rule only by a later decision.** The Anthropic API offers no mechanism that reliably proves a request did not run, so Phase 2 assumes it might have.

   This keeps the exactly-once promise for held turns: a turn reaches Claude at most once without the person asking again. Test P2-B4.

The checks before a reservation are the same thresholds as before:

| Month-to-date | Effect |
|---|---|
| ≥ 80% | Event and TODAY notification |
| ≥ 100%, or the daily ceiling reached | No reservation, so the turn is held and an URGENT notification goes out. Manj raises the cap only by changing config and redeploying. |

**Cap holds expire (K2-5, unchanged):** a turn held by the monthly cap or the daily ceiling waits at most 1 hour, retrying the reservation every 5 minutes. If the budget still doesn't allow it, the turn is marked expired and Jarvis sends a TODAY notification in that session: "I couldn't answer that: the monthly limit is reached" (or "today's limit"). It never runs later. If the cap is raised within the hour, the turn runs once.

P2-B2 covers the thresholds. P2-B3: 20 concurrent turns against a budget with room for 3 → exactly 3 reservations granted; a crash mid-call lapses the reservation at its full amount.

### 10.2 Failure handling

- A Claude timeout or 5xx marks the brain DOWN after 2 consecutive failures.
- Jarvis tells the person (CLAUDE_DOWN, §8.1). Which turns wait for Claude to come back:
  - turns that arrive while the brain is DOWN: they are held **before** any reservation is made, so nothing has been sent;
  - turns whose call failed in the **connect phase**, whose reservation was RELEASED (§10.1 step 4).

  These are re-queued with a delay, not failed, and run once when Claude is back. A turn whose call entered the **send phase** and did not complete successfully is never re-queued for another call. That includes 4xx, 429, 5xx and 529 responses, timeouts and cut streams. It is LAPSED and indeterminate (§10.1 step 6), and the person is told. Both kinds of failure count towards marking the brain DOWN.
- `brain.probe` runs every 2 minutes, with a minimal request whose cost is reserved like any other. On success, CLAUDE_BACK is emitted and held turns run once, in order.
- Reminders, expiries, delivery and verification never call Claude, so they continue (P2-B1).
- **No direct desktop fallback (my call):** after cutover the desktop holds no Claude key. If the server is unreachable, the desktop says so locally ("I can't reach the server right now"). This keeps F1 true.

## 11. Atlas foundation (ADR-014)

```python
class Atlas(Protocol):
    def accept_plan(self, plan: ApprovedPlan, *, by: Actor) -> list[TaskId]: ...
```

Phase 2 has one implementation: one plan becomes one ledger task, moved through `ledger.transition()`. The task moves DETECTED → INTERPRETED → PROPOSED → APPROVAL_PENDING → READY → IN_PROGRESS → COMPLETED, or FAILED or EXPIRED. Every move writes the audit row it already writes.

Dispatch, dependencies, retries and specialists come in Phase 3 behind the same interface.

## 12. Memory integration

- **Viewer and writer:** `viewer` and `by` come from the authenticated person on every call. They are never taken from client input or from Claude's tool arguments (S3). Background work uses `system`.
- **Writes from conversation:** "remember that I prefer aisle seats" becomes a REVERSIBLE `memory.write` (STATED, by the speaking person), shown in history. Claude can only propose INFERRED entries. Confirming stays a person's own act (Phase 1 §7).
- **New call, `set_visibility(memory_id, visibility, shared_with, *, by)`:** deferred from Phase 1. Only the subject or the recorder may call it, it is audited, and it makes a new git version.
- **What's in memory on the shared server:** fictional people only (D11). The real-memory switch stays off until the dedicated server, encryption at rest and a verified restore are in place.

## 13. Desktop changes (Rust)

- **`ServerBrain`** implements the existing `Brain` trait. It sends `utterance` and turns `reply.chunk` into `BrainEvent::Speech` and `node.action` into `BrainEvent::Action`. The rest of the desktop (panels, voice, modules) is unchanged.
- **Gateway client:** a WebSocket client with reconnect (backoff up to 30 s), JCS signing and checking (§7.1, shared test vectors), pinned core and gateway keys, `seq`, ping/pong, and `received` / `ack`. Outbox items are shown once per `outbox_id`; `outbox.cleared` removes them.
- **Local journal:** an append-only, fsync'd file in the app's data folder, holding execution keys and their receipts (§9.4) and the outbox seen-list (§8.2).
- **Approval card panel:** summary, time left, approve and deny. It shows the server's state after a decision.
- **`pc.list_test_folder` and `pc.delete_test_file`:** limited to one configured test folder, following the path-safety and same-file rules in §9.4 exactly. The delete goes to the Recycle Bin, never a permanent delete.
- **Enrolment screen and key storage:** DPAPI.
- **Cutover:** a config switch between `ClaudeBrain` (today) and `ServerBrain`. The Claude key is removed from the desktop once Phase 2 is accepted.

## 14. Acceptance tests

Architecture tests (agreed at kick-off):

| Test | Proves |
|---|---|
| P2-E01 | Server initiates an event without a node request |
| P2-E02 | Offline node does not block server operations |
| P2-E03 | Outbox survives a server restart (and a reply stream resumes from the last chunk) |
| P2-E04 | Retry keeps the notification ID |
| P2-E05 | Acknowledgement on one node clears outstanding delivery everywhere |
| P2-E06 | Superseded and expired notifications stay in history |
| P2-E07 | Reconnection order follows the four priority levels |
| P2-E08 | Bundling works with Claude unavailable |
| P2-E09 | A conversation resumes on a different node |
| P2-E10 | A revoked node can't authenticate |
| P2-E11 | An approval can't be acted on after expiry |
| P2-E12 | The server keeps operating with every node offline |
| P2-E13 | Multi-node delivery: concurrent acknowledgement, re-send and supersede across two nodes in random order (1,000 runs) always ends consistent, with no item shown twice on one node |

Flow tests:

| Test | Proves |
|---|---|
| P2-A1 | Voice request → approval card on the desktop → approve → test file deleted → receipt verified → spoken result; full audit trail |
| P2-A2 … A10 | The negative cases in §9.4: no approval; expired; revoked node; duplicate or concurrent execution; swapped file; symlink, junction or traversal; desktop crash mid-action; decision not matching the signed card or person; staging tampered |
| P2-B1 | Claude down: turn held, person told, reminders, expiries and delivery continue; Claude back: held turn runs exactly once |
| P2-B2 | Cost cap: 80% warns; 100% refuses new calls; the daily ceiling holds; a cap-held turn expires after 1 hour with a notification and never runs later |
| P2-B3 | Budget under concurrency: 20 concurrent turns, room for 3 → exactly 3 reserved; a crashed call's reservation lapses at full value |
| P2-B4 | Core killed after Claude was called but before the reply was stored: the retried job makes **no** second Claude call; the turn is INDETERMINATE and the person is notified once. A 5xx, 429 or timeout after the request was sent does the same. A proxy-refused or TLS-failed connection is RELEASED, re-queued and answered exactly once. |
| P2-B5 | Two jobs for the same turn claimed concurrently (and, separately, a duplicate inserted by hand): exactly one Claude call; the second job completes without calling; if the owner is then killed, the sweep marks the turn indeterminate exactly once |
| P2-S1 | Gateway role can't read memory, call `ledger.transition()` or write `audit.event` |
| P2-S2 | Person and viewer come from the node row even when the message claims another person |
| P2-S3 | A bad signature, a replayed `seq` or an unknown Tailscale peer is refused |
| P2-S4 | Signing: the shared JCS test vectors pass in Python and Rust; an action carrying only a valid gateway signature, a core signature for another node, or a past `valid_until` is refused by the node |
| P2-S5 | From inside the gateway container, every outbound connection except Postgres and tailnet replies fails; compose contains no `ports:` |
| P2-S6 | Enrolment privileges: token reuse, expired token and two concurrent uses of one token (one wins); the gateway can't insert into `gw.node` directly, change a node's key or identity, or read `gw.enrolment`; every SECURITY DEFINER function has a fixed `search_path` (checked from `pg_proc`) |
| P2-S7 | Core can reach `api.anthropic.com` through the proxy and nothing else |

Tests use fake nodes (a Python WebSocket client), and P2-B tests use a fake brain. One live check on the desktop runs P2-A1 by hand. Every test that can run in CI does (Postgres 17, as now). init's smoke check gains rows for the E- and S-tests that need no node.

## 15. Testing, observability and deployment

- **Timing logs:** gateway receive → job claim → first Claude token → first chunk sent, so latency can be read per hop.
- **Compose:**
  - **New services:** `gateway-ts` (§4.2) and `egress-proxy`, both pinned by digest.
  - **gateway:** a real command; `network_mode: service:gateway-ts`; the `pg_gateway_password` and `gateway_signing_key` secrets; the shared local-API socket volume; a 256 MB limit.
  - **core:** stays on `jarvis_internal` only and gets `HTTPS_PROXY` pointing at the egress proxy, plus the `anthropic_api_key` and `core_signing_key` secrets.
  - **No service has `ports:`**, enforced by a CI check.
- **Smoke:** init prints the new SMOKE lines. The runbook (`server/deploy/README.md`) gains:
  - node enrolment and revocation;
  - core key rotation and the re-enrol runbook;
  - creating the Tailscale auth key and the ACL rule for `tag:jarvis-gateway`;
  - the gateway checks.
- **P0-8:** the external port scan must still show only 22/80/443.

## 16. Approaches considered (gateway and core split)

| Approach | Trade-off | Decision |
|---|---|---|
| **A. A separate gateway process, with Postgres in between** | Durability and the security boundary are structural: the gateway holds no Claude key or memory, and a restart of either side loses nothing. Matches v2 §14. Cost: streaming passes through Postgres (chunks plus a NOTIFY wake-up), and there is one more container. | **Chosen (Manj, 22 Nov)** |
| B. The gateway as a module inside core | Simplest, with direct streaming. But the internet-facing code shares a process with the Claude key and memory, a gateway crash takes the brain down, and the split would be needed later under load. | Rejected |
| C. A separate gateway calling core over an internal HTTP API | Two network APIs to secure. Durability depends on core remembering to write rows. No gain over A. | Rejected |

## 17. Left to the builder

- Library choices inside the stack: the Rust WebSocket library; Ed25519 crates and packages (`ed25519-dalek` and `cryptography` suggested). The JCS libraries are fixed in §7.1.
- The egress proxy implementation (a small allowlisting CONNECT proxy, for example tinyproxy or a ~100-line Python one), as long as it enforces the single-host rule and P2-S7 passes.
- Exact message windows and prompt layout, the model name, and the price table values.
- Order of work inside each step, and how to split it into PRs.

## 18. Order of work (for the plan)

1. Contracts: envelope v1 final, payload schemas, examples, the JCS signing test vectors (Python and Rust), and ADR-011 to 014.
2. Migrations 0008+ (`gw`, `conv`, `comm`, approval, brain usage, gateway role and grants) with tests, including P2-S1.
3. Gateway: handshake, enrolment, inbound, streaming, delivery and acks (fake node tests).
4. Core: job handlers, the conversation turn with a fake brain, events and notification rules, reconnect summary.
5. Brain router: Claude, cost meter, down and back.
6. Policy, approvals and the Atlas interface; memory integration and `set_visibility`.
7. Desktop: `ServerBrain`, gateway client, approval card, test-file action, enrolment.
8. Deploy on the shared server; live P2-A1; reviews (QA, security).
9. **Track D11 (separate workstream, §19):** its own short design and acceptance criteria D11-1 to D11-8. It runs alongside steps 3–8. Phase 2 is declared complete only when both the build and this track pass.

## 19. Track D11: the dedicated server (separate workstream)

It gets its own short design and its own acceptance criteria. It can run alongside the build. **Phase 2 is complete only when both the build (§14) and this track pass.**

| # | Criterion |
|---|---|
| D11-1 | JARVIS runs alone on its own server: none of the shared projects; hardening as in Phase 0 (Tailscale, SSH key-only, firewall 22/80/443); P0-8 passes |
| D11-2 | Encryption at rest for the Postgres and memory volumes (method chosen in the D11 design) |
| D11-3 | Off-server encrypted backups of the database dumps and memory bundles, outside the server's provider account or disk |
| D11-4 | **Restore drill (P0-7) passes:** rebuild from the repo plus a backup onto a clean machine, with ledger, audit, memory history and conversations intact |
| D11-5 | S4: image digests pinned in compose |
| D11-6 | `conv.*` and `comm.*` emptied of test-period content before the move (§5.7); fictional memory removed |
| D11-7 | The full Phase 2 acceptance set re-run on the new server |
| D11-8 | Only after D11-1 to D11-7: Manj's go-ahead to turn on real memories |

## 20. Review changes (Manj, 22 Nov 2026)

Architecture direction approved; implementation approval pending these changes. The four ADRs are unchanged and stay Proposed.

| # | Review point | Resolved in |
|---|---|---|
| 1 | Gateway network contradiction | §4.2: no host port; reachable only through the sidecar; binds only the tailnet address; egress blocked by UID |
| 2 | Budget safe under concurrency | §5.6, §10.1: atomic reservation under a row lock; settle, release and lapse; P2-B3 |
| 3 | Approval execution and file deletion | §5.5, §9.3, §9.4: file ID binding; exclusive handle; rename by handle into staging, then Recycle Bin; reparse-point and name rules; durable journal; atomic consumption; P2-A6 to A8 |
| 4 | Signing and trust contract | §7.1: JCS and shared test vectors; three purposes and three keys; signed authorisation fields; pinning at enrolment; rotation |
| 5 | Grants and enrolment | §4.1 (full grant and function table; SECURITY DEFINER rules), §7.2 (atomic single use; insert-only; identity from the connection); P2-S6 |
| 6 | Multi-node delivery state | §8.2: exact states, `received` vs `ack`, offline nodes, retries, locking; P2-E13 |
| C1 | Approval urgency | §8.1: every Phase 2 approval is URGENT, intentionally; the warning moved to 2 minutes before expiry |
| C2 | Core egress | §4.2: allowlisting egress proxy; core has no direct internet route; P2-S7 |
| C3 | Live action expiry | §7.1 and §9.4: signed `valid_until`, enforced by the gateway, the node and core |
| C4 | Retention | §5.7 |
| C5 | Dedicated server | §19 |

**Final review points (Manj, rev 2 → rev 3):**

| # | Point | Resolved in |
|---|---|---|
| R1 | An uncertain Claude call must not be retried as a new call | §10.1 step 6, §10.2; P2-B4 |
| R2 | Protect staging → Recycle Bin against a swap | §9.4 step 4 (own protected folder, fresh subfolder, verify by handle immediately before, check after, fail closed); P2-A10 |
| R3 | Verify the card signature when processing a decision | §5.5 `card_authz`, §9.3 step 6; P2-A9 |
| R1a | Concurrent jobs for the same turn | §10.1 step 6: per-turn row lock (turn, then budget), partial unique index, a table of what each job does, the sweep owns crash cleanup; P2-B5 |
| R1b | What qualifies for RELEASED | §10.1 step 4: connect phase vs send phase; RELEASED only for connect-phase failures; any send-phase outcome short of success is indeterminate; unclassifiable counts as send; §10.2; P2-B4 |

**Revision 5 (22 Nov 2026). Matching plan: revision 4.** Two additions:

| # | Change | Where |
|---|---|---|
| R5-1 | **Offline-node recovery after key rotation** (first drafted with plan revision 3). **A node that missed the key-rotation overlap must re-enrol.** Activation (all acknowledged, or 24 h) marks the non-acknowledging nodes STALE; the gateway closes a STALE node's `hello` with 4409 before any signed message; a node that sees 4409, or an unknown `key_id`, runs nothing and asks to be re-enrolled; `jarvis-core node reenrol` revokes the old node and issues a code with the current fingerprints. A node never accepts a key from the gateway alone. | §7.1 |
| R5-2 | **Validated resume** (with plan revision 4). **Reconnect `resume` is validated before use**: a `last_seq` above the stored maximum, or for an unknown or foreign reply, is ignored and the gateway resumes from the acknowledged cursor, logging `node.resume_out_of_range`. | §6.2 step 6 note |

**Revision 6 (22 Nov 2026). Matching plan: revision 5.** Owner rulings on Task 1's open questions:

| # | Change | Where |
|---|---|---|
| R6-1 | **`challenge` handshake:** a 17th message kind. The gateway sends it first on every connection, signed `gw-transport`, with a fresh single-use 32-byte nonce, `issued_at` / `expires_at` (+30 s), and its key ID and public key. The node verifies it against its pinned gateway key (or, during enrolment, the code's fingerprint), then signs the nonce in `hello` or `enrol`. The gateway checks first message, connection-bound nonce, expiry, signature, revocation and identity; the nonce is consumed on the first reply; any failure closes with 4401. Server time is advisory, for skew detection only. The nil UUID is used as the envelope `node_id` before the node is known. | §6.1, §7.1 table |
| R6-2 | **Key announcements expire:** the validity fields are inside the signed `authz.object` (`valid_until` = `issued_at` + 24 h); the node rejects an expired announcement with 30 s tolerance; shared cases for valid, expired and boundary | §7.1 |
| R6-3 | **Gateway key never accepted as a core signature,** even under the core `key_id`; a shared case | §7.1 |
| R6-4 | **Timestamps:** protocol and signed timestamps are RFC 3339 with an offset; database records stay `timestamptz` and are written in RFC 3339 when they go on the wire | §7.1 |
| R6-5 | **`hello.resume.last_seq` is any integer in the schema,** so the gateway's validation rule (negative → 0; above the maximum → ignored) is what applies, not a schema rejection. This makes the approved Task 7 behaviour reachable. | §7.1 table |
| R6-6 | **Enrolment key pinning and stale-node refusal after the challenge:** after a successful enrolment, `welcome` carries `node_id`, `core_key_id` and `core_public_key`. The node pins the core key only if it matches the code's fingerprint, refuses all core-authorised content until then, and on a mismatch discards its keys and stores nothing. A STALE node gets the pre-authentication challenge, then 4409 instead of `welcome`, and no post-handshake content. | §6.1 step 7, §7.1, §7.2 |
| R6-7 | **Separate `hello` and `enrol` validation paths:** path A (existing node: registered key, not revoked, Tailscale identity matches the row) is unchanged; path B (enrolment: signature by the message's own key, key not already registered, token rules and identity from the connection, through `gw.enrol_node`). Neither path can do the other's job. | §6.1 step 6 |
| R6-8 | **`seq` per sender, per connection:** each side numbers its own messages from 1, increasing by exactly 1, and checks the peer's counter independently; a gap, repeat or decrease closes the connection | §7.1 |

**Revision 7 (22 Nov 2026). Matching plan: revision 7.** Owner rulings on Task 2's NEEDS_CONTEXT report:

| # | Change | Where |
|---|---|---|
| R7-1 | **Audited refusals return, they don't raise.** A function writes the REJECTED audit row and returns a structured refusal with a reason code. The Python wrapper commits its own transaction, then raises. Audited functions are never called inside a transaction that might roll back; the helper requires an idle connection. Tests check the audit row from a separate connection after the exception. Fenced connections raise `JF001`; malformed calls raise `invalid_parameter_value`. | §4.1, §7.2 |
| R7-2 | **Enrolments are created only by the audited core function `gw.create_enrolment(…)`.** No direct inserts; the gateway has no grant on `gw.enrolment` | §5.1, §7.2 step 1 |
| R7-3 | **The gateway's `gw` surface is five functions:** `enrol_node`, `open_connection`, `close_connection`, `touch_connection` (replacing `touch_node`) and `current_node` | §4.1 |
| R7-4 | **`gw.node_connection.last_ping_at`** added | §5.1 |
| R7-5 | **Fenced connections raise SQLSTATE `JF001`** (`connection_fenced`), distinct from permission denial. No audit row of its own; the cause is already audited | §4.1 |
| R7-6 | **Rollback of migration 0008** refuses if any node audit row, node or enrolment exists, and leaves the schema intact. A down-and-up test covers the empty case (plan Task 2) | plan Task 2 |
