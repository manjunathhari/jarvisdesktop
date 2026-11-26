# Contracts

Language-neutral JSON Schemas (draft 2020-12) shared by the JARVIS server (Python) and the desktop node (Rust, from Phase 2).
They are the source of truth for record shapes and state names.

| Schema | Status |
|---|---|
| `task`, `commitment`, `audit-event` | Used from Phase 0 |
| `memory-entry` | Used from Phase 1 (memory engine) |
| `node-envelope`, `approval`, `messages/*` (one payload schema per message kind, plus `authz`) | Final from Phase 2 (spec §7.1, §5.5) |
| `messages/pc.*` (the test-folder actions' `authz.object.args`, and their `action.receipt` payloads) | Final from Phase 2 (spec §9.3, §9.4, R12-25) |
| `messages/board.*` (the Board's live actions' `authz.object.args`, their `action.receipt` payloads, and the `board.state` message from the node) | Final from the Board, slice 1 (plan `.10x/plans/2026-11-26-board-slice-1.md`) |
| `receipt` | Stub; used from Phase 3 |

`examples/` holds one valid and one invalid example per schema (`examples/messages/` for the message payloads). The server's tests check every example against both the schema and the Python models, and check that the database enums match the schema enums. Change a schema only together with its models, migration and examples.

`signing-vectors/` holds the golden JCS + Ed25519 signing vectors and the node authorisation cases, made by an independent generator; see its README.

## The Board (`messages/board.*`)

- A panel's `content` is limited to **32,000 bytes of UTF-8** and its `title` to 120 characters. JSON Schema counts characters, so the schemas cap `content` at 32,000 characters and core's models and the node also count bytes. `content_sha256` must be the hash of the content's UTF-8 bytes; the schemas check only its form.
- Panel kinds are `text` and `canvas`; panel states are `open`, `minimised`, `maximised` and `closed`. A closed panel is kept.
- `board.show.receipt` is the receipt of `board.show` and `board.update`; `board.set_state.receipt` is the receipt of `board.set_state` and `board.focus`. Both travel as the live form of `action.receipt`. `RENDERED` means the Board drew the panel at that revision.
- `board.state` is a message kind (node to core). The others are not kinds: they are validated by core and the node, not by the gateway.
