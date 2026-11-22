# Handoff
**Now:** Phase 0 and Phase 1 complete. Phase 1 (memory engine) merged 2026-11-22 (PR #3, main `f373775`): 236 tests green, M1–M4 pass, reviews in `reviews/2026-11-22-phase-1-*` with no blocking issues. Close-out PR #4 limits image publishing to `main`; then redeploy `main`. Next: Phase 2 (Jarvis core + nodes) — brainstorm, design doc and ADRs (next free: 011) before any code.

**Read first:** spec (§15 lists build changes) `.10x/specs/2026-11-21-phase-1-memory-engine-design.md`, ADR-010, `decisions/*/phase-1-memory-engine.md`.

**Phase 1 boundaries (Manj):** engine only; fictional people in tests and smoke; no embeddings, AI inference, consolidation, external memory services or real personal data. Dedicated server before any real memory.

**Follow-ups carried forward**
- S3: the Phase 2 gateway must derive the acting person/node (memory `viewer`/`by`) from authentication, never from client input.
- Phase 2: add a call to change visibility/sharing after a write; consider an index on memory audit `details->>'commit'` if reconciliation slows.
- S4: pin image digests before moving to JARVIS's own server. S5: closed in migration 0007 (Phase 1).
- Q3: turn off deploy smoke (`JARVIS_SMOKE_ON_INIT=0`) or clean `smoke-check` rows once real data arrives.
- P0-7 restore drill when the build grows or before the move.
- Phase 2 policy engine must call `memory.assert_actionable()` before any action relying on memory.

**Constraints:** no SSH from the build workspace; connector has no shell (deploy and logs only). Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
