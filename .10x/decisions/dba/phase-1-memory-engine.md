# DBA — phase-1-memory-engine
- Migration 0007: `audit.action` + UPDATE, CONFIRM, ARCHIVE (appended; DB/contract parity test); `audit.event` CHECK allows `memory`; `audit.memory_entity_id(text)` (UUID v3, IMMUTABLE); `audit.record_memory(...)` SECURITY DEFINER, `search_path = pg_catalog, pg_temp`, validates ref/action/tags/commit, caps reason at 500, calls `audit.append`. EXECUTE granted to `jarvis_app` only.
- Downgrade refuses once memory audit rows exist (append-only); otherwise rebuilds the enum.
- No new tables or indexes: memory rows use the existing `(entity_type, entity_id, seq)` index. Reconciliation reads `details->>'commit'` for memory rows (small; add an expression index if it grows).
- S5: `REVOKE CONNECT ON DATABASE postgres FROM PUBLIC` in bootstrap.
