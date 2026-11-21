# CTO — phase-0-foundations (Architecture v2 server)
**Status:** Proposed (2026-11-21), spec: `.10x/specs/2026-11-21-phase-0-foundations-design.md`
- Build, not buy: the ledger, audit and state machine are Jarvis's owned continuity (v2 "intelligence is rented, continuity is owned").
- Server: existing Hostinger KVM 2 Mumbai, **shared** with pubmedgemini/Ollama/Traefik (Manj's choice, cost). Isolation trade-off accepted; mitigations in ADR-009.
- Repo: same repo as the desktop node, `server/` folder (Manj's choice); CI path filters keep builds independent.
- Stack: Python 3.12, uv, psycopg 3 + plain SQL, Alembic, pytest, ruff, mypy; FastAPI from Phase 2 (ADR-006).
- Deferred with triggers: vault (Phase 3 OAuth tokens), audit hash chain (second admin), PITR backups (daily reliance).
