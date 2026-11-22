# Handoff
**Now:** Phase 0 complete and live. Phase 1 (memory engine) built and reviewed 2026-11-22 on branch `phase-1-memory-engine`: 236 tests green, M1–M4 pass, QA + security reviews in `reviews/2026-11-22-phase-1-*` with no blocking issues. PR #3 open, CI green, live check passed (live server runs branch commit 6e49572). Next: Manj merges; then Phase 2.

**Read first:** spec (§15 lists build changes) `.10x/specs/2026-11-21-phase-1-memory-engine-design.md`, ADR-010, `decisions/*/phase-1-memory-engine.md`.

**Phase 1 boundaries (Manj):** engine only; fictional people in tests and smoke; no embeddings, AI inference, consolidation, external memory services or real personal data. Dedicated server before any real memory.

**Follow-ups carried forward**
- S3: the Phase 2 gateway must derive the acting person/node (memory `viewer`/`by`) from authentication, never from client input.
- After merge: remove `phase-1-memory-engine` from the image-publish condition in `.github/workflows/server.yml`.
- Phase 2: add a call to change visibility/sharing after a write; consider an index on memory audit `details->>'commit'` if reconciliation slows.
- S4: pin image digests before moving to JARVIS's own server. S5: closed in migration 0007 (Phase 1).
- Q3: turn off deploy smoke (`JARVIS_SMOKE_ON_INIT=0`) or clean `smoke-check` rows once real data arrives.
- P0-7 restore drill when the build grows or before the move.
- Phase 2 policy engine must call `memory.assert_actionable()` before any action relying on memory.

**Constraints:** no SSH from the build workspace; connector has no shell (deploy and logs only). Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
