# ADR-007: Ledger in Postgres, versioned migrations, state machine enforced in the database

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** phase-0-foundations
**Author:** 10x-Team (Architect + Staff Engineer + DBA)

## Context
v2 makes the ledger the system of record: "models interpret and propose; only validated code changes state". Phase 0 must hold the 13 v1 entities (v2 §10.1), give every record a stable ID, timestamps, visibility and source, enforce the Task and Commitment lifecycles, and build a fresh database from migrations alone. Later phases will add agents, workers and connectors that all write to the ledger; any one of them having a bug must not be able to make an illegal state change or skip the audit.

## Decision
1. **One database `jarvis`, three schemas:** `ledger` (entities), `audit` (ADR-008), `ops` (job queue, heartbeats).
2. **Roles** (created once by a superuser bootstrap step, `jarvis_server.bootstrap`, together with the database; everything else is migrations run as the owner):
   - `jarvis_owner` — owns all objects; used only by migrations.
   - `jarvis_app` — used by core, scheduler and (later) workers. `SELECT` and `INSERT` on ledger tables (a new row's state is forced to the initial state, point 7); `UPDATE` on every column **except** `state` (column-level grants); no `DELETE` on stateful tables (end a task with CANCELLED or SUPERSEDED instead); `SELECT` on `audit.event` (rows are written only by the database functions); full use of `ops` tables; `EXECUTE` on `ledger.transition`.
3. **Common columns** on every ledger table: `id uuid default gen_random_uuid()`, `created_at`, `updated_at` (trigger), `visibility` enum (default PRIVATE), `source_ref text not null`.
4. **States as Postgres enums** (`task_state`, `commitment_state`), spelled exactly as in `contracts/`.
5. **Legal moves as data:** table `ledger.allowed_transition(entity_type, from_state, to_state)`, filled by migration from the tables in the spec §6. Changing the lifecycle = a new migration, reviewed like code.
6. **One door for state changes:** `ledger.transition(entity_type, entity_id, to_state, actor_kind, actor_id, reason, correlation_id) returns text`, `SECURITY DEFINER`, `search_path` pinned. It locks the row, checks `allowed_transition`, then either updates the state and writes an APPLIED audit row, or leaves the state alone, writes a REJECTED audit row and returns `REJECTED` (so the refusal commits). Python's `lifecycle.transition()` wraps it and raises `IllegalTransition` on REJECTED.
7. **Creation:** an `AFTER INSERT` trigger on `task` and `commitment` writes the CREATE audit row; a `BEFORE INSERT` check only allows the initial states (DETECTED for tasks, EXPECTED for commitments).
8. **Migrations:** Alembic, raw SQL, every revision has a working downgrade. CI runs upgrade → downgrade → upgrade on an empty database (P0-6). Seed data (one Person, one Organization) is its own revision.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| State machine only in Python | Simpler SQL; easier to unit-test | Any other code path or manual SQL can set any state and skip the audit | Violates "only validated code changes state" the moment a second writer exists |
| Generic state-machine library (e.g. `transitions`) | Ready-made | Still app-only enforcement; another dependency | Same weakness; our tables are small |
| Event sourcing (state derived from the audit log) | Perfect history | Much more complex reads and projections | Overkill for a single-family ledger; the audit table already gives history |
| One `entity` table with JSON body | Flexible | No foreign keys, no typed states, weak queries | Ledger integrity matters more than flexibility |
| Separate database per schema | Stronger isolation | More connections, cross-database joins impossible | Schemas + roles give enough separation |

## Consequences
### Positive
- Illegal transitions are impossible from any client holding `jarvis_app`, not just from well-behaved Python.
- The lifecycle is visible, versioned data that tests are generated from.
- A fresh server is rebuilt from migrations alone.
### Negative
- Logic split between SQL (rules) and Python (callers); developers must read both.
- Column-level grants are less common and need care when adding tables (a test checks `jarvis_app` cannot update any `state` column).
### Risks
- `SECURITY DEFINER` misuse (search-path hijack) → `SET search_path = ledger, audit, pg_temp` on the function; owned by `jarvis_owner`, not superuser.
- Lock contention on hot rows → negligible at family scale; revisit if workers multiply.

## Dependencies
- Depends on ADR-006 (Postgres 17, Alembic, psycopg 3).
- ADR-008 defines the audit table this writes to.
- Phase 3 (Atlas) builds dispatch on top of `transition()` and `ops.job`.
