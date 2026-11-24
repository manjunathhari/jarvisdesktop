# ADR-014: Atlas foundation — an interface inside core, no workers yet

**Status:** Proposed
**Date:** 2026-11-22
**Feature:** phase-2-core-and-nodes
**Author:** 10x-Team (Architect + Engineering Manager)

## Context
Atlas (v2 §5) turns Jarvis's approved plans into tickets, dispatches specialists and tracks dependencies. Specialists, the verifier and Gmail/Calendar arrive in Phase 3. Phase 2's approval test still needs a ticket to hang the approval on (`approval.task_id`).

## Decision
- Atlas exists in Phase 2 as an interface inside core: `accept_plan(plan, *, by) -> list[TaskId]`.
- The Phase 2 implementation turns one approved plan into one ledger task and moves it through `ledger.transition()`, which writes the existing audit rows.
- **No `workers` process in Phase 2**; it stays a compose placeholder. The only action executor is the desktop node (`pc.list_test_folder`, `pc.delete_test_file`).
- Dispatch, dependencies, retries and specialists arrive in Phase 3 behind the same interface.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| No Atlas until Phase 3; core creates tasks directly | Less code now | Phase 3 must re-route every task-creating path | The interface is cheap; retrofitting is not |
| Build the workers process now | Phase 3 starts further ahead | No specialists to run; untested infrastructure | Out of Phase 2 scope |

## Consequences
### Positive
- Approvals, tasks and audit already follow the final shape; Phase 3 adds behaviour behind the interface.
### Negative
- The Phase 2 implementation is deliberately thin.
### Risks
- The interface may need to grow when dispatch arrives; it is internal, so changing it is cheap.
