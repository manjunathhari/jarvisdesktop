# Contracts

Language-neutral JSON Schemas (draft 2020-12) shared by the JARVIS server (Python) and the desktop node (Rust, from Phase 2).
They are the source of truth for record shapes and state names.

| Schema | Status |
|---|---|
| `task`, `commitment`, `audit-event` | Used from Phase 0 |
| `memory-entry` | Used from Phase 1 (memory engine) |
| `node-envelope`, `approval`, `messages/*` (one payload schema per message kind, plus `authz`) | Final from Phase 2 (spec §7.1, §5.5) |
| `receipt` | Stub; used from Phase 3 |

`examples/` holds one valid and one invalid example per schema (`examples/messages/` for the message payloads). The server's tests check every example against both the schema and the Python models, and check that the database enums match the schema enums. Change a schema only together with its models, migration and examples.

`signing-vectors/` holds the golden JCS + Ed25519 signing vectors and the node authorisation cases, made by an independent generator; see its README.
