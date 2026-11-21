# Handoff
**Now:** Architecture v2 Phase 0 is complete apart from Manj merging PR #2. JARVIS server runs on the shared Hostinger server (project `jarvis`).
- Tests: 72 green (CI, Postgres 17); P0-1…P0-6 and P0-8 pass; P0-7 deferred by Manj.
- Reviews: `.10x/reviews/2026-11-21-phase-0-qa-report.md`, `.10x/reviews/2026-11-21-phase-0-security-review.md`.
- Runbook (deploy, rebuild, move, restore): `server/deploy/README.md`.

**Follow-ups carried into later phases**
- S3: the Phase 2 gateway must derive the acting person/node from authentication, never from client input.
- S4: pin image digests before moving to JARVIS's own server. S5: revoke CONNECT on database `postgres` from PUBLIC.
- Q3: turn off deploy smoke (`JARVIS_SMOKE_ON_INIT=0`) or clean `smoke-check` rows once real data arrives.
- P0-7 restore drill when the build grows or before the move.

**Next phase:** Phase 1 — memory engine (markdown + git, `retrieve_context()`, IDs, classifications; tests M1–M4). Start with brainstorming + design doc per 10x protocol.

**Constraints:** no SSH from the build workspace; connector has no shell (deploy and logs only). Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
