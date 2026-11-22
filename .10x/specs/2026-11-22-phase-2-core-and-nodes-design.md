# JARVIS server — Phase 2 (Jarvis core and nodes) design

**Status:** Draft for Manj's review (kick-off questions and builder-check questions answered 22 Nov)
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

### 4.1 Database roles

A new role, **`jarvis_gateway`**, gets only what the gateway needs:

| Table | Grant |
|---|---|
| `gw.node` | SELECT; UPDATE of `last_seen_at` only; new rows only through `gw.enrol_node()` (§7.2) |
| `gw.node_connection` | INSERT, UPDATE |
| `conv.inbound` | INSERT |
| `conv.stream_chunk` | SELECT |
| `comm.outbox`, `comm.delivery` | SELECT; INSERT and UPDATE on `comm.delivery`; UPDATE of `comm.outbox.state` only through `comm.mark_delivering()` / `comm.acknowledge()` (SECURITY DEFINER) |
| `ops.job` | INSERT only, through `ops.enqueue_inbound()` (SECURITY DEFINER, fixed job kinds) |

No grants on `ledger.*`, `audit.*`, memory or `ops.brain_usage`. Its password is a new secret file, `pg_gateway_password`.

### 4.2 Network

- **The gateway joins the tailnet through its own Tailscale sidecar (K2-4).** A `tailscale` container, `gateway-ts`, is a separate tailnet device named `jarvis-gw` with ACL tag `tag:jarvis-gateway`. The gateway shares its network namespace (`network_mode: service:gateway-ts`), so it listens only on that device's tailnet address. **No service publishes a host port**, so the firewall and P0-8 are unchanged.
- Peer identity comes from the sidecar's local API (`whois`) on a socket shared only between the sidecar and the gateway. The host's Tailscale socket is never mounted.
- Manj's Tailscale ACL decides which of his devices may reach `tag:jarvis-gateway`, and on which port. That's a second check, alongside the node keys.
- The sidecar keeps its state in a volume, `gateway_ts_state`, so it keeps its identity across redeploys.
- **Core needs a way out to reach Claude.** Today every service is on an internal network with no internet route. Core joins a second network, `jarvis_egress`, that can reach the internet. Nothing else joins it. Limiting egress to `api.anthropic.com` alone needs a proxy, which is listed as a builder's call (§17).
- The gateway stays on the internal network plus its published port. It has no outbound internet.

### 4.3 Secrets (files under `/etc/jarvis/secrets`, as now)

| New secret | Used by |
|---|---|
| `tailscale_gateway_authkey` | gateway-ts only (a tagged, reusable-off auth key Manj creates once in the Tailscale admin) |
| `pg_gateway_password` | gateway, init |
| `gateway_signing_key` | gateway only (transport messages, §7.1) |
| `anthropic_api_key` | core only |
| `server_signing_key` (Ed25519 private key) | **core only**. Core signs every `node.action` and every approval-related outbox item when it writes them, so the gateway only relays signatures it cannot make. A compromised gateway can't forge an approval. |

## 5. Data model (new migrations, from 0008)

Three new schemas keep the boundaries visible: `gw` (nodes), `conv` (conversations), `comm` (events, outbox, delivery). Approvals go in `ledger`, next to tasks.

### 5.1 Nodes (`gw`)

| Table | Key columns |
|---|---|
| `gw.node` | `id`, `person_id` → `ledger.person`, `kind` (DESKTOP, PHONE), `name`, `public_key` (Ed25519), `tailscale_identity`, `registered_at`, `revoked_at`, `last_seen_at` |
| `gw.node_connection` | `id`, `node_id`, `opened_at`, `closed_at`, `close_reason` |

A revoked node (`revoked_at` set) is refused at the handshake, and its open connection is closed within one sweep (P2-E10).

### 5.2 Conversations (`conv`)

| Table | Key columns |
|---|---|
| `conv.session` | `id`, `person_id`, `started_at`, `last_activity_at`, `closed_at` |
| `conv.inbound` | `id` = the node's `message_id` (unique, so a resend is a no-op), `node_id`, `person_id` (filled from the node row, never from the message), `session_id`, `kind`, `payload`, `received_at` |
| `conv.message` | `id`, `session_id`, `seq`, `role` (PERSON, JARVIS, SYSTEM), `person_id`, `node_id` (null for JARVIS), `text`, `inbound_id`, `created_at` |
| `conv.stream_chunk` | `reply_id`, `seq`, `type` (SPEECH, ACTION), `text` or `action` (jsonb), `sig` (for ACTION), `final` (bool), `target_node_id` (for ACTION), `created_at`. Primary key `(reply_id, seq)` |
| `conv.session_ticket` | `session_id`, `task_id` (v2 §8.4: sessions link to the tickets they create) |

**Session rule (my call):** a person has at most one open session. A message after 30 minutes of inactivity starts a new one. Any node of that person joins the open session, which is how a conversation resumes on a different node (P2-E09).

### 5.3 Events, outbox and delivery (`comm`)

| Table | Key columns | Lifecycle |
|---|---|---|
| `comm.event` | `id`, `kind`, `subject_type`, `subject_id`, `person_id`, `details` (codes and IDs, no free text), `occurred_at` | **Append-only.** The app roles have INSERT only, and a trigger refuses UPDATE and DELETE, as in `audit.event`. |
| `comm.outbox` | `id` (stable; P2-E04), `person_id`, `event_id`, `priority` (URGENT, TODAY, DIGEST, SILENT), `kind`, `body`, `group_key`, `supersedes_id`, `expires_at`, `sig` (core's signature, for approval items), `state`, `created_at` | `PENDING → DELIVERING → DELIVERED → ACKNOWLEDGED`, or the terminal states `SUPERSEDED` and `EXPIRED`. |
| `comm.delivery` | `outbox_id`, `node_id`, `attempt`, `sent_at`, `acked_at` | One row per item per node per attempt. |

**Rules:**
- Moving an outbox item to ACKNOWLEDGED on any node ends delivery on all nodes (P2-E05).
- SUPERSEDED and EXPIRED items stay readable in history (P2-E06).
- State moves only through SQL functions, each of which writes an audit row (ADR-008 style).
- **No content in `comm.event` or `audit.event`.** Content lives in `comm.outbox.body` and `conv.message`, which are subject to retention (D10).

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

Jobs use the existing `claim` with `FOR UPDATE SKIP LOCKED`. Every job handler must be idempotent: re-running a job after a crash changes nothing the second time.

### 5.5 Approvals (`ledger.approval`)

The columns follow `contracts/approval.schema.json`, extended as below: `id`, `task_id`, `requested_from_person_id`, `tier`, `action_kind`, `action_args`, `summary`, `expires_at`, `decision` (PENDING, APPROVED, DENIED, EXPIRED), `decided_at`, `decided_by_node_id`, `execution_key` (unique; §9.4).

A trigger allows `decision` to change only from PENDING, and only before `expires_at` for APPROVED and DENIED (P2-E11). The check is in the database, so no code path can skip it.

### 5.6 Cost meter (`ops.brain_usage`)

`id`, `job_id`, `model`, `input_tokens`, `output_tokens`, `cost_inr` (computed from a price table in config), `at`. Core only.

## 6. Conversation lifecycle

### 6.1 A node connects

1. The WebSocket opens on `jarvis-gw`'s tailnet address. The gateway reads the peer's Tailscale identity from the sidecar's local API (`whois`). An unknown peer is closed.
2. The node sends `hello`: `node_id`, plus a signature over a server-sent nonce. The gateway checks the signature against `gw.node.public_key`, that the node is not revoked, and that the Tailscale identity matches the node row. Any failure closes the connection and writes an audit row.
3. The gateway opens a `gw.node_connection` row and sends `welcome`: the open session, if any, plus the reconnect summary (§8.3).

### 6.2 A conversation turn (reactive)

1. The node sends `utterance`: what was heard, plus a short screen summary, which is what the desktop's `BrainRequest` carries today.
2. The gateway writes `conv.inbound` (a duplicate `message_id` is a no-op) and enqueues `conv.turn`. Then it NOTIFYs `jarvis_jobs`.
3. Core claims the job and works out the person from `conv.inbound.person_id`, which came from the node row. It finds or opens the session and appends the PERSON message.
4. Core builds the brain request from:
   - the last 20 messages of the session;
   - `memory.retrieve_context(person, utterance, viewer=person)`;
   - the tool menu: the desktop modules (news, live TV, screen actions) plus `pc.delete_test_file`. `pc.list_test_folder` is called by core itself (§9.3), not offered to Claude.
5. Core calls the router (§10). As speech arrives, core writes `conv.stream_chunk` rows (`seq` 1, 2, …) and NOTIFYs `jarvis_stream` with only the `reply_id`.
6. The gateway, woken by NOTIFY, reads the new chunks from its last `seq` and sends them to every connected node in the session. **NOTIFY is only a wake-up; the chunks are the durable stream.** If either process restarts mid-reply, the gateway resumes from the last `seq` it sent (P2-E03).
7. When the reply is complete, core writes the final chunk (`final = true`) and the JARVIS `conv.message`.
8. **Tool calls go to policy (§9), never straight to a node.** A READ_ONLY module call, such as "show the news", is part of the live reply: core writes it as a `conv.stream_chunk` of type `action` (signed by core, §7.1), and the gateway sends it as `node.action` to the node that spoke. It is valid only while that reply is live (2 minutes); a node that reconnects later never replays it. It does **not** go in the outbox. An IRREVERSIBLE call becomes an approval, which does go in the outbox (§9.3), and the action it leads to is sent only after approval.
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

### 7.1 Envelope

Every message uses `contracts/node-envelope.schema.json`. Phase 2 makes it final:
- **Added fields:** `seq` (per-connection counter, strictly increasing) and `sig` (Ed25519 over the canonical JSON of every other field).
- **Gateway checks:** the gateway checks `sig` and `seq` on every message from a node. A bad signature or a replayed `seq` closes the connection.
- **Server messages:** core signs `node.action` and approval-related `outbox.item` payloads with `server_signing_key` when it writes them; the signature is stored with the row (`comm.outbox.sig`, `conv.stream_chunk.sig` for actions) and the gateway passes it through unchanged. The gateway signs only transport messages (`welcome`, `pong`) with its own key, `gateway_signing_key`. Nodes hold both public keys and refuse an action or approval that is not signed by core. A forged "approved, go ahead" can't reach the desktop, even from a compromised gateway.

| Kind | Direction | Payload |
|---|---|---|
| `hello` / `welcome` | node → / ← server | handshake (§6.1) |
| `utterance` | → | `text`, `screen_summary` |
| `reply.chunk` | ← | `reply_id`, `seq`, `text`, `final` |
| `outbox.item` | ← | `outbox_id`, `priority`, `kind`, `body`, `expires_at` |
| `ack` | → | `outbox_id` or `reply_id` |
| `approval.decision` | → | `approval_id`, `decision` |
| `node.action` | ← | `action_kind`, `args`, `approval_id` (if any), `execution_key` |
| `action.receipt` | → | `execution_key`, `result`, `evidence` |
| `ping` / `pong` | both | heartbeat, every 30 s; 3 missed = offline (v2 §13.3) |

Payload schemas go in `contracts/` with examples and contract tests, before feature code (v2 §15).

### 7.2 Registering a node (my call)

1. Manj runs `jarvis-core node enrol --person manj --kind desktop --name "study PC"` on the server over Tailscale SSH (`docker compose exec core …`). It writes a row in **`gw.enrolment`** (`id`, `token_hash` (SHA-256), `person_id`, `kind`, `name`, `expires_at` = now + 15 min, `used_at`) and prints the token once. Nothing in the repo or the build workspace sees it.
2. The desktop generates its key pair, stores the private key with Windows DPAPI, and connects with `enrol` (token + public key) instead of `hello`.
3. The gateway calls **`gw.enrol_node(token, public_key, tailscale_identity)`**, a SECURITY DEFINER function. In one transaction it checks the token hash, expiry and `used_at`; sets `used_at`; inserts the `gw.node` row with the Tailscale identity the gateway read from `whois` for this connection; and writes an audit row. The gateway has EXECUTE on that function only: no direct grant on `gw.enrolment` and no INSERT on `gw.node`. No job is involved, so enrolment works with core down.
4. The connection then continues as a normal `hello`.

Revoking a node is `jarvis-core node revoke <node>`.

## 8. Notifications, delivery and reconnection

### 8.1 Notification rules (deterministic, in code, with tests)

| Event | Priority | Outbox behaviour |
|---|---|---|
| Approval requested | TODAY, or URGENT if it expires within an hour | Expires with the approval; superseded when decided on another node |
| Approval 10 minutes from expiry | URGENT | Superseded by the decision or the expiry |
| Approval expired | TODAY | "Expired, not done" |
| Queued turn completed after Claude came back | TODAY | The reply itself, in its session |
| Claude down | DIGEST (TODAY if a turn is held) | One item, updated in place through `supersedes_id` |
| Claude back | DIGEST | Supersedes "down" |
| Cost cap at 80% / 100% | TODAY / URGENT | |
| Held turn expired (cap) | TODAY | In its session; supersedes the "held" item |
| Node action succeeded (READ_ONLY) | SILENT | History only |

### 8.2 Delivery

- On connect, and whenever new rows appear (NOTIFY `jarvis_outbox`), the gateway sends the person's PENDING and DELIVERED-but-unacknowledged items to each connected node, using the same `outbox_id` each time.
- The node shows each `outbox_id` once and acknowledges it.
- A sweep every 60 s re-sends items that were delivered but not acknowledged within 2 minutes.

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
2. **Before an approval is created**, core sends `pc.list_test_folder` to the requesting desktop as a live action and waits for its receipt (timeout 10 s). If the named file isn't there, Jarvis says so and nothing is proposed. Otherwise the file's SHA-256 and size are stored in the approval's `action_args`, so the person approves deleting *that exact file*.
3. Policy finds IRREVERSIBLE. Core creates a ledger task, through the Atlas interface (§11), and a `ledger.approval`: expiry 10 minutes (my call), summary "Delete test file `x.txt`?".
4. Core writes the outbox item, and Jarvis says "That needs your approval."
5. The desktop shows the approval card. Manj approves by tap or by voice ("approve"); a voice approval is sent as `approval.decision` from that authenticated node.
6. Core sets the decision through the guarded trigger and then sends `node.action` (signed by core, with the stored hash) with the `execution_key`. The desktop deletes only if the file's current hash matches the approved hash.

### 9.4 Execution, receipt and verification

- **The desktop's guard:** the desktop runs `pc.delete_test_file` only if the `node.action` carries the server's signature, an `approval_id` and a new `execution_key`. It keeps a local list of keys it has already used and refuses a duplicate.
- **The action:** it moves the named file from the configured test folder (only that folder; the name may not contain path separators) to the Recycle Bin.
- **The receipt:** the desktop returns `action.receipt` with evidence: the file is absent from the folder, plus its SHA-256 before deletion.
- **Verification at source:** core checks that the receipt's before-hash equals the hash stored in the approval, then sends a second `pc.list_test_folder` and checks the file is absent. Only then does it close the task with a verified `ledger.outcome`. If either check fails, the task is FAILED and Jarvis says so (v2 §13.3: never marked done).
- **Phase 2 limit:** a node reporting on itself is the most Phase 2 can do. The independent verifier is Phase 3.

**Negative cases (P2-A2 to P2-A5):**
- no approval → refused;
- expired approval → refused, approval EXPIRED;
- revoked node → can't connect, so it can't execute;
- duplicate `node.action` → the desktop refuses the reused key, and the server never issues a second key for one approval.

## 10. Brain router (Claude only, D4, D8)

```python
class Brain(Protocol):
    def think(self, request: BrainRequest) -> Iterator[BrainEvent]: ...   # Speech | ToolCall
```

- **Provider:** Claude through the Anthropic API, streaming, with tool use. The model name is in config. Prompts and outputs are provider-neutral types, so a second provider can sit behind the same interface later.
- **Cost meter:** every call writes `ops.brain_usage`.
  - Month-to-date spend ≥ 80% of Rs 5,000 → event and TODAY notification.
  - ≥ 100% → the router refuses new calls, the turn is held, and an URGENT notification goes out. Manj can raise the cap only by changing config and redeploying.
  - **Cap holds expire (K2-5):** a turn held by the monthly cap or the daily ceiling waits at most 1 hour. If the budget still doesn't allow it, the turn is marked expired and Jarvis sends a TODAY notification in that session: "I couldn't answer that: the monthly limit is reached" (or "today's limit"). It never runs later. If the cap is raised within the hour, the turn runs once.
  - **Daily ceiling:** a turn is refused if today's spend would pass `remaining ÷ days left in the month`, so one runaway day can't use the month.
- **Failure handling:** a Claude timeout or 5xx marks the brain DOWN after 2 consecutive failures.
  - Held `conv.turn` jobs are re-queued with a delay, not failed.
  - Jarvis tells the person (CLAUDE_DOWN, §8.1).
  - `brain.probe` runs every 2 minutes. On success, CLAUDE_BACK is emitted and held turns run once, in order.
  - Reminders, expiries, delivery and verification never call Claude, so they continue (P2-B1, the provider fallback test).
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
- **Gateway client:** a WebSocket client with reconnect (backoff up to 30 s), signing, `seq`, ping/pong and acknowledgements. Outbox items are shown once per `outbox_id`.
- **Approval card panel:** summary, time left, approve and deny. It shows the server's state after a decision.
- **`pc.delete_test_file`:** limited to one configured test folder, with the used-key list and the receipt.
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

Flow tests:

| Test | Proves |
|---|---|
| P2-A1 | Voice request → approval card on the desktop → approve → test file deleted → receipt verified → spoken result; full audit trail |
| P2-A2 … A5 | Delete refused with no approval, with an expired approval, from a revoked node, and on a duplicate execution request |
| P2-B1 | Claude down: turn held, person told, reminders, expiries and delivery continue; Claude back: held turn runs exactly once |
| P2-B2 | Cost cap: 80% warns; 100% refuses new calls; the daily ceiling holds; a cap-held turn expires after 1 hour with a notification and never runs later |
| P2-S1 | Gateway role can't read memory, call `ledger.transition()` or write `audit.event` |
| P2-S2 | Person and viewer come from the node row even when the message claims another person |
| P2-S3 | A bad signature, a replayed `seq` or an unknown Tailscale peer is refused |

Tests use fake nodes (a Python WebSocket client), and P2-B tests use a fake brain. One live check on the desktop runs P2-A1 by hand. Every test that can run in CI does (Postgres 17, as now). init's smoke check gains rows for the E- and S-tests that need no node.

## 15. Testing, observability and deployment

- **Timing logs:** gateway receive → job claim → first Claude token → first chunk sent, so latency can be read per hop.
- **Compose:** a new `gateway-ts` sidecar (official `tailscale/tailscale` image, pinned by digest, `TS_AUTHKEY` from the secret file, state volume). `gateway` gets a real command, the `pg_gateway_password` and `gateway_signing_key` secrets, `network_mode: service:gateway-ts`, and a 256 MB limit. It still reaches Postgres on the internal network: `gateway-ts` joins `jarvis_internal` so the shared namespace sees Postgres. Core joins `jarvis_egress` and gets `anthropic_api_key` and `server_signing_key`.
- **Smoke:** init prints the new SMOKE lines. The runbook (`server/deploy/README.md`) gains node enrolment, revocation and the gateway checks.
- **P0-8:** the external port scan must still show only 22/80/443.

## 16. Approaches considered (gateway and core split)

| Approach | Trade-off | Decision |
|---|---|---|
| **A. A separate gateway process, with Postgres in between** | Durability and the security boundary are structural: the gateway holds no Claude key or memory, and a restart of either side loses nothing. Matches v2 §14. Cost: streaming passes through Postgres (chunks plus a NOTIFY wake-up), and there is one more container. | **Chosen (Manj, 22 Nov)** |
| B. The gateway as a module inside core | Simplest, with direct streaming. But the internet-facing code shares a process with the Claude key and memory, a gateway crash takes the brain down, and the split would be needed later under load. | Rejected |
| C. A separate gateway calling core over an internal HTTP API | Two network APIs to secure. Durability depends on core remembering to write rows. No gain over A. | Rejected |

## 17. Left to the builder

- Library choices inside the stack: the WebSocket library on the Rust side; the Ed25519 libraries (`cryptography` / `ed25519-dalek` suggested); the canonical JSON method for signing.
- Restricting core's egress to the Anthropic API (a small proxy or firewall rules), if it is simple. Otherwise document the risk.
- Exact message windows and prompt layout, the model name, and the price table for the cost meter.
- Order of work inside each step, and how to split it into PRs.

## 18. Order of work (for the plan)

1. Contracts (envelope v1 final, payload schemas, examples, tests) and ADR-011 to 014.
2. Migrations 0008+ (`gw`, `conv`, `comm`, approval, brain usage, gateway role and grants) with tests, including P2-S1.
3. Gateway: handshake, enrolment, inbound, streaming, delivery and acks (fake node tests).
4. Core: job handlers, the conversation turn with a fake brain, events and notification rules, reconnect summary.
5. Brain router: Claude, cost meter, down and back.
6. Policy, approvals and the Atlas interface; memory integration and `set_visibility`.
7. Desktop: `ServerBrain`, gateway client, approval card, test-file action, enrolment.
8. Deploy on the shared server; live P2-A1; reviews (QA, security).
9. **Track D11 (separate workstream, my call):** the move to the dedicated server gets its own short design (provisioning and hardening, encryption at rest, off-server backups, the P0-7 restore drill, S4 digest pinning, the move runbook). It can run alongside steps 3–8. Phase 2 is declared complete only when both the build and this track are done.
