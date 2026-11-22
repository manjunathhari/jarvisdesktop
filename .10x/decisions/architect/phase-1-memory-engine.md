# Architect — phase-1-memory-engine
See spec `.10x/specs/2026-11-21-phase-1-memory-engine-design.md` and ADR-010.
- Components: `memory/format.py` (strict parse/serialise), `memory/store.py` (git repo, lock, commit, history, no-remote), `memory/engine.py` (rules, visibility, retrieval, audit hook), `memory/text.py` (word matching + aliases), `memory/types.py`.
- Source of truth: markdown; in-memory index reloaded on HEAD change.
- Boundaries: memory never holds tasks/context; people/relationships as records stay in the ledger; memory links a subject slug to `ledger.person.id`.
- Failure modes: unparseable file → refuse to load; audit failure → reset own commit; crash between commit and audit → startup reconcile; remote configured → refuse to open.
