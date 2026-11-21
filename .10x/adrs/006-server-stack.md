# ADR-006: Server stack — Python 3.12, uv, psycopg 3 with plain SQL, Alembic, pytest

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** phase-0-foundations
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
D1 settled Python for the server (core, workers, connectors) and Rust for the desktop node. Phase 0 needs a database access layer, a migration tool, a test runner and code checks; Phase 2 will need a web framework for the gateway. The server lives in the desktop repo under `server/` (kick-off decision K2), so its tooling must not interfere with the Rust/Tauri build. One builder maintains everything, so fewer, well-known tools beat clever ones. The core rules of the system (state machine, audit) must be easy to read and verify.

## Decision
| Job | Choice |
|---|---|
| Runtime | Python 3.12 |
| Packages and virtualenv | **uv**, with a committed `uv.lock` |
| Database driver | **psycopg 3** (sync API in Phase 0; async available later), connection pool via `psycopg_pool` |
| Queries | **Plain SQL** in repository modules; SQLAlchemy Core only if a query builder genuinely helps later. No ORM. |
| Migrations | **Alembic**, each revision written as raw SQL (`op.execute`), with a real `downgrade` |
| Contracts | JSON Schema in `contracts/` + **pydantic v2** models; tests keep the two in step |
| Tests | **pytest** against a real Postgres 17 (GitHub Actions service container in CI; a throwaway container locally) |
| Code checks | **ruff** (lint + format) and **mypy** (strict on `src/`) |
| Web framework | **FastAPI** — chosen now, first used in Phase 2 for the gateway |
| Database | **PostgreSQL 17** (matches the version pubmedgemini already runs on the same server) |

CI: new workflow `.github/workflows/server.yml`, triggered only by changes under `server/**` or `contracts/**`. The existing Rust CI and Windows installer workflows ignore those paths.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| SQLAlchemy ORM + Alembic autogenerate | Less SQL to write; popular | State changes and privileges hidden behind the ORM; autogenerate misses functions, triggers, grants | The core rules are database functions, triggers and grants — they need explicit SQL anyway |
| asyncpg | Fastest Postgres driver | Async-only; no sync path for simple scripts and tests | Phase 0 has no concurrency need; psycopg 3 offers both |
| Poetry / pip-tools | Familiar | Slower; separate tools for Python version + venv | uv does all of it in one fast tool |
| yoyo / sqitch / dbmate for migrations | Plain-SQL focused | Less common in Python; sqitch needs Perl | Alembic is standard and supports raw SQL revisions with downgrades |
| testcontainers-python | Starts Postgres from inside tests | Extra dependency; Docker-in-CI quirks | CI service container is simpler; locally one `docker run` |
| Rust for the server too | One language across repo | Contradicts D1; slower to build connectors and agents | D1 is settled |

## Consequences
### Positive
- Every rule that protects data (legal transitions, append-only audit) is visible as SQL in the migrations and testable directly.
- Server and desktop builds stay independent despite sharing a repo.
- Tests run against the same database engine and version as production.
### Negative
- More hand-written SQL than an ORM would need.
- Two languages and two toolchains in one repo; contributors need both only if they touch both.
### Risks
- Schema drift between hand-written SQL and pydantic models → contract tests plus a test that the database enum values equal the schema enum values.
- Postgres 17 vs future 18 features (e.g. native uuidv7) → `gen_random_uuid()` now; revisit on upgrade.

## Dependencies
- Depends on D1 (Python server) and K2 (same repo).
- Constrains ADR-007 (migrations in raw SQL), ADR-008 (audit enforced in the database), and Phase 2 (FastAPI gateway).
