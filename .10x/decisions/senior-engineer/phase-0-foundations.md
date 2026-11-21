# Senior Engineer — phase-0-foundations
- Contracts first: JSON Schema (draft 2020-12) is the source; pydantic models hand-written; tests validate every example against both and compare enum sets with the DB enums.
- Migrations: Alembic revisions call `op.execute()` with SQL kept inline; revision 0001 roles+schemas, 0002 ledger, 0003 audit + lifecycle, 0004 ops, 0005 seed. Roles are cluster-wide: created if missing, never dropped by downgrade (downgrade drops objects only).
- `ledger.transition()` in PL/pgSQL, SECURITY DEFINER, `SET search_path = pg_catalog, ledger, audit, pg_temp`; dynamic SQL limited to whitelisted entity tables via `format('%I')`.
- Tests: session fixture creates a fresh database per test run, migrates with owner role, connects tests as `jarvis_app`; owner/superuser connections only for privilege tests.
- Tricky parts: column-level UPDATE grants (grant UPDATE on explicit column list); TRUNCATE trigger is statement-level; REJECTED path must not raise.
