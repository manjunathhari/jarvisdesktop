# Handoff
**Voice slice 1 (28 Nov):** branch `voice-slice-1` (head `ed6e471`) holds the whole of `.10x/plans/2026-11-27-voice-slice-1.md`: a Python voice helper (Pipecat) that speaks George's replies, with the built-in George as fallback. `main` (`a4276f1`) is merged into it. CI is green on Windows and Linux (run 46115401713). The helper release is tag `voice-helper-v1` (commit `cfa7d12`, published as a pre-release by Manj on 28 Nov), and its SHA-256 is pinned in `crates/jarvis-helper/src/install.rs`. An independent whole-branch review found 0 Critical and 2 Important issues; both are fixed and re-reviewed. **Nothing is merged or installed, and the live check is not done.**
- **Read first:** the design `.10x/specs/2026-11-27-voice-upgrade-design.md` (revision 2), the plan `.10x/plans/2026-11-27-voice-slice-1.md`, `.10x/adrs/015-pipecat-voice-helper.md`, and the review record `.10x/reviews/2026-11-27-voice-slice-1.md` (it lists rulings R1 to R25).
- **Needs Manj's go-ahead, in order:** (1) open the PR and merge; `release.yml` then builds the installer; (2) install it; (3) run the live check, the eight items in the plan's Task 9, Step 6, with fictional phrases only. Item 8's log file is `%LOCALAPPDATA%\in.mhsurgery.jarvis\logs\jarvis.<date>.log`, not `jarvis.log`.
- **The branch `listen-60`** (the 60-second listening window) stays out of `main` and out of this merge (Manj, 28 Nov).
- **Only `voice_engine` and `tts_speed` act** in this slice. The other `voice.toml` settings are read and held for slices 2 and 3.
- **The risk only his PC can settle:** the helper has 60 s to say it is ready. The reviewer estimates 15 to 40 s for a first cold start with antivirus, and perhaps a 10 to 20% chance of passing 60 s the first time. If it does, he sees "Voice helper unavailable. Using my built-in voice.", George still speaks, and the next start tries again. The log line `helper_ready_ms` gives the real time.
- **Known limits carried to slice 2:**
  - the helper may report "finished speaking" 50 to 200 ms before the sound ends, so the ears may catch the tail;
  - nothing notices a helper that is alive but stuck;
  - on first start JARVIS is caption-only during the 440 MB download and up to 60 s of start-up;
  - downloads have no idle timeout;
  - the helper ends with the app only because its input closes (a Windows Job Object would guarantee it);
  - before the next helper tag: `uv sync --locked` and pinned action versions;
  - the Python half of the `LOG_LEVEL` fix needs a new helper build;
  - `first_chunk_max_words` only if the log shows a slow first word.
- **Slices 2 and 3** (the helper listens; speakers and staying awake) each get their own plan after the live check.

**Board slice 1 (27 Nov):** branch `board-slice-1` holds all nine tasks of `.10x/plans/2026-11-26-board-slice-1.md`, each independently reviewed with the fixes applied. It is pushed and passes CI on Windows and Linux through Task 8; nothing is merged or deployed. **What is left is the live check on Manj's PC** (plan Task 9, Step 6): install the desktop build first, then deploy the server (migration 0016), each step with his approval. It includes the Canvas seal checklist in `.10x/reviews/2026-11-27-board-canvas-hostile-page.md`, which is the only proof of the seal on Windows, and the first real test of each window being limited to its own commands (a slip there shows as a button that does nothing). The server now runs `983c2cf`, not `354f1fe` as the next paragraph says. Open decisions for Manj are listed in the project docs `claude/board-task-2-handover.md` to `claude/board-task-9-handover.md`.

**Now (26 Nov):** the server runs `354f1fe` (deployed 26 Nov; migration 0015; smoke 18/18). Phase 2 server and Plan B (the Rust desktop node) are merged, deployed and live-checked on Manj's PC (PR #7 to #9, 25 Nov); his desktop "Manj PC" is enrolled and runs in server mode. Since then, all merged: single instance, the "Hey Jarvis" / "Jarvis" wake word, a reused connection to Claude with Opus for conversation (PR #10 to #14), and the receipt timeout (PR #15). The desktop's own-brain mode stays in the code as a fallback (owner, 26 Nov); Manj has deleted the stored Claude key on his PC.

**Receipt-timeout hardening (PR #16, which also adds ruling R12-38 to the spec):** from the independent review of PR #15: the handler checks its deadline, the transition comes first, a scheduler safety net, and the missing tests. No migration. It needs a server deploy after merge. On that first deploy the safety net also closes any action left ISSUED from earlier testing (task FAILED, "I couldn't confirm", a notice). Project docs: `claude/server-task-receipt-timeout.md` and its handover.

The "Before deploying" list below is historical: it was done for the 24 Nov deploy.

**Read first:**
- spec `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md` (Revision 12; §20 has R12-1 to R12-37);
- plan `.10x/plans/2026-11-22-phase-2-server.md`, and for the desktop node `.10x/plans/2026-11-24-plan-b-desktop-node.md`;
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
  - ~~S-9: shared keyset-target and keyset-role cases~~ (Plan B Task 1).
  - S-10: lax payload validation.
  - S-5 remainder: exception text still logged at `relay.py:640/718` and `core_main.py:69/83/178`.
  - `approvals.py:346` parses `approval_id` before the signature check (fails closed).
- **QA:** the QA review lists its remaining minors.
- **Earlier per-task minors:** listed in each task's review and summarised in the two final reviews.
- **Plan B follow-ups (server, not approved yet):**
  - ~~core never ends an action still ISSUED when no receipt arrives~~ (done: `action.receipt_timeout`, PR #15; hardened in PR #16, ruling R12-38);
  - clears are not replayed on reconnect;
  - REFUSED live receipts are recorded as `action.readonly_ok`.
- **Plan B owner decisions:** not acking approved-action items; withholding `received` when nothing is sent; idle desktop and null `in_reply_to`; ears/voice timestamps for the full latency measure; the D-3 key-store deviation.
- **Receipt-timeout review, left open (minor):** if core dies while handling a receipt, the timeout can win and a completed delete is reported "couldn't confirm"; a late receipt's result is not kept in the `reused_key` audit row. From the review of PR #16: an action whose task cannot be failed stays ISSUED and is retried by the sweep about once in 30 days (one REJECTED audit row each time); the sweep query has no supporting index (about 1 ms at 300k events; add a partial index on ISSUED actions with a later migration); nothing tests the sweep's job through the handler end to end.
- **Deferred by Manj to the dedicated-server move (26 Nov):** `jarvis-core person add` and its deploy-guide step, to be done before the move.
- **Carried from Phase 1:** run the P0-7 restore drill before the move; turn deploy smoke off once real data arrives (Q3).

**Constraints:**
- No SSH from the build workspace.
- The connector has no shell (deploy and logs only).
- Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
- Live testing uses scripted, fictional phrases only (§5.7).
