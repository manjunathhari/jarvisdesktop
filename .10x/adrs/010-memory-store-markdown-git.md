# ADR-010: Memory store — markdown files in a private git repo, audited in Postgres

**Status:** Accepted
**Date:** 2026-11-22
**Feature:** phase-1-memory-engine
**Author:** 10x-Team (Architect + Staff Engineer + Security Engineer)

## Context
Architecture v2 §11 makes memory a markdown + git store behind an interface ("markdown is the first implementation, not the architecture"), with stable IDs, five categories, five classification tags and `retrieve_context()` designed first. Constraints: the code repo is public; the server is shared and temporary; Phase 1 has no AI (the brain router is Phase 2); the audit trail must be append-only (ADR-008); Phase 1 holds no real personal data (Manj, P1).

## Decision
- **Source of truth:** markdown files (spec §4). The engine parses every file into memory, answers reads from there, and reloads when the repo's HEAD moves. No database copy.
- **Repository:** a separate git repo on a Docker volume (`memory`) mounted only into `core`. It has **no remote**; the engine refuses to open a repo that has one.
- **Writes:** one change = one commit, under an exclusive file lock. The engine is the only writer in Phase 1. Strict parsing: any unreadable line stops the load.
- **Tags change only through engine calls** (spec §7). Only a person can confirm; agents and `system` can only write INFERRED/HISTORICAL. `assert_actionable()` is the single guard the Phase 2 policy engine calls.
- **Visibility is checked against a viewer on every read** (spec §5). No automatic parental access to PRIVATE.
- **Retrieval is deterministic** (spec §6): subject + household, tag, visibility, topic/word match with a fixed alias table.
- **Audit:** each change (applied or refused) writes one `audit.event` row through a new SECURITY DEFINER function `audit.record_memory()`. The row holds the memory ID (as a UUID v5 `entity_id` plus the text ID in `details`), the tag change and the git commit. It never holds content. Order: commit → audit; if the audit fails the engine resets its own commit; if the process dies between the two, startup reconciles from `git log`.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Markdown + Postgres search index | Faster search at large size | Two copies that drift; rebuild logic | Not needed below ~50,000 entries |
| Postgres as truth, markdown exported | Transactions, one store | Markdown becomes a view; loses "readable memory you own" | Contradicts v2 §11 |
| Memory folder inside the code repo | One repo | Code repo is public | Unacceptable privacy risk |
| Pure-Python git (dulwich) | No binary in the image | Less familiar, slower, partial porcelain | The `git` CLI is standard and easier to recover by hand |
| Audit only in git history | No migration | Git history can be rewritten by anyone with file access | ADR-008 requires an append-only record |

## Consequences
### Positive
- Memory is readable and diffable by a person; every version is recoverable.
- The public repo can never carry memory; the audit proves what changed and when without exposing what was said.
- The interface is stable for Phase 2 (AI ranking can slot behind `retrieve_context`).

### Negative
- Commit and audit row are not one transaction; covered by undo-on-failure and startup reconciliation.
- Search is a linear scan; fine at this size, revisit with option B.
- Only one writer process; a second writer process would need the same lock on a shared volume.

### Risks
- The memory volume is plaintext on a shared server. Mitigation: no real memories in Phase 1; JARVIS moves to its own server before real memories (Manj, 2026-11-22); encryption at rest remains a later requirement.
- A hand edit can break strict parsing and stop `core`. Mitigation: the error names file and line; the runbook says how to fix or restore from a bundle.

## Dependencies
- ADR-006 (stack), ADR-008 (audit), ADR-009 (shared server, secrets, backups).
- Constrains Phase 2: the gateway must pass the authenticated person as `viewer`/`by` (S3); the policy engine must call `assert_actionable()`.
