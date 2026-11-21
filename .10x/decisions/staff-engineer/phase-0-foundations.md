# Staff Engineer — phase-0-foundations
**Status:** Proposed (2026-11-21)
- Python standards: 3.12, uv + uv.lock, ruff (lint+format), mypy strict on src/, pytest against real Postgres 17. No ORM; SQL lives in repository modules and migrations.
- Migrations: Alembic, raw SQL, every revision has a downgrade; CI runs up/down/up.
- Contracts first: schema + pydantic + examples, tested before feature code; DB enums must equal schema enums (test).
- Public repo: never commit IPs, hostnames, server IDs, secrets; scripts take them as arguments.
- CI: server.yml on server/** and contracts/**; Rust CI and installer ignore those paths.
- ADR numbering is shared across desktop and server; desktop barge-in ADR takes the next free number (010+).
