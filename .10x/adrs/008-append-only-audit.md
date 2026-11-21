# ADR-008: Append-only audit trail enforced by privileges and a trigger

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** phase-0-foundations
**Author:** 10x-Team (Architect + Security Engineer + DBA)

## Context
v2 requires an append-only audit of every state change ("who/what, from-state, to-state, when, why"), including refused attempts. Phase 0's acceptance tests require that the application's database role cannot update or delete audit rows (P0-3) and that every transition produces exactly one row, in order (P0-1). The audit will later be how Manj checks what Jarvis, Atlas and the specialists actually did.

## Decision
- **Table `audit.event`** with columns: `seq bigint generated always as identity` (order of writing), `id uuid`, `occurred_at`, `actor_kind` (PERSON/AGENT/SYSTEM), `actor_id`, `entity_type`, `entity_id`, `action` (CREATE/TRANSITION), `from_state`, `to_state`, `outcome` (APPLIED/REJECTED), `reason not null`, `correlation_id`, `details jsonb`. Indexed on `(entity_type, entity_id, seq)` and `occurred_at`.
- **Written only inside the database** — by `ledger.transition()` and the creation triggers (ADR-007) — in the same transaction as the change it records. Python never inserts audit rows for state changes itself.
- **Layer 1, privileges:** `jarvis_app` gets `SELECT` only — it can neither change nor forge audit rows; no ownership. The internal writer `audit.append()` is not executable by the app.
- **Layer 2, trigger:** `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers raise an exception for every role, including `jarvis_owner`. A future retention job that must remove old rows will be a reviewed migration that archives to cold storage first (v2 §13.2), not an ad-hoc delete.
- **Layer 3, backups:** daily copies (ADR-009) keep older versions of the history; off-site copies are added when JARVIS moves to its own server.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Audit written by Python after each change | Simple | Can be forgotten, or fail after the change commits | Must be atomic with the change |
| Privileges only (no trigger) | Fewer moving parts | Owner role or a careless migration can still edit history | Trigger costs nothing and covers mistakes |
| Hash chain (each row hashes the previous) | Tamper-evident even against root | More code; needs a verifier; ordering subtleties | One admin, family scope: trigger + off-site backups suffice. Add when there is a second admin or audit is evidence for others |
| Separate audit database or log service | Stronger isolation | Breaks single-transaction atomicity; more infrastructure | Atomicity matters more at this scale |
| Postgres extension `pgaudit` | Logs all SQL | Statement logs, not business events; noisy | We need domain events (from/to state, reason) |

## Consequences
### Positive
- Every applied or refused change is recorded atomically, with a strict order.
- P0-3 is enforced by the database, not by convention.
### Negative
- Correcting a wrong audit row is impossible by design; corrections are new rows referencing the old one.
- The table only grows; tiering to warm/cold storage is needed later (v2 §13.2, Phase 5).
### Risks
- Superuser (`postgres`) can still disable triggers → superuser credentials stay in the secrets folder, never used by any service; older backups expose silent edits.
- Audit volume → trivial at family scale; `seq` index keeps reads fast.

## Dependencies
- Depends on ADR-007 (roles, `transition()`), ADR-009 (backups).
- Constrains every future writer: state changes must go through `transition()` to be audited.
