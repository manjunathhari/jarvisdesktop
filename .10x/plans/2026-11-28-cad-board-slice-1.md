# CAD board, slice 1 (a drawing on the Board) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

*Status: written 28 Nov 2026 after Manj's "U can build" (00:10 IST). Not yet read by him. Nothing is merged or deployed.*

**Goal:** Manj asks by voice for a drawing; Jarvis draws it with the CAD engine in its own container, and it appears on the Board as a Drawing panel he can zoom, move and open as the CAD view.

**Architecture:** A drawing is a recipe (ordered steps of engine commands) kept by the server. A new job, `cad.make`, runs a loop of at most 4 Claude calls; after each step the server asks a small runner in a locked-down `cad` container to replay the whole recipe and shows Claude the results and a picture. The finished picture (SVG) is too large for a panel, so it travels to the desktop in signed chunks before a `board.show` whose content only describes and pins it; the desktop checks the hash, keeps the file, and serves it to the Board from its loopback server as an image.

**Tech Stack:** Python 3.12 server (psycopg, alembic, httpx, pytest); Rust workspace (new crate `jarvis-cad-runner`; `jarvis-node`; Tauri 2 app); TypeScript with Vite and vitest; CADCraft 0.12.0 `cadcraft-cli`, pinned by SHA-256. One new Rust dependency in `jarvis-node`: `flate2` (already in `Cargo.lock`).

**Spec:** project doc `claude/cad-board-design.md` (approved 27 Nov 2026). Test evidence: `claude/cad-board-slice-0-handover.md`, `claude/cad-board-spike-part1-handover.md`. The Board this extends: `.10x/plans/2026-11-26-board-slice-1.md`. Mock Board screens 4 and 5: https://claude.ai/artifact/6yEYPSHRapNZhJKULGYiAG. Slice 0's working loop, to port: branch `spike-cad-engine`, `spikes/cad-engine/draw_test.py`.

## Decisions for Manj (where the code or the test forced a choice the design left open)

| # | The design said | What was found | Planned |
|---|---|---|---|
| C-1 | The desktop asks for the picture by its hash and gets it in chunks | The link has no request-and-answer message; adding one means a new inbound kind, a migration of `conv.record_inbound` and a job | **The server sends the picture first, unasked**, as signed `board.content` chunks in the same reply as `board.show`. A desktop that lacks the picture refuses the panel and Jarvis says so. Asking again later belongs with the resync work already deferred |
| C-2 | Chunks that each fit a frame | The engine writes text as strokes, so SVGs are large (48 KB for the tiny test plan) but compress well (19 KB) | **Chunks carry the gzip of the SVG**, 40,000 bytes each, at most 64 |
| C-3 | Reserve about Rs 75 per drawing | Measured Rs 10 to 22 | **Rs 75 kept, and made a hard stop**: the loop does not start a call that could pass it. Lowering the figure is Manj's call |
| C-4 | `board_draw {request, panel?}` | Changing a drawing is slice 2 | **`board_draw {request}` only** in this slice |
| C-5 | At most 4 Claude calls | 3 of 10 test drawings lost a call to a reply cut off at 4,000 tokens (cause not confirmed) | **8,000 output tokens per call**, and a cut-off reply is told so and may be sent again within the 4 calls |
| C-6 | Runner takes requests from `core` | The workspace has no HTTP server library; the Canvas server is hand-written | **One JSON line in, one JSON line out, over TCP** on the internal network. No HTTP |
| C-7 | The CAD view opens when a drawing is maximised or on "open the CAD board" | Maximise and restore already exist, by click and voice | **The CAD view is how a maximised Drawing panel is shown.** "Open the CAD board" maximises the newest drawing; "back to the board" restores it. No new action |

## Operating rule

As for the Board: contracts and the spec own shared interfaces. If the code shows a contract or a step here is wrong, stop, write the mismatch and a proposed change in the task report, and wait for Manj. One task at a time: build with tests first, then an independent review, then fixes, then a handover note (`claude/cad-task-N-handover.md`). Branch `cad-board-slice-1` from `main` at `27ee333`. No merge, pull request to `main` or deploy without his words.

## Global Constraints

- Fictional content only in tests, fixtures and examples. No secrets, server names, addresses or account ids in any committed file (the repo is public).
- The engine binary is `cadcraft-cli` 0.12.0, Linux x86_64, SHA-256 `6ae27b9eabc3608caef356a00f69b9fdccbf1e408d6308b5614b6e6b256308de`, checked at image build. It is never run outside the `cad` container or a CI runner.
- The `cad` container: no egress, read-only file system, non-root, all capabilities dropped, memory 256 MB, no environment, no secrets, no database access. Everything it returns is untrusted data.
- Limits: 2,000 commands per recipe, 400 per step, 10 s per replay, picture at most 2,000,000 bytes, 4 Claude calls per drawing, 8,000 output tokens per call, Rs 75 (7,500 paise) per drawing request.
- Millimetres. The picture is the engine's SVG, black on white, shown as a white sheet on the dark Board, in an image element, never as markup.
- Jarvis never says the drawing is on the board before the desktop's `RENDERED` receipt.
- Orb, voice, reply line, news wall, Text and Canvas panels behave exactly as before. Existing tests stay green and unchanged unless a task names the change.
- Spoken sentences (exact): `CAD_ENGINE_DOWN_TEXT = "I couldn't draw that: the drawing engine isn't answering."`; `CAD_FAILED_TEXT = "I couldn't draw that."`; `CAD_PARTLY_TEXT = "The drawing is on the board, but it isn't finished."`; `CAD_TOO_LARGE_TEXT = "That drawing came out too large for the board. Try asking for less."`; `DRAW_ACK_TEXT = "On it. I'll draw it on the board."`. Budget and Claude-down sentences are the Board's own (`BOARD_CAP_TEXT`, `BOARD_DOWN_TEXT`).
- Tasks 9 and 10 build exactly Mock Board screens 4 and 5 and start only when Manj has frozen them.

## Review Focus

1. The engine container is down or slow: the request ends with `CAD_ENGINE_DOWN_TEXT`, said once; no Claude call is charged if none was made. (Task 5)
2. An engine upgrade writes an SVG element the check does not know: the picture is refused and never reaches the page. (Task 3)
3. A chunk is missing, repeated or arrives after a reconnect: a repeat is harmless; a gap means the panel is refused with `content_missing`, never a half picture. (Task 8)
4. Claude's reply is cut by the output limit: Claude is told, and the loop goes on inside the 4 calls. (Task 5)
5. A very small or very large drawing, or wild zooming: zoom is clamped between 0.1x and 40x of fit, and double-click always brings the whole sheet back. (Task 9)

---

### Task 1: Contracts for the Drawing panel and picture chunks

**Files:**
- Modify: `server/src/jarvis_server/contracts.py`, `server/src/jarvis_server/policy.py`
- Create: `contracts/messages/board.content.args.schema.json`, `contracts/messages/board.content.receipt.schema.json`, their examples under `contracts/examples/` as the existing Board schemas have them
- Modify: `contracts/messages/board.show.args.schema.json`, `contracts/messages/board.update.args.schema.json` (kind gains `drawing`), `contracts/README.md`
- Test: `server/tests/test_contracts.py`, `server/tests/test_message_contracts.py`, `server/tests/test_policy.py`

**Interfaces — Produces** (in `contracts.py`):
- `PanelKind.DRAWING = "drawing"`.
- `BOARD_PICTURE_MAX_BYTES = 2_000_000`, `BOARD_CHUNK_MAX_BYTES = 40_000`, `BOARD_CHUNKS_MAX = 64`, `DRAWING_STEPS_MAX = 40`, `DRAWING_STEP_LABEL_MAX = 80`.
- `class DrawingPicture(_Contract)`: `sha256: Sha256Hex`; `bytes: int` (1 to `BOARD_PICTURE_MAX_BYTES`); `media_type: Literal["image/svg+xml"]`.
- `class DrawingContent(_Contract)`: `engine: str` (`^[a-z0-9-]{1,30}$`); `engine_version: str` (1 to 30 characters, `^[0-9A-Za-z.+-]+$`); `units: Literal["mm"]`; `items: int` (0 to 100,000); `steps: tuple[str, ...]` (at most 40, each 1 to 80 characters, one line: no C0, DEL or C1 control, no U+2028 or U+2029, not blank); `picture: DrawingPicture`.
- `def drawing_content(d: DrawingContent) -> str`: the panel's `content`: JSON with sorted keys, separators `(",", ":")`, `ensure_ascii=False`.
- `def parse_drawing_content(content: str) -> DrawingContent`: raises `ValueError` unless `content` parses and equals `drawing_content` of what it parsed to.
- `BoardShowArgs` gains a check: when `kind` is `drawing`, `parse_drawing_content(content)` must succeed.
- `class BoardContentArgs(_Contract)`: `sha256: Sha256Hex` (of the whole picture, uncompressed); `media_type: Literal["image/svg+xml"]`; `encoding: Literal["gzip"]`; `bytes: int` (uncompressed, 1 to `BOARD_PICTURE_MAX_BYTES`); `index: int` (0 to 63); `count: int` (1 to 64, `index < count`); `data: str` (standard padded base64, 4 to 53,336 characters, decoding to at most `BOARD_CHUNK_MAX_BYTES`).
- `class ContentEvidence(_Check)`: `sha256: Sha256Hex | None`; `index: int | None`. `class BoardContentReceipt(_Contract)`: `reply_id`, `seq`, `result: Literal["STORED", "REFUSED"]`, `evidence: ContentEvidence`; `STORED` names both.
- `DESKTOP_MODELS` gains `"board.content.args"` and `"board.content.receipt"`.
- `policy.py`: `"board.content"` and `"board.draw"` are `Tier.READ_ONLY`; both target `{"DESKTOP"}`, as `board.make` does (`decide_tool_call` needs a row for every tier).

*As built (commit `4abe276` and the review-fix commit after it):* the tests are in `test_message_contracts.py`, where the Board's contract tests live. The schemas are hand-written and compared with the models by the existing tests. The model is stricter than the schema where a schema cannot see: `index < count`, the decoded chunk size, canonical base64, and the description inside `content`. Independent review: "sound after fixes"; its one important finding (a description of 10,000 nested brackets crashed instead of being refused) and the label rule above are fixed. Not checked here: the 20 policy tests that need Postgres; CI runs them once the branch is pushed.

- [ ] **Step 1: Write the failing tests.** `test_drawing_content_round_trips_and_is_canonical` (a fictional description; `parse_drawing_content(drawing_content(d)) == d`; the same JSON with a space added is refused). `test_show_args_accept_a_drawing_and_refuse_a_bad_description` (kind `drawing` with content `"hello"` is refused; a text panel with the same content still passes). `test_content_chunk_limits` (index 64 refused; `index == count` refused; `data` of 53,340 characters refused; non-base64 refused; a 40,000-byte chunk passes and the whole args fit 65,536 bytes with 3,500 to spare). `test_stored_receipt_names_the_chunk`. `test_board_content_is_read_only_for_desktops`. The schema-registry test must list the two new schemas.
- [ ] **Step 2: Run** `cd server && uv run pytest tests/test_contracts.py tests/test_message_contracts.py tests/test_policy.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the models, then regenerate or hand-write the schema files the way the existing Board schemas were made (the registry test compares them with the models).
- [ ] **Step 4: Run** the three files again, then `uv run pytest -q -x` for the whole server suite, `uv run ruff check . && uv run ruff format --check .` and the type check the repo's CI runs. Expected: PASS.
- [ ] **Step 5: Commit** `contracts: drawing panel and picture chunks`.

### Task 2: The engine image and its runner

**Files:**
- Create: `crates/jarvis-cad-runner/{Cargo.toml,src/main.rs,src/lib.rs,src/protocol.rs,src/engine.rs,src/replay.rs,src/bin/fake-cadcraft.rs}`, `crates/jarvis-cad-runner/tests/{replay.rs,real_engine.rs}`
- Create: `server/config/cad_commands.txt` (the 37 allowed command ids, one per line, copied from `ALLOWED` in `spikes/cad-engine/draw_test.py` on branch `spike-cad-engine`), `server/deploy/cad.Dockerfile`, `.github/workflows/cad.yml`
- Modify: root `Cargo.toml` (member), `.github/workflows/ci.yml` only if it must skip the new paths

**Interfaces — Produces:**
- Binary `jarvis-cad-runner --listen 0.0.0.0:7411 --engine /usr/local/bin/cadcraft-cli --commands /etc/jarvis/cad_commands.txt`. One connection carries one request line and one answer line (UTF-8 JSON, `\n`-ended), then closes.
- Request `{"v":1,"op":"replay","steps":[{"label":str,"commands":[{"command":str,"params":object}]}],"picture":bool}` or `{"v":1,"op":"catalogue"}`.
- Answer to `replay`: `{"v":1,"ok":true,"engine":"cadcraft","engine_version":str,"results":[{"step":int,"n":int,"command":str,"ok":bool,"text":str}],"failed":int,"counts":{str:int},"items":int,"extents":[w,h]|null,"texts":[str],"png_b64":str|null,"svg_b64":str|null}`. `text` is one line, at most 300 characters; `texts` at most 80 entries of at most 80 characters; `png_b64` is an 800x600 render, always present when the engine started; `svg_b64` only when `picture` is true and `failed` is 0.
- Answer to `catalogue`: `{"v":1,"ok":true,"engine_version":str,"commands":{id:params_note}}` for the allowed ids only.
- Failure: `{"v":1,"ok":false,"error":code}` with `code` one of `bad_request`, `not_allowed`, `too_many_commands`, `timeout`, `busy`, `engine_failed`, `too_large`.
- Rules: request line at most 1,048,576 bytes; a command id outside the commands file is `not_allowed` before the engine starts; more than 2,000 commands is `too_many_commands`; the engine child is started fresh per request with an empty environment and a new empty directory under `/tmp` as its working directory, and is killed, and the directory removed, when the answer is written or 10 s have passed (`timeout`); one replay at a time, a second waits at most 10 s then `busy`; an SVG over 2,000,000 bytes is `too_large`. A replay begins with the engine's `new_drawing {"metric":true}`; commands go through its `execute` tool; counts and extents come from `inspect_drawing`, texts from `find_text {"limit":80}`, the PNG from `render`, the SVG from `save` to the working directory (read back, then deleted). The MCP details are in `spikes/cad-engine/measure.py` (`class Mcp`) and `draw_test.py` (`Engine.replay`).
- `src/bin/fake-cadcraft.rs`: a stand-in MCP server for unit tests (counts lines, texts and dimensions it is sent; fails a command named `fail`; sleeps on `sleep`).
- Image: `server/deploy/cad.Dockerfile` builds the runner, downloads the pinned release, checks the SHA-256, and ends on `debian:trixie-slim` with `USER 10001:10001`, the runner as entry point, port 7411. Published as `ghcr.io/manjunathhari/jarvis-cad:<commit>` by `cad.yml` the way `server.yml` publishes the server image.

- [ ] **Step 1: Write the failing tests** in `tests/replay.rs` against the fake engine: `replays_steps_in_order_and_reports_each_command`; `a_failed_command_is_counted_and_no_picture_is_returned`; `a_command_outside_the_list_is_refused_before_the_engine_starts`; `more_than_2000_commands_is_refused`; `a_replay_past_ten_seconds_is_killed_and_answers_timeout` (the test sets the limit to 1 s through a builder, not the 10 s default); `the_engine_gets_no_environment_and_an_empty_directory`; `a_request_line_over_one_mebibyte_is_bad_request`; `a_second_request_waits_then_is_busy`.
- [ ] **Step 2: Run** `cargo test -p jarvis-cad-runner`. Expected: FAIL (crate has no code).
- [ ] **Step 3: Implement** the crate: `protocol.rs` (serde types above), `engine.rs` (child process and JSON-RPC lines), `replay.rs` (`pub async fn replay(cfg: &Config, req: Request) -> Answer`), `main.rs` (listener). Dependencies: `tokio` (add features `process`, `net`, `io-util` for this crate), `serde`, `serde_json`, `data-encoding`.
- [ ] **Step 4: Write `tests/real_engine.rs`,** skipped unless `JARVIS_CAD_ENGINE` names the real binary: `draws_the_two_room_plan` (the canned plan from `draw_test.py`'s `CANNED`; expects counts `Line` 5, `Text` 2, `Dimension` 2 and both room names in `texts`); `the_same_recipe_twice_gives_the_same_svg_bytes`; `the_svg_uses_only_these_elements_and_attributes` (prints the sets and asserts they are within Task 3's allow-list); `the_catalogue_names_every_allowed_command`.
- [ ] **Step 5: Write `cad.yml`:** on changes under `crates/jarvis-cad-runner/**`, `server/deploy/cad.Dockerfile`, `server/config/cad_commands.txt`: `cargo fmt --check`, `cargo clippy -p jarvis-cad-runner -- -D warnings`, `cargo test -p jarvis-cad-runner`; download and verify the pinned engine, run the real-engine tests; build the image; run it with `--network none --read-only --tmpfs /tmp --cap-drop ALL --memory 256m` and replay the canned plan through port 7411 from a second container on a private network. `permissions: contents: read` for the test job; `packages: write` only in the publish job, which runs on `main`.
- [ ] **Step 6: Run** locally `cargo test -p jarvis-cad-runner && cargo clippy -p jarvis-cad-runner -- -D warnings && cargo fmt --check`; push the branch and confirm `cad.yml` is green, including the real-engine tests. If the two replays do not give the same SVG bytes, stop and report: pass mark 2 of the design depends on it.
- [ ] **Step 7: Commit** `cad: engine runner and image`.

### Task 3: Server side of the engine, the picture check and the store

**Files:**
- Create: `server/src/jarvis_server/core/cad_engine.py`, `server/src/jarvis_server/core/drawing.py`, `server/migrations/versions/0017_drawings.py`, `server/tests/fakes/cad_runner.py`
- Test: `server/tests/test_cad_engine.py`, `server/tests/test_drawing_store.py`

**Interfaces — Consumes:** Task 1's `DrawingContent`, limits; Task 2's protocol and `server/config/cad_commands.txt`.

**Interfaces — Produces:**
- `cad_engine.ALLOWED: frozenset[str]` (from the commands file); `STEP_MAX = 400`; `RECIPE_MAX = 2000`.
- `@dataclass(frozen=True) class Step: label: str; commands: tuple[dict[str, Any], ...]`; `def step_from(args: dict[str, Any]) -> Step` raises `BadStep(reason: str)` (label not 1 to 80 characters on one line; no commands; a command that is not `{command: allowed id, params: object}`; over `STEP_MAX`).
- `@dataclass(frozen=True) class CommandResult: step: int; n: int; command: str; ok: bool; text: str`.
- `@dataclass(frozen=True) class Replay: results: tuple[CommandResult, ...]; failed: int; counts: dict[str, int]; items: int; extents: tuple[float, float] | None; texts: tuple[str, ...]; png: bytes | None; svg: bytes | None; engine: str; engine_version: str`.
- `class EngineDown(Exception)` (no connection, timeout, `busy`, `engine_failed`, a malformed answer); `class EngineRefused(Exception)` with `.code` (`too_many_commands`, `too_large`, `not_allowed`, `timeout`).
- `def replay(steps: Sequence[Step], *, picture: bool, address: str | None = None) -> Replay`; `def catalogue(address: str | None = None) -> dict[str, str]`. Address from env `JARVIS_CAD_ADDR`, default `cad:7411`; connect timeout 3 s, whole exchange 15 s. Every field of the answer is checked for type and size before use.
- `def check_svg(svg: bytes) -> None` raises `BadPicture(reason)`: over `BOARD_PICTURE_MAX_BYTES`; contains `<!DOCTYPE`, `<!ENTITY` or `<?` other than a leading XML declaration; not well-formed; root not `svg` in the SVG namespace; an element outside `{svg, g, rect, line, polyline, polygon, path, circle, ellipse}`; an attribute outside `{xmlns, viewBox, width, height, x, y, x1, y1, x2, y2, cx, cy, r, rx, ry, d, points, fill, stroke, stroke-width, stroke-linecap, stroke-linejoin, stroke-dasharray, transform, opacity, fill-opacity, stroke-opacity}`; any attribute value containing `url(`, `javascript:` or `&#`. (The engine's own output, measured: `svg`, `rect`, `polyline`, `path` only; text is drawn as strokes.)
- Migration `0017`: `board.panel.kind` check gains `'drawing'`; `board.picture(person_id uuid, sha256 text CHECK (sha256 ~ '^[0-9a-f]{64}$'), media_type text, body bytea CHECK (octet_length(body) BETWEEN 1 AND 2000000), created_at timestamptz DEFAULT now(), PRIMARY KEY (person_id, sha256))`; `board.drawing(panel_id uuid, revision int, engine text, engine_version text, recipe jsonb, picture_sha256 text, PRIMARY KEY (panel_id, revision), FOREIGN KEY (panel_id, revision) REFERENCES board.panel_revision)`. `jarvis_app` may SELECT and INSERT both, nothing else. Downgrade refuses once a drawing exists.
- `drawing.put_picture(conn, person_id, svg: bytes) -> str` (the hash; storing the same picture twice is not an error); `drawing.picture(conn, person_id, sha256) -> bytes` (raises `LookupError`); `drawing.save(conn, *, panel_id, revision, replay: Replay, steps: Sequence[Step], picture_sha256: str) -> None`; `drawing.recipe(conn, person_id, panel_id) -> tuple[Step, ...]`.
- `board.check_content` accepts kind `drawing` when `contracts.parse_drawing_content(content)` succeeds.
- `tests/fakes/cad_runner.py`: `class FakeRunner` (a thread serving Task 2's protocol on an ephemeral port; scripted answers; records requests), used by Tasks 5 and 11.

- [ ] **Step 1: Write the failing tests.** `test_cad_engine.py`: `test_replay_parses_a_good_answer`; `test_no_listener_is_engine_down`; `test_busy_and_engine_failed_are_engine_down`; `test_too_large_is_engine_refused`; `test_an_answer_with_wrong_types_is_engine_down`; `test_step_from_refuses_a_command_outside_the_list`; `test_step_from_refuses_401_commands`; `test_check_svg_accepts_the_engines_own_shapes` (a fictional SVG of `rect`, `polyline`, `path`); and for Review Focus 2, one test per refusal: `script` element, `foreignObject`, `image`, `use`, an `onload` attribute, a `style` attribute, `href`, a `DOCTYPE`, an entity, `url(` in `fill`, a 2,000,001-byte file, bad XML. `test_drawing_store.py`: `test_picture_round_trips_and_is_per_person`; `test_same_picture_twice_is_one_row`; `test_recipe_comes_back_as_saved`; `test_another_person_cannot_read_a_picture`; `test_panel_kind_drawing_is_storable`.
- [ ] **Step 2: Run** `cd server && uv run pytest tests/test_cad_engine.py tests/test_drawing_store.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** XML parsing with the standard library after the byte checks above (they are what stops entity expansion).
- [ ] **Step 4: Run** both files, the migration tests the repo has for upgrade and downgrade, then the whole suite and lint. Expected: PASS.
- [ ] **Step 5: Commit** `server: CAD engine client, picture check and drawing store`.

### Task 4: A brain call that can be continued with tool results

**Files:**
- Modify: `server/src/jarvis_server/brain/types.py`, `server/src/jarvis_server/brain/claude.py`, the brain fakes under `server/tests/fakes/`
- Test: `server/tests/test_brain_claude.py`

**Interfaces — Produces:**
- `ToolCall` gains `call_id: str = ""` (the API's `tool_use` id; `ClaudeBrain` fills it).
- `@dataclass(frozen=True) class ToolResult: call_id: str; text: str; image_png: bytes | None = None; is_error: bool = False`.
- `@dataclass(frozen=True) class Exchange: text: str; calls: tuple[ToolCall, ...]; results: tuple[ToolResult, ...]; note: str = ""`. One earlier round of the same job: what the model sent, and what it is given back. `note` is plain text for the user turn when there were no calls.
- `BrainRequest` gains `exchanges: list[Exchange] = field(default_factory=list)`.
- `build_payload`: after the utterance, each exchange adds an assistant message (a `text` block if `text` is not blank, then one `tool_use` block per call with its `call_id`, name and args) and a user message: one `tool_result` block per result, in call order, all before anything else, each with `tool_use_id`, `content` of a `text` block and, when present, an `image` block (`base64`, `image/png`), and `is_error: true` when set; or, with no calls, the `note` as text. An exchange whose results do not match its calls one for one is a `ValueError`.
- `IMAGE_TOKEN_ALLOWANCE = 2_000`: `count_input_tokens` counts each image as this many tokens and leaves its base64 out of the byte count.
- A request without exchanges builds exactly the payload it builds today.

- [ ] **Step 1: Write the failing tests:** `test_payload_without_exchanges_is_unchanged` (compare with a payload captured before the change); `test_exchange_becomes_tool_use_then_tool_result`; `test_tool_results_come_first_and_in_call_order`; `test_image_is_sent_as_base64_png`; `test_an_error_result_is_marked`; `test_results_must_match_calls`; `test_a_note_is_sent_when_there_were_no_calls`; `test_images_are_counted_by_allowance_not_bytes`; `test_tool_call_carries_the_api_id` (a recorded stream with a `tool_use` block).
- [ ] **Step 2: Run** `uv run pytest tests/test_brain_claude.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, then the whole suite (the turn and Board job tests prove nothing else moved) and lint. Expected: PASS.
- [ ] **Step 5: Commit** `brain: continue a call with tool results`.

### Task 5: The drawing job

**Files:**
- Create: `server/src/jarvis_server/core/cad_job.py`
- Modify: `server/src/jarvis_server/core/board_node.py` (receipts for `board.content`), `server/src/jarvis_server/core_main.py` (register the job module)
- Test: `server/tests/test_cad_job.py`, `server/tests/test_board_job.py` (only if a shared helper moves)

**Interfaces — Consumes:** Tasks 1, 3, 4; from `board_job`: `_work`, `_said`, `_owned`, `_headline`, the reply and `board.headline` queueing used by `_deliver`; `budget.reserve/settle/release/lapse`; `actions.issue_live`.

**Interfaces — Produces:**
- Job kind `cad.make`, payload as `board.make`: `{session_id, person_id, node_id, inbound_id, request}`.
- Constants: `CAD_MAX_CALLS = 4`; `CAD_MAX_TOKENS = 8_000`; `CAD_RESERVE_PAISE = 7_500`; the sentences in Global Constraints.
- Tools given to Claude: `cad_step {label, commands:[{command, params}]}` and `cad_finish {title, headline}`. `CAD_PROMPT`: the text of `PROMPT` in `spikes/cad-engine/draw_test.py`, with the person's name as the Board prompt has it, followed by one line per allowed command with its parameter note from `cad_engine.catalogue()` (kept in memory per engine version).
- `def run(conn, job) -> None`, registered for `cad.make`.

Behaviour, in order:
1. The checks of `board.make` steps 1 to 3 (turn `ANSWERED`, ownership, a re-run after a crash never calls again, Claude DOWN said). Then `cad_engine.catalogue()`; `EngineDown` here is `CAD_ENGINE_DOWN_TEXT`, no reservation.
2. `budget.reserve` for `CAD_RESERVE_PAISE`. No budget: `BOARD_CAP_TEXT`, no call.
3. The loop, at most `CAD_MAX_CALLS` rounds. Before each call: if what is spent so far plus `prices.cost_paise(model, count_input_tokens(req), CAD_MAX_TOKENS)` is more than `CAD_RESERVE_PAISE`, the loop ends. Each round's tool calls are answered in order:
   - `cad_step`: `step_from`; a `BadStep` or a recipe over 2,000 commands is an error result naming the reason. Otherwise replay the recipe with the step added, `picture=False`. All commands worked: the step is kept. Any failed: the step is not kept, and the result lists the failed commands (at most 25) and why. The result text is the report `draw_test.py`'s `step_report` writes, with the PNG; before the last round it adds that the next turn is the last.
   - `cad_finish`: refused if a step in the same round was not kept or nothing is drawn; otherwise the loop ends as finished, with the title (through `PanelTitle`) and headline.
   - A reply with no tool call: if the output limit cut it (`brain.truncated`), the round is answered with the note `"Your reply was cut off before the step was complete. Send the step again, in one piece."` and the loop goes on; otherwise the loop ends.
   - `EngineDown` during the loop ends it; `EngineRefused` is an error result with its code.
4. Money: CONNECT failure on the first call releases the reservation and says `BOARD_DOWN_TEXT`; a SEND failure or a completed call without usage lapses the reservation in full; otherwise the reservation is settled once, at the sum of the calls' real costs.
5. Delivery, only if the recipe has a kept step: replay with `picture=True`; `check_svg`; `drawing.put_picture`; `board.create(kind="drawing", title=..., content=drawing_content(...), cost_paise=...)`; `drawing.save`; then in one reply that answers no utterance: `board.content` for each 40,000-byte chunk of `gzip.compress(svg, mtime=0)` in order, then `board.show`. The title is Claude's, or the request's first 120 characters if the loop did not finish. `EngineDown`, `BadPicture`, `too_large` or a failed final replay: nothing is stored or sent, and the matching sentence is said (`CAD_ENGINE_DOWN_TEXT`, `CAD_FAILED_TEXT`, `CAD_TOO_LARGE_TEXT`).
6. The headline goes through `board.headline` exactly as for a Board panel: Claude's headline when finished, `CAD_PARTLY_TEXT` when the loop ended without `cad_finish`. Nothing drawn at all: `CAD_FAILED_TEXT`, said directly.
7. `board_job._store_and_issue` refuses a `board_show` whose kind is `drawing` (`_BadCall`): only this job makes drawing panels.
8. `board_node.on_live_receipt`: a `board.content` receipt is recorded like any receipt and never counts as a panel answered; a `REFUSED` one is logged. The `board.show` receipt alone decides the headline.

- [ ] **Step 1: Write the failing tests** with a scripted brain and Task 3's `FakeRunner`: `test_a_request_becomes_a_drawing_panel_and_chunks` (two rounds; checks the stored recipe, the picture row, the panel's content, the chunk count and order, that the chunks' gunzip has the picture's hash, and that nothing is spoken yet); `test_a_step_with_a_failed_command_is_not_kept_and_claude_is_told`; `test_finish_is_refused_in_a_round_with_a_failed_step`; `test_the_loop_stops_at_four_calls_and_says_partly`; `test_a_cut_off_reply_is_told_and_the_loop_goes_on` (Review Focus 4); `test_engine_down_before_any_call_charges_nothing` and `test_engine_down_mid_loop_shows_the_last_good_recipe` (Review Focus 1); `test_the_loop_never_starts_a_call_that_could_pass_the_reservation`; `test_a_refused_picture_is_never_sent`; `test_too_large_is_said`; `test_connect_failure_releases_and_send_failure_lapses`; `test_settled_at_the_sum_of_the_calls`; `test_a_rerun_after_a_crash_never_calls_again`; `test_headline_waits_for_the_show_receipt_not_the_chunks`; `test_board_make_cannot_create_a_drawing_panel`; `test_the_request_and_tool_results_are_marked_as_data` (the prompt carries the data notice; a step label saying "ignore your instructions" changes nothing).
- [ ] **Step 2: Run** `uv run pytest tests/test_cad_job.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `cad_job.py`, following `board_job.py`'s transactions (budget steps and each store step on the committing sibling connection; no lock held during a call or a replay).
- [ ] **Step 4: Run** the file, `tests/test_board_job.py`, the whole suite and lint. Expected: PASS.
- [ ] **Step 5: Commit** `server: the drawing job`.

### Task 6: The voice tool

**Files:**
- Modify: `server/src/jarvis_server/core/prompt.py`, `server/src/jarvis_server/core/turn.py`
- Test: `server/tests/test_turn_board.py`

**Interfaces — Produces:**
- Tool `board_draw {request}` (`request` as `board_make`'s). Description: `"Make a technical drawing on the person's Board with the CAD engine: a floor plan, a part, a schematic or a site layout, with real dimensions. The drawing is made after this reply and appears as a panel."` `board_make`'s description loses the words `or a drawing`. `TOOL_ACTIONS["board_draw"] = "board.draw"`.
- The Board lines of the voice prompt gain: a drawing with dimensions goes to `board_draw`, everything else to `board_make`; "open the CAD board" means maximise the newest drawing panel; "back to the board" means restore it.
- `turn._board`: kind `board.draw` queues `cad.make` with the payload of `board.make`, keyed by the turn. One Board job per turn across both kinds: the second call of either kind in a turn is left out and logged. It issues `board.working` as `board.make` does. With no words from the brain the reply is `DRAW_ACK_TEXT`.

- [ ] **Step 1: Write the failing tests:** `test_board_draw_queues_one_cad_job_and_says_working`; `test_draw_then_make_in_one_turn_queues_only_the_first`; `test_board_draw_without_a_request_is_refused`; `test_the_prompt_tells_drawings_from_other_board_work`; `test_open_the_cad_board_is_a_maximise` (scripted brain calls `board_set_state`; nothing new is issued).
- [ ] **Step 2: Run** `uv run pytest tests/test_turn_board.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, the whole suite and lint; if `evals/` has a tool-choice check, run it as its README says. Expected: PASS.
- [ ] **Step 5: Commit** `server: board_draw`.

### Task 7: The `cad` service in the deployment

**Files:**
- Modify: `server/deploy/compose.yaml`, `.github/workflows/server.yml` only if it must know the new image, the deploy notes the repo keeps beside the compose file
- Test: `server/tests/test_compose.py`

**Interfaces — Produces:** service `cad`: image `ghcr.io/manjunathhari/jarvis-cad:${JARVIS_REF...}` with the same guard as the server image; `networks: [jarvis_internal]`; `read_only: true`; `tmpfs: ["/tmp:rw,size=64m,mode=1777"]`; `cap_drop: [ALL]`; `security_opt: ["no-new-privileges:true"]`; `user: "10001:10001"`; `mem_limit: 256m`; `pids_limit: 128`; no `environment`, `secrets`, `volumes`, `ports` or `depends_on` on the database. `core` gains `JARVIS_CAD_ADDR: cad:7411`.

- [ ] **Step 1: Write the failing tests:** `test_cad_has_no_secrets_environment_volumes_or_ports`; `test_cad_is_only_on_the_internal_network`; `test_cad_is_read_only_capless_and_limited`; `test_core_knows_the_cad_address`; `test_no_other_service_changed` (a snapshot of the other services' keys).
- [ ] **Step 2: Run** `uv run pytest tests/test_compose.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and `docker compose -f server/deploy/compose.yaml config -q` with the placeholder environment the existing compose test uses. Expected: PASS.
- [ ] **Step 5: Commit** `deploy: cad service`. Deploying it is a separate step, on Manj's word, with a rollback note.

### Task 8: The desktop node keeps and serves the picture

**Files:**
- Create: `crates/jarvis-node/src/content.rs`, `crates/jarvis-node/tests/content.rs`
- Modify: `crates/jarvis-node/src/{lib.rs,board.rs,node.rs,canvas.rs,wire.rs}`, `crates/jarvis-node/Cargo.toml` (`flate2`), `crates/jarvis-node/src/bin/jarvis-node-probe.rs` (answers `board.content`), `crates/jarvis-node/tests/board.rs`, the canvas tests
- Modify: `app/src-tauri/src/board.rs` (pass the store's folder), `app/src-tauri/tauri.conf.json` (`img-src` gains the three loopback origins) and its test

**Interfaces — Produces:**
- `board::PanelKind::Drawing`; `board::Drawing { engine, engine_version, units, items, steps, picture_sha256, picture_bytes }`; `pub fn parse_drawing(content: &str) -> Result<Drawing, &'static str>` with Task 1's limits, its step-label rule (no C0, DEL or C1 control, no U+2028 or U+2029, not blank) and the canonical-form rule (check `"drawing_content"`); nesting deeper than the description's own shape is refused, never a crash.
- `content::ContentStore::open(dir: &Path) -> Self` (folder `pictures/` beside `board.json`); `pub fn put_chunk(&mut self, chunk: &Chunk, now: Instant) -> Result<Stored, &'static str>` where `Stored` is `Kept` or `Complete`; `pub fn has(&self, sha256: &str) -> bool`; `pub fn read(&self, sha256: &str) -> io::Result<Vec<u8>>`; `pub fn parse_chunk(args: &Map<String, Value>) -> Result<Chunk, &'static str>`.
- Chunk rules, each refusal a check code: unknown keys or bad fields `chunk_malformed`; a chunk for a picture already kept is `Complete` at once; chunks may come in any order and a repeat of the same bytes is `Kept`; a repeat with different bytes, or a `count` or `bytes` that differs from the first chunk seen, is `chunk_conflict`; at most 2 pictures in assembly, the older dropped; an assembly untouched for 120 s is dropped; on the last missing chunk: gunzip, stopping at `bytes + 1` (`picture_too_large` if it does not end exactly at `bytes`), SHA-256 must equal `sha256` (`picture_hash`), then write to a temporary file and rename to `pictures/<sha256>`.
- `node.rs`: a live `board.content` action is answered `STORED` with `{sha256, index}` or `REFUSED` with the check; it is never passed to the app. `board.show` or `board.update` of kind `drawing` is refused with check `content_missing` unless `ContentStore::has(picture_sha256)`.
- `canvas.rs`: `GET /<secret>/<panel id>/<revision>/picture` answers 200 with the picture's bytes for a drawing panel at the revision the store holds, `Content-Type: image/svg+xml`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`; it marks the panel served as a Canvas page does. The Canvas path never answers for a drawing, nor the picture path for a canvas. `CanvasServer::picture_url(panel_id, revision) -> String`.
- The Board snapshot the app gives the page carries a drawing panel's `content` unchanged (the description).

- [ ] **Step 1: Write the failing tests.** `tests/content.rs`: `chunks_in_any_order_make_the_picture`; `a_repeated_chunk_is_harmless`; `a_repeat_with_other_bytes_is_a_conflict`; `a_missing_chunk_never_makes_a_file` (Review Focus 3); `a_wrong_hash_is_refused_and_nothing_is_written`; `a_gzip_that_grows_past_its_size_is_stopped`; `a_third_picture_drops_the_oldest_assembly`; `a_stale_assembly_is_dropped`; `a_kept_picture_survives_reopening`. `tests/board.rs`: `a_drawing_panel_parses_and_a_loose_description_is_refused`; `a_drawing_without_its_picture_is_refused_content_missing`. Canvas tests: `the_picture_path_serves_svg_with_a_closed_policy`; `the_picture_path_is_404_for_a_canvas_and_the_page_path_for_a_drawing`; `a_served_picture_is_marked_once`. Config test: `img_src_allows_only_self_data_and_the_three_ports`.
- [ ] **Step 2: Run** `cargo test -p jarvis-node`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cargo test -p jarvis-node && cargo clippy -p jarvis-node --all-targets -- -D warnings && cargo fmt --check`; the server's probe tests (`JARVIS_NODE_PROBE` set) still pass. The app crate compiles only on Windows CI: push and confirm it is green.
- [ ] **Step 5: Commit** `desktop: picture store and the picture address`.

### Task 9: The Drawing frame on the Board page (Mock Board screen 4, frozen)

**Files:**
- Create: `app/src/board/drawing.ts`, `app/src/board/drawing.test.ts`
- Modify: `app/src/board/{frames.ts,model.ts,panels.ts,board.css}`, `app/src-tauri/src/board.rs` (command `board_picture_address`)

**Interfaces — Produces:**
- `model.ts`: `Panel["kind"]` gains `"drawing"`. `panels.ts`: `KIND_LABEL.drawing = "Drawing"`.
- `drawing.ts`: `export interface Drawing { engine: string; engineVersion: string; units: string; items: number; steps: string[] }`; `export function readDrawing(content: string): Drawing | null`; `export interface View { scale: number; x: number; y: number }`; `export function fit(): View` (`{scale: 1, x: 0, y: 0}`); `export function zoomAt(v: View, factor: number, px: number, py: number): View` (scale clamped to 0.1 to 40; the point under the cursor stays put); `export function pan(v: View, dx: number, dy: number): View`; `export async function mountDrawing(body: HTMLElement, panel: Panel, app: CanvasApp): Promise<boolean>`.
- `mountDrawing` builds: a `.sheet` (white) holding one `<img alt="Drawing: <title>">` whose `src` is the app's picture address and whose transform is the view; under it a `.foot` line `"mm · N items · K steps"` and `"Wheel to zoom · drag to move · double-click to fit"`. Wheel zooms at the cursor, drag pans, double-click fits. It resolves true only after the image's `load` event **and** `app.served(panelId, revision)` is true; on `error`, or a description that does not read, the body says `"This drawing couldn't be shown."` and it resolves false.
- The picture is never fetched by script and never put in the page as markup.

- [ ] **Step 1: Write the failing tests:** `reads_a_description_and_refuses_a_broken_one`; `zoom_keeps_the_point_under_the_cursor`; `zoom_is_clamped_between_a_tenth_and_forty` (Review Focus 5); `double_click_fits_again`; `drawn_only_after_load_and_served`; `a_failed_image_says_so_and_is_not_drawn`; `the_foot_line_counts_items_and_steps`; `the_picture_is_an_img_and_nothing_else` (no `svg`, `iframe` or `object` element in the body); and in `panels.test.ts`, `a_drawing_panel_is_labelled_drawing`.
- [ ] **Step 2: Run** `cd app && npx vitest run`. Expected: FAIL.
- [ ] **Step 3: Implement,** matching screen 4's layout and the Board's existing colours.
- [ ] **Step 4: Run** `npx vitest run && npm run build`; drive the built page in headless Chrome with the stand-in app as Task 6 of the Board plan did, and put a screenshot beside the mock in the task report. Expected: PASS.
- [ ] **Step 5: Commit** `desktop: Drawing frame`.

### Task 10: The CAD view (Mock Board screen 5, frozen)

**Files:**
- Create: `app/src/board/cadview.ts`, `app/src/board/cadview.test.ts`
- Modify: `app/src/board/{main.ts,layout.ts,board.css}`, `app/board.html`

**Interfaces — Consumes:** Task 9's `readDrawing`, `mountDrawing`.

**Interfaces — Produces:**
- `export function cadMode(panels: Panel[]): Panel | null`: the maximised panel if it is a drawing and open on the page, else null.
- `export function applyCadView(root: HTMLElement, panel: Panel | null, formatRupees: (paise: number) => string): void`: with a panel, `body` gets class `cad`: the top bar reads `Board · CAD`, the drawing's title, `"mm · N items · Rs X"` (the panel's own cost) and a button `Back to the board`; a `Steps` strip on the right lists the step labels, numbered, with the note `"Undo and spoken changes come with the next slice."`; the shelf and the panel's own header are hidden; the drawing fills the centre. With null, everything is as before.
- `Back to the board` calls the existing `board_set_state` with `open` for that panel. Nothing new is sent to the server.

- [ ] **Step 1: Write the failing tests:** `a_maximised_drawing_is_cad_mode_and_a_maximised_text_panel_is_not`; `the_top_bar_names_the_drawing_units_items_and_cost`; `steps_are_listed_in_order_and_as_text` (a label with `<b>` is shown as typed); `back_restores_the_panel`; `leaving_cad_mode_puts_the_board_back` (counts, spend and shelf visible again); `a_new_panel_arriving_leaves_cad_mode` (the existing rule that an arrival un-maximises).
- [ ] **Step 2: Run** `npx vitest run`. Expected: FAIL.
- [ ] **Step 3: Implement,** matching screen 5.
- [ ] **Step 4: Run** `npx vitest run && npm run build`; headless screenshots of both states at 1920x1080 and 1280x800 in the task report. Expected: PASS.
- [ ] **Step 5: Commit** `desktop: CAD view`.

### Task 11: End to end, and the live check

**Files:**
- Modify: `server/tests/test_rust_node_board.py`, `server/tests/test_acceptance.py` if the Board's acceptance test lives there
- Create: `claude/cad-slice-1-live-check.md` (project doc, not in the repo)

- [ ] **Step 1: Write the failing tests** (skipped unless `JARVIS_NODE_PROBE` is set; brain scripted; engine is Task 3's `FakeRunner`): `test_a_spoken_drawing_request_becomes_a_rendered_drawing` (acknowledgement, `cad.make`, chunks stored by the probe, `board.show`, `--render-next`, the server's `rendered_revision`, then the headline as its own reply); `test_the_headline_is_not_spoken_if_the_drawing_is_refused`; `test_a_node_without_the_picture_refuses_the_panel` (the probe drops one chunk); `test_replaying_the_stored_recipe_gives_the_same_hash`; `test_the_drawing_is_back_after_a_node_restart`; `test_text_and_canvas_requests_behave_as_before`.
- [ ] **Step 2: Run** the server suite with the probe built. Expected: the new tests FAIL, then PASS once any gap they find is fixed in the owning task's files.
- [ ] **Step 3: Run everything:** server suite and lint; `cargo test --workspace`, clippy, fmt; `npx vitest run`, `npm run build`; push and confirm CI and `cad.yml` are green on Windows and Linux.
- [ ] **Step 4: Write the live-check list** for Manj's PC, each server step on his word. Order: desktop build first, then the server (migration 0017 and the `cad` service). Checks: (1) a fictional spoken request for a flat plan: acknowledgement within 3 s, a Drawing panel with labels, notes and a title block, the headline only after it is drawn; (2) wheel, drag and double-click; (3) maximise opens the CAD view, "back to the board" by click and by voice; (4) minimise, close and reopen by click and voice; (5) restart the app: the drawing is back; (6) a Text and a Canvas request, the orb, "Hey Jarvis" and the news wall as before; (7) the cost of the drawing from the top bar, and the time from request to drawn, recorded for Manj; (8) on the server: `cad` has no route out (a connection attempt from inside fails) and holds no secrets.
- [ ] **Step 5: Commit** `tests: drawing end to end`.

## Not in this plan

Changing a drawing and undo, DXF and PDF copies (slice 2); opening existing files (slice 3); a desktop asking for a picture it lacks; the phone node; the engineering workspace.
