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

- A panel's `content` is limited to **30,000 bytes of UTF-8**, with no control character except tab, line feed and carriage return. JSON Schema counts characters, so the schemas cap `content` at 30,000 characters and core's models count bytes as well; the node must do the same (plan Task 5). `content_sha256` must be the hash of the content's UTF-8 bytes; the schemas check only its form.
- Why 30,000: a frame is limited to 65,536 bytes (the gateway enforces this on frames from a node; core must not issue a larger one). The gateway serialises without escaping non-ASCII, and with control characters refused, content at most doubles on the wire (`"`, `\`, line breaks). 60,000 bytes of content, a doubled title and about 1,300 bytes of envelope, authorisation and signatures stay under the limit.
- A `title` is one line of 1 to 120 **Unicode code points** (not bytes, not UTF-16 units), with no control character, and not blank. It is put on one line of Jarvis's prompt, so it must not be able to break that line.
- `revision` and `cost_paise` are at most 2,147,483,647. `cost_paise` is what the panel has cost so far, all revisions together.
- Panel kinds are `text`, `canvas` and `drawing`; panel states are `open`, `minimised`, `maximised` and `closed`. A closed panel is kept.
- `board.update.args` has the shape of `board.show.args` (the whole panel at its new revision), so a node that has lost the panel can restore it.
- `board.show.receipt` is the receipt of `board.show` and `board.update`; `board.set_state.receipt` is the receipt of `board.set_state` and `board.focus`. Both travel as the live form of `action.receipt`. `RENDERED` means the Board drew the panel at that revision. In a `REFUSED` or `ERROR` receipt, `panel_id`, `revision` and `content_sha256` are null when the args gave the node no usable value; `RENDERED` and `OK` always carry them. `check` is omitted when there is none, never null.
- `board.working` (args `{"state": "started"}`, receipt `board.working.receipt`) tells the desktop that a voice turn has queued Board work, so the Board can say that Jarvis is working. Nothing is stored and nothing depends on its receipt. There is no "ended": the desktop ends the line itself when a panel is shown, when Jarvis speaks unasked, or after three minutes (R12-42). The line does not track one request: nothing on the wire ties a panel or a spoken line to the request that caused it, so any panel shown and anything Jarvis says unasked (a delete result, for example) ends it, and with two requests under way the first to finish ends it for both. Tying it to the request needs the job's key in `board.working`, `board.show` and the headline; left for later.
- A `drawing` panel's `content` is a description, never the picture: canonical JSON (sorted keys, no spaces, not ASCII-escaped) with `engine`, `engine_version`, `units` (`mm`), `items`, `steps` (at most 40 one-line labels of at most 80 characters) and `picture` (`sha256`, `bytes`, `media_type` `image/svg+xml`). Core and the node refuse a drawing whose content is not exactly that canonical text (`parse_drawing_content`); the schemas cannot see inside `content`, and a `text` or `canvas` panel is checked as before.
- The picture itself (an SVG of at most 2,000,000 bytes) travels in `board.content` actions ahead of the `board.show`: the gzip of the whole SVG is cut into at most 64 chunks of at most 40,000 bytes, each base64 (standard, padded, at most 53,336 characters) with `sha256` and `bytes` of the whole uncompressed picture, `index` (0 to 63) and `count`. `index` must be below `count` (core and the node check that; the schema cannot). A worst-case chunk is about 53,500 bytes in a frame of 65,536, leaving over 3,500 for the envelope and signatures. `board.content.receipt` is `STORED` (naming the picture's `sha256` and the chunk's `index`) or `REFUSED`; a repeated chunk is stored again, harmlessly. A missing chunk is the node's `content_missing` refusal of the panel, never a half picture.
- `board.state` is a message kind (node to core). Core applies these in the order they arrive: the last one wins. The others are not kinds: they are validated by core and the node, not by the gateway.
