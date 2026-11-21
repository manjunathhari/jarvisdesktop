# SDE — phase-0-foundations
## Built (branch phase-0-foundations, 2026-11-21) — CI green on Postgres 17
- T0.1 CI path filters; `server/` (uv, ruff, mypy strict, pytest); `server.yml` (tests, image build, compose check).
- T0.2 `contracts/` JSON Schemas + examples; `jarvis_server.contracts` pydantic mirrors; memory Protocol.
- T0.3 `bootstrap.py` (roles + DB, superuser); Alembic 0001–0006 (schemas/types, 13 entities, audit, lifecycle, ops, seed); `ledger.py`.
- T0.4 `jobs.py` (queue + heartbeats), `core_main.py` (bootstrap/migrate/serve/smoke), `scheduler_main.py`, `smoke.py`, Dockerfile, `deploy/compose.yaml`.
- Tests: 70 (P0-1…P0-6, every transition rule vs spec, enum parity, privilege tests, job race/retry/lease, smoke).
## Deviations from design
- Roles + database are created by `bootstrap` (superuser), not a migration (roles are cluster-wide; owner can't create roles).
- Audit: app role is SELECT-only (design said INSERT+SELECT); rows written only by SECURITY DEFINER `audit.append()`; spec/ADRs updated.
- Creation audit needs the caller to declare the actor via `set_config('jarvis.actor_*')` (ledger.py does it); missing actor = refused.
- Secret files on the host must be `root:10001 0440` (container user uid/gid 10001 reads them).
## Next / tech debt
- No SSH from the build workspace and the Hostinger API can't run host commands → T0.5 hardening is a script Manj pastes into the Hostinger web console.
- For deploys through the Hostinger Docker API, replace `docker compose run admin …` with a one-shot `init` service (bootstrap + migrate) that core waits on.
- Image publishing to GHCR (pinned digest) still to add for T0.6.
