# Handoff
**Now:** Phase 2 server (Jarvis core + nodes) is built and reviewed on `phase-2-server`, with **PR #5 to `main` open and not merged**. Nothing is deployed. 1 202 tests pass with `-W error`, and ruff, mypy and the compose config are clean. The final QA and security reviews (`reviews/2026-11-24-phase-2-qa.md`, `reviews/2026-11-24-phase-2-security.md`) found nothing blocking after one fix wave. The test map is in `reviews/2026-11-24-phase-2-test-map.md`.

**Read first:**
- spec `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md` (Revision 12; §20 has R12-1 to R12-34);
- plan `.10x/plans/2026-11-22-phase-2-server.md`;
- the runbook `server/deploy/README.md`;
- `decisions/{qa,security}/phase-2-core-and-nodes.md`.

**Before deploying (Manj approves the deploy separately):**
- Check that `10.214.14.0/24` is free on the server.
- Check that Compose is 2.23.1 or later, which inline `configs.content` needs.
- For the first deploy of this version, run `docker compose down` once (never `-v`).
- `init` records the trusted keys, so `keys init` is automatic.
- Run the Step 6 live checks with `server/tests/fakes/live_node.py`, using fictional text only:
  - SMOKE PASS lines;
  - the real gateway path;
  - first-chunk latency against 3 s;
  - P2-S5: no gateway egress, including 127.0.0.11:53, egress-proxy:3128 and new tailnet connections, plus the new `--socket-exists ! --uid-owner 0` rule;
  - P2-S7: core reaches only Anthropic;
  - P0-8: the external scan shows only ports 22, 80 and 443.
- If a pinned image fails because it is read-only or has dropped capabilities, relax only that container.

**Open owner decisions:** if latency is tight, use a shared pooled `httpx.Client` for the brain (changes the Task 8/10 one-brain-per-job ruling).

**Follow-ups carried forward (deferred, low):**
- **Security:**
  - S-3: a node marked STALE keeps its open connection.
  - S-4: an authenticated node can grow the audit trail.
  - S-6: the gateway reads historical outbox bodies; move to the dedicated server.
  - S-7: STATED memory written by Claude; fix before real memory.
  - S-8: read-only tool arguments aren't schema-validated.
  - S-9: shared keyset-target and keyset-role cases; needed before Plan B.
  - S-10: lax payload validation.
  - S-5 remainder: exception text still logged at `relay.py:640/718` and `core_main.py:69/83/178`.
  - `approvals.py:346` parses `approval_id` before the signature check (fails closed).
- **QA:** the QA review lists its remaining minors.
- **Earlier per-task minors:** listed in each task's review and summarised in the two final reviews.
- **Plan B (Rust node) must follow:**
  - R12-25: the desktop wire schemas in `contracts/messages/`;
  - R12-26: ack the keyset `outbox_id` after it verifies;
  - R12-29: close codes (reconnect except 4409);
  - R12-6: envelope `key_id`;
  - R12-34: send fully signed envelopes, which core re-verifies.
- **Carried from Phase 1:** run the P0-7 restore drill before the move; turn deploy smoke off once real data arrives (Q3).

**Constraints:**
- No SSH from the build workspace.
- The connector has no shell (deploy and logs only).
- Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
- Live testing uses scripted, fictional phrases only (§5.7).
