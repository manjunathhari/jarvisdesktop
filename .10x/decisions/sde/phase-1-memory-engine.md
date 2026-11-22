# SDE — phase-1-memory-engine (what was built)

Branch `phase-1-memory-engine`. Package `server/src/jarvis_server/memory/`:

| Module | Does |
|---|---|
| `format.py` | Layout table, strict parser (errors name file + line), canonical writer, `index.md` |
| `text.py` | Word matching: stop-words, plural folding, fixed alias table (every alias tested) |
| `store.py` | Git CLI wrapper: no system/global config, hooks off, fcntl lock, atomic file writes, history, bundle, no-remote check, discard uncommitted on open |
| `engine.py` | `MemoryEngine`: write/update/confirm/archive/add_person, get/read/search/retrieve_context/history/assert_actionable; visibility; audit hook with undo; startup reconciliation |
| `pg.py` | `PgDirectory` (ledger.person), `PgAudit` (`audit.record_memory`), UUID v3 entity IDs |
| `checks.py` | Fictional family + M1–M4 as runnable checks (tests and deploy smoke) |
| `service.py` | Open the engine in core; daily git bundle (keep 7) |

Also: `contracts/memory-entry.schema.json` (+ examples), audit-event schema gains `memory` and UPDATE/CONFIRM/ARCHIVE; migration 0007; S5 in bootstrap; Dockerfile installs git; compose volumes `memory` + `memory_backups` (core only); smoke gains memory lines; runbook §7.

## Deviations from the spec
| Spec | Built | Why |
|---|---|---|
| §9 S5 in migration 0007 | In `bootstrap()` | REVOKE on database `postgres` needs superuser; migrations run as owner. Bootstrap runs on every deploy |
| §9 bundle into `backups` volume at `/backups/memory` | Separate volume `memory_backups` | Core never sees the database dumps; no compose `subpath` dependency |
| §8 `MemoryEntry.version` and `recorded_at` | No `version`; `recorded_on` (date) | Per-entry commit needs blame on every load; `history()` gives versions. The file stores a date |
| §8 `history(memory_id)` | `history(memory_id, *, viewer)` | Must not reveal hidden entries |
| §9 UUID v5 entity id | UUID v3 (MD5) | Computable in plain SQL without the uuid-ossp extension; parity tested |
| — | `get(memory_id, *, viewer)`, `people()` | Needed by checks and Phase 2 |
| §7 update/archive "allowed by" | Person must be subject or recorder (any household member for `household` entries); agents may update only INFERRED/HISTORICAL; agents cannot archive | Spec named only confirm's rule; same rule applied |
| — | Entry metadata carries `domain:` | Every entry carries everything needed to place it |

## Tests
217 total (Phase 0: 72). New: format 50, engine 66, acceptance (Postgres) 18, service 4; contracts 35 → 42.

## Tech debt
- `search`/`retrieve_context` scan linearly (fine to ~50k entries; ADR-010 option B later).
- Image not built in the build session (registry blocked); CI builds and publishes it.
