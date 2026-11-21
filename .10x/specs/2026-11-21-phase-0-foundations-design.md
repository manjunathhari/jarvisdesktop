# JARVIS server — Phase 0 (Foundations) design

**Status:** Proposed — awaiting Manj's review
**Date:** 2026-11-21
**Feature slug:** `phase-0-foundations`
**Inputs:** Architecture v2 (project doc `Jarvis_Architecture_v2.md`), decisions D1–D8 (`jarvis-architecture.md`), `phase-0-handover.md`
**Decision records:** ADR-006 (server stack), ADR-007 (ledger, migrations, state machine), ADR-008 (audit trail), ADR-009 (shared VPS: network, secrets, backups)

## 1. Goal

Lay the foundations every later phase builds on: a locked-down server, a private network, the Postgres ledger with the v1 entities, an append-only audit trail and a state machine that only allows legal changes.

**Done when** a test ticket moves DETECTED → … → COMPLETED with one audit row per step, and acceptance tests P0-1 … P0-8 (§9) pass.

Not in Phase 0: the gateway, any AI call, the memory engine (Phase 1), connectors (Phase 3), the phone app.

## 2. Kick-off decisions (settled 21 Nov 2026)

| # | Question | Decision |
|---|---|---|
| K1 | Server | The existing Hostinger **KVM 2, Mumbai** (2 vCPU, 8 GB, 100 GB), Ubuntu 24.04. **Shared** with pubmedgemini, Ollama, Traefik and a standalone Postgres 16. gemma-lab removed. |
| K2 | Repository | **Same repo** as the desktop node (`jarvisdesktop`), server code under `server/`. |
| K3 | Database | A **dedicated Postgres 17 container** for JARVIS; the existing Postgres 16 is not touched. |
| K4 | Python tools | Python 3.12 + uv; psycopg 3 with plain SQL; Alembic; pytest against real Postgres; ruff + mypy; FastAPI from Phase 2. |

Consequences of sharing the server are handled in ADR-009: ports 80/443 stay public for pubmedgemini, JARVIS and SSH become Tailscale-only, pubmedgemini's deploy moves onto Tailscale.

## 3. Repository layout

```
jarvisdesktop/
├── app/, crates/, ...            # desktop node (unchanged)
├── contracts/                    # language-neutral JSON Schemas shared by server and desktop
│   ├── task.schema.json
│   ├── commitment.schema.json
│   ├── audit-event.schema.json
│   ├── receipt.schema.json       # stub (Phase 2/3)
│   ├── approval.schema.json      # stub (Phase 2/3)
│   ├── node-envelope.schema.json # stub (Phase 2)
│   └── examples/                 # one valid + one invalid example per schema
└── server/
    ├── pyproject.toml, uv.lock
    ├── src/jarvis_server/
    │   ├── contracts/            # pydantic models mirroring contracts/*.schema.json
    │   ├── db.py                 # connection pool, transaction helper
    │   ├── ledger/               # entity repositories (plain SQL)
    │   ├── lifecycle.py          # transition tables + transition() (ADR-007)
    │   ├── audit.py              # audit writer (ADR-008)
    │   ├── jobs.py               # job queue claim/complete
    │   ├── memory.py             # Protocol only: retrieve_context, write, update, confirm, archive
    │   ├── core_main.py          # core process entry point
    │   └── scheduler_main.py     # scheduler process entry point (heartbeat)
    ├── migrations/               # Alembic, raw SQL in each revision
    ├── tests/
    └── deploy/
        ├── compose.yaml
        ├── harden/               # idempotent shell scripts, run once per server
        ├── backup/               # backup + restore scripts, systemd units
        └── README.md             # runbook
```

CI: a new `server.yml` workflow runs only when `server/**` or `contracts/**` change. The existing Rust CI and Windows installer workflows get `paths-ignore: [server/**, contracts/**, .10x/**]` so a server commit never builds or publishes a desktop release.

## 4. Interface contracts (written and tested before feature code)

Each contract is a JSON Schema in `contracts/` plus a pydantic model. A test checks that every example in `contracts/examples/` validates (or fails) against both, so the two can never drift. In Phase 2 the desktop generates Rust types from the same schemas.

| Contract | Phase 0 content |
|---|---|
| Task | id, title, definition_of_done, owner_person_id, state, visibility, source_ref, due_at, created_at, updated_at |
| Commitment | id, person_id, description, due_at, state, event_id?, visibility, source_ref, timestamps |
| Audit event | see §7 |
| Receipt (stub) | id, task_id, action_id, kind, evidence (object), verified_at?, verified_by? |
| Approval (stub) | id, task_id, requested_from_person_id, tier, expires_at, decision (pending/approved/denied/expired), decided_at? |
| Node envelope (stub) | version, message_id, node_id, session_id?, sent_at, kind, payload |
| Memory interface | Python `Protocol` with the v2 §11.5 signatures; no implementation |

States are enums in the schemas, so the desktop and server agree on spelling.

## 5. Ledger schema

One Postgres database `jarvis`, schema `ledger`. All 13 v1 entities from v2 §10.1 get a table now, kept minimal; later phases add columns through migrations.

**Every table has:** `id uuid primary key default gen_random_uuid()`, `created_at`, `updated_at` (trigger-maintained), `visibility` (enum: PRIVATE, SELECTED_PERSON, SELECTED_GROUP, FAMILY_SHARED, ORGANIZATION_SHARED; default PRIVATE), `source_ref text not null` (where the record came from: `manual`, `msg-…`, `test`, …).

| Table | Key columns beyond the common ones |
|---|---|
| organization | name, kind |
| person | display_name, organization_id? |
| relationship | from_person_id, to_person_id, kind (spouse, child, …) |
| information | subject_person_id?, kind, content jsonb |
| event | title, starts_at, ends_at?, location? |
| commitment | person_id, description, due_at?, event_id?, **state** |
| responsibility | owner_person_id, description, recurrence (text, e.g. RRULE) |
| task | title, definition_of_done, owner_person_id?, responsibility_id?, due_at?, **state** |
| dependency | from_task_id, to_task_id, kind (BLOCKS, RELATES) |
| action | task_id, actor_kind, actor_id, idempotency_key unique, attempted_at, status |
| outcome | action_id, verified (bool), evidence jsonb, verified_at? |
| permission | person_id, scope, tier, granted_by_person_id, expires_at? |
| context | subject_person_id?, kind, summary, valid_from, **expires_at not null** |

- `state` columns are Postgres enums matching the contracts.
- A view `ledger.current_context` returns rows where `valid_from <= now() < expires_at` (P0-5). Context is never copied into memory.
- Seed migration: one Person (Manj) and one Organization (household). D6: the family model stays in the schema; nobody else is seeded.
- Job queue and heartbeats live in schema `ops` (§8). Audit lives in schema `audit` (§7).

## 6. State machine

Only `lifecycle.transition(conn, entity, id, to_state, actor, reason)` changes a `state` column, and it does so by calling one database function, `ledger.transition(...)` (ADR-007). The legal moves live in a table, `ledger.allowed_transition`, filled by migration from the tables below. The application role has no `UPDATE` privilege on any `state` column; the function runs as `SECURITY DEFINER`, so even a bug elsewhere in the code cannot skip the rules or the audit row.

### 6.1 Task transitions

| From | Allowed to |
|---|---|
| DETECTED | INTERPRETED, CANCELLED, SUPERSEDED |
| INTERPRETED | PROPOSED, DEFERRED, CANCELLED, SUPERSEDED |
| PROPOSED | APPROVAL_PENDING, READY *(policy allows skipping approval for read-only/reversible tasks)*, DEFERRED, CANCELLED, SUPERSEDED |
| APPROVAL_PENDING | READY *(approved)*, CANCELLED *(denied)*, EXPIRED *(approval expired)*, SUPERSEDED |
| READY | IN_PROGRESS, BLOCKED, DEFERRED, CANCELLED, SUPERSEDED |
| IN_PROGRESS | COMPLETED, BLOCKED, FAILED, CANCELLED |
| BLOCKED | READY, CANCELLED, SUPERSEDED |
| DEFERRED | PROPOSED, CANCELLED, SUPERSEDED |
| COMPLETED, CANCELLED, FAILED, EXPIRED, SUPERSEDED | — *(terminal)* |

A retry is a new Action on the same IN_PROGRESS task, not a state change. Re-doing failed work is a new task that supersedes the old one.

### 6.2 Commitment transitions

| From | Allowed to |
|---|---|
| EXPECTED | AT_RISK, SECURED, FULFILLED, MISSED |
| AT_RISK | SECURED, EXPECTED *(threat resolved)*, FULFILLED, MISSED |
| SECURED | AT_RISK *(new threat)*, FULFILLED, MISSED |
| FULFILLED, MISSED | — *(terminal)* |

### 6.3 Behaviour

- Legal: row locked (`SELECT … FOR UPDATE`), state updated, audit row written — one transaction; the function returns `APPLIED`.
- Illegal: state unchanged; the function writes an audit row with `outcome = REJECTED` and returns `REJECTED` instead of raising, so the transaction commits and the refusal is recorded (P0-2). Python then raises `IllegalTransition` to the caller.
- Unknown entity or id: error, audited as REJECTED with reason `not_found`.
- Creating a record writes an audit row too (`from_state = null`).

## 7. Audit trail

Table `audit.event`, append-only (ADR-008).

| Column | Meaning |
|---|---|
| seq bigint identity | Strict order of writing |
| id uuid | Stable reference |
| occurred_at timestamptz | Server time |
| actor_kind | PERSON, AGENT, SYSTEM |
| actor_id text | e.g. person uuid, `atlas`, `test-runner` |
| entity_type, entity_id | What was touched |
| action | CREATE, TRANSITION |
| from_state, to_state | Null where not applicable |
| outcome | APPLIED or REJECTED |
| reason text | Why (required) |
| correlation_id uuid? | Groups the rows of one request or ticket walk |
| details jsonb | Anything else |

Protection, three layers:
1. **Privileges:** the app role `jarvis_app` has only `INSERT` and `SELECT` on `audit.event`; `UPDATE`, `DELETE`, `TRUNCATE` revoked (P0-3).
2. **Trigger:** a `BEFORE UPDATE OR DELETE` trigger raises an error for every role, including the owner, so mistakes in migrations or manual sessions are blocked too.
3. **Backups:** off-site encrypted copies (ADR-009) mean even the server's root cannot quietly rewrite history without the older copies disagreeing.

Tamper-evident hash chaining is deliberately left out (YAGNI); trigger to add it: more than one admin, or audit used as evidence for someone other than Manj.

## 8. Job queue and scheduler heartbeat (minimal)

- `ops.job`: id, kind, payload jsonb, run_at, status (QUEUED, RUNNING, DONE, FAILED), attempts, max_attempts (default 2 — v2's "retry once"), locked_by, locked_until, last_error.
- Claim: `SELECT … WHERE status='QUEUED' AND run_at <= now() ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1`. A job whose `locked_until` passed is reclaimable. Two workers never get the same job (tested).
- `ops.heartbeat`: process, last_seen. The scheduler writes every 30 s. A query returns processes not seen for 3 intervals (v2 §13.3 "3 missed heartbeats").
- The core process in Phase 0 runs one job kind, `noop`, proving the claim loop works end to end.

## 9. Acceptance tests

| # | Test | Pass when | Where it runs |
|---|---|---|---|
| P0-1 | Test ticket walk | DETECTED → INTERPRETED → PROPOSED → APPROVAL_PENDING → READY → IN_PROGRESS → COMPLETED; 7 audit rows (create + 6 transitions), in `seq` order, all APPLIED | CI + server |
| P0-2 | Illegal transition | DETECTED → COMPLETED refused; state still DETECTED; one REJECTED audit row | CI + server |
| P0-3 | Audit append-only | As `jarvis_app`: UPDATE, DELETE, TRUNCATE on `audit.event` fail with permission errors; as owner: UPDATE/DELETE blocked by trigger | CI + server |
| P0-4 | Commitment lifecycle | EXPECTED → AT_RISK → SECURED → FULFILLED with 4 audit rows | CI + server |
| P0-5 | Context expiry | Expired row absent from `current_context`; live row present | CI + server |
| P0-6 | Migrations | Empty database → `alembic upgrade head` → `downgrade base` → `upgrade head` succeeds; schema matches | CI |
| P0-7 | Backup restore | Latest off-site backup restored into a scratch Postgres container; P0-1's ticket and its 7 audit rows present and identical | Server (manual drill, scripted) |
| P0-8 | Network | From outside the tailnet: only TCP ports 80 and 443 answer (plus Tailscale's UDP port); SSH (22) and anything JARVIS refused. From Manj's PC over Tailscale: SSH works | Server, run from the PC and from an outside host |

Plus unit tests for every allowed and every forbidden transition (generated from the tables in §6), the job claim race, and contract examples.

## 10. Deployment on the shared server

- Location on the server: `/opt/jarvis/` (compose file, `.env` without secrets), secrets in `/etc/jarvis/secrets/` (ADR-009).
- Compose project `jarvis`, separate from the others. Services running in Phase 0: `postgres`, `core`, `scheduler`. `gateway` and `workers` are defined under the `phase2` profile and do not start.
- Networks: `jarvis_internal` (internal: no internet route) joins postgres, core and scheduler. **No JARVIS port is published on the host.** Admin access to the database is `docker exec` over SSH (itself Tailscale-only).
- Memory limits: postgres 1 GB, core 512 MB, scheduler 256 MB, so Ollama and pubmedgemini keep their headroom.
- Images: built on Manj's PC or in CI and pulled from GitHub Container Registry, pinned by digest (same pattern pubmedgemini already uses).
- Migrations run as a one-off `core migrate` command with the owner role before a new version starts; the running app uses `jarvis_app`.

## 11. Order of work

1. This spec + ADR-006…009 → Manj reviews.
2. Branch `phase-0-foundations`: CI path filters; `server/` skeleton; `contracts/` with tests (contracts first).
3. Migrations: roles, ledger, audit, ops; lifecycle function; tests P0-1…P0-6 green in CI.
4. Server hardening (ADR-009): Tailscale on the server and the PC; pubmedgemini deploy moved onto Tailscale and tested; SSH keys only; Hostinger firewall allowing only 80/443; P0-8.
5. Deploy compose stack; run P0-1…P0-5 on the server.
6. Backups to off-site storage; restore drill P0-7.
7. QA and security review; merge to main.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Closing SSH to the internet locks Manj out | Tailscale verified working from the PC *before* the firewall change; Hostinger web console is the break-glass route |
| pubmedgemini deploys break | Its GitHub workflow joins the tailnet with a tagged, ephemeral key; tested before port 22 is closed |
| Docker-published ports bypass the Ubuntu firewall | JARVIS publishes none; the Hostinger firewall sits outside the VM and Docker cannot override it |
| Another project on the box exhausts memory | Memory limits on JARVIS containers; heartbeat check flags a starved scheduler |
| Public repo exposes infrastructure details | No IPs, hostnames, server IDs or secrets committed; scripts take them as arguments |
| Backup password lost → backups useless | Restic password and storage keys also stored in Manj's password manager; restore drill proves it |
