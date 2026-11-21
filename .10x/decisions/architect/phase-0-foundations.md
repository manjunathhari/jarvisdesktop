# Architect — phase-0-foundations
**Status:** Proposed (2026-11-21), spec: `.10x/specs/2026-11-21-phase-0-foundations-design.md`, ADR-006…009
- One Postgres database `jarvis`, schemas ledger / audit / ops; roles jarvis_owner (migrations) and jarvis_app (runtime).
- State changes only via SECURITY DEFINER `ledger.transition()`; legal moves stored in `ledger.allowed_transition`; app role cannot UPDATE state columns.
- Audit written inside the database in the same transaction; refused transitions recorded and committed.
- Compose project `jarvis`: postgres, core, scheduler running; gateway, workers under profile phase2. Internal network, no published ports, memory limits.
- Contracts as JSON Schema in `contracts/` (shared with desktop in Phase 2) + pydantic mirrors.
- Failure modes: lock-out during hardening, pubmedgemini deploy break, shared-box compromise, server loss (rebuild from repo; data loss accepted) — see spec §12.
