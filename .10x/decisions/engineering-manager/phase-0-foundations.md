# Engineering Manager — phase-0-foundations
**Status:** Planned 2026-11-21. Branch `phase-0-foundations`, PR #2.

| Task | Content | Depends on | Who acts |
|---|---|---|---|
| T0.1 | CI path filters on Rust/installer workflows; `server/` skeleton (uv, ruff, mypy, pytest); `server.yml` | — | Claude |
| T0.2 | `contracts/` JSON Schemas + examples; pydantic models; schema↔model tests | T0.1 | Claude |
| T0.3 | Alembic migrations: roles, ledger (13 entities), audit, allowed_transition, `ledger.transition()`, triggers, seed; P0-1…P0-6 | T0.2 | Claude |
| T0.4 | `ops.job` claim/complete, heartbeat; core + scheduler entry points; Dockerfile; compose.yaml | T0.3 | Claude |
| T0.5 | Hardening scripts; Tailscale on server + PC; pubmedgemini deploy via tailnet; SSH key-only; Hostinger firewall; P0-8 | T0.4 | Claude + Manj (Tailscale sign-in, 2FA, approve firewall change) |
| T0.6 | Deploy compose stack; P0-1…P0-5 on server | T0.5 | Claude |
| T0.7 | Local daily backups; restore drill P0-7; rebuild/move runbook | T0.6 | Claude |
| T0.8 | QA + security review; merge | all | Claude, Manj approves merge |

Risks: lock-out during T0.5 (order fixed in ADR-009); local sandbox has Postgres 16 only (CI runs 17).
