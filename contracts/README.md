# Contracts

Language-neutral JSON Schemas (draft 2020-12) shared by the JARVIS server (Python) and the desktop node (Rust, from Phase 2).
They are the source of truth for record shapes and state names.

| Schema | Status |
|---|---|
| `task`, `commitment`, `audit-event` | Used from Phase 0 |
| `memory-entry` | Used from Phase 1 (memory engine) |
| `receipt`, `approval`, `node-envelope` | Stubs; used from Phase 2/3 |

`examples/` holds one valid and one invalid example per schema. The server's tests check every example against both the schema and the Python models, and check that the database enums match the schema enums. Change a schema only together with its models, migration and examples.
