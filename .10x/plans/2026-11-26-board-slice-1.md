# JARVIS Board, slice 1 (shell, Conversation, Text, Canvas) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

*Status: approved by Manj on 26 Nov 2026, with the seven changes below. Tasks 1 to 4 are built on local branch `board-slice-1` and independently reviewed, with the reviews' fixes applied; Tasks 5 to 9 are not started. Repo `manjunathhari/jarvisdesktop`, `main` = `983c2cf`.*

**Goal:** Manj asks by voice; Jarvis answers briefly, does the work, and shows the result as a panel on a Board window on monitor 2, with a running conversation and minimise, maximise and close on every panel, by click or by voice.

**Architecture:** The server owns panels. A voice turn stays short and can hand work to a new server job (`board.make`), which calls Claude with a larger output limit and web search, stores the panel, and sends it to the desktop as a core-signed live action (`board.show`), followed by a spoken headline. The desktop's `jarvis-node` checks the action, stores the panel locally, and the app's second window renders it; only then is the receipt `RENDERED` sent. A Canvas panel is served from its own origin with its own content policy inside a sandboxed frame.

**Tech Stack:** Python 3.12 server (psycopg, alembic, httpx, pytest); Rust workspace (`jarvis-node`, Tauri 2 app); TypeScript with Vite; new front-end packages `markdown-it`, `highlight.js`, `vitest` (dev); Tauri plugin `tauri-plugin-opener`. Anthropic Messages API with the server-run web search tool.

**Spec:** project doc `claude/jarvis-board-design-brief.md` (the brief). Protocol rules: `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md` §6 to §9. Mock: https://claude.ai/artifact/5MeQCjCSPohrB1MQeip9aM

## Decisions for Manj (where the code forced a change from the brief)

| # | The brief said | The code says | Proposed |
|---|---|---|---|
| P-1 | One reply: Jarvis says the headline and shows the detail | The desktop cuts any reply not finished in 8 s (`BRAIN_TIMEOUT`), and voice turns are capped at 400 output tokens. Research takes 30 to 90 s | **Two steps.** Jarvis answers at once ("On it, I'll put it on the board"), the work runs as a server job, and Jarvis speaks the headline when the panel is up. Panel controls ("minimise the chart") stay instant, in the voice turn |
| P-2 | Text fills its panel as it streams | A tool call's content arrives whole | **The panel appears complete** in slice 1, with "Jarvis is working…" in the Conversation column meanwhile. Streaming later |
| P-3 | Large content: inline or fetched | A message is limited to 65,536 bytes | **Inline, up to 30,000 bytes** of content per panel (lowered from 32,000 after the Task 1 review, so the worst case still fits a message). Larger is refused with a plain spoken reason. Fetching by ID comes with the Document frame |
| P-4 | A `board_read` tool | The server makes one Claude call per job, with no tool-result round trip | **No read tool.** The server already holds every panel, so it gives Jarvis the panel list on every turn and the open panels' content in a Board job |
| P-5 | Panels come back after a restart, from the server | The protocol has no "send me everything" message | **The desktop keeps its own copy** and restores from it. The server stays the record. A full server-to-desktop resync comes with the phone app |
| P-6 | A charting library bundled for Canvas | The sealed page allows only its own inline code | **No library in slice 1.** Jarvis writes plain HTML, SVG and script |
| P-7 | Top bar shows today's spend | The desktop has no channel for the server's budget figures | **Each panel carries its own cost**; the bar shows today's Board total from those |

## Operating rule

As in Plan B: contracts and the spec own shared interfaces. If the code shows a contract, ruling or step here is wrong, stop, write the mismatch and a proposed change in the task report, and wait for Manj. One task at a time; an independent review and its fixes after each; then stop for Manj. Work stays local: no push, merge, PR or deploy without his separate approval.

## Global Constraints

- **Public repo:** no secrets, addresses, tailnet IPs or enrolment codes in any committed file.
- **Existing behaviour is frozen:** orb, wake word, reply line, news wall, approvals and the test-folder actions behave exactly as at `983c2cf`. No existing test is weakened.
- **CI unchanged:** `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace` on ubuntu-latest and windows-latest; server `ruff check`, `ruff format --check`, `mypy`, `pytest`; `cd app && npm run build`. New: `cd app && npx vitest run`.
- **Contracts are the authority (R12-25):** every Board payload is checked against its schema in tests, on both sides.
- **Action kinds:** `board.make` (server only), `board.show`, `board.update`, `board.set_state`, `board.focus`. All READ_ONLY tier, DESKTOP nodes only. Live actions keep `LIVE_VALIDITY` (2 min).
- **Limits:** title one line of 1 to 120 Unicode code points, no control character, not blank; content 1 to 30,000 bytes of UTF-8 with no control character except tab and line breaks; `revision` and `cost_paise` at most 2,147,483,647; `content_sha256` is lower-case hex of the content's UTF-8 bytes. Server, node and Board all count the same way (Rust `chars().count()`, not `len()`; TypeScript code points, not `.length`).
- **Frame size:** core never issues an action whose serialised frame would exceed 65,536 bytes; it refuses the panel as too large instead (Task 4). Panel kinds: `text`, `canvas`. Panel states: `open`, `minimised`, `maximised`, `closed`.
- **Board job:** output limit 6,000 tokens; web search `max_uses` 3; one Claude call per job.
- **Spending:** every Board call reserves and settles against the Rs 5,000 monthly cap through the existing budget code. A model or tool price missing from `server/config/prices.toml` fails closed.
- **Receipts, not claims:** the receipt for `board.show` and `board.update` is `RENDERED` only after the Board window reports the panel drawn at that revision.
- **Untrusted content:** panel content, search results and Canvas pages are data. Canvas runs with no network, no file access and no access to the app.
- **Nothing is deleted:** closing a panel changes its state. Clearing closed panels is a local action behind a confirmation.
- **Live tests:** scripted, fictional phrases only.

## Review Focus

1. **Bad panel from the server** (over the limit, hash mismatch, unknown kind, malformed args): the node sends `REFUSED` with the check, the Board is unchanged, and Jarvis says it couldn't show it. *Task 5 `bad_show_is_refused_and_not_stored`.*
2. **Board closed or monitor 2 unplugged when a panel arrives:** the window reopens (as a normal window if there is one monitor) and the panel is shown. *Task 6 `placement_falls_back_to_one_monitor`; Task 9 live check.*
3. **The Board job fails part-way** (Claude down, cap reached, call cut): Jarvis says plainly what happened; panels already shown stay; no half-made panel exists. *Task 4 `job_failure_speaks_and_leaves_no_panel`.*
4. **Jarvis names a panel that isn't there** (a wrong reference, or a number that was never used): refused `unknown_panel`, said once, nothing changes. *Task 3 `unknown_panel_reference_is_refused`.*
5. **A hostile Canvas page** (calls the network, reaches for the parent window or the app, navigates away, loops for ever): contained; closing the panel ends it; the rest of the Board keeps working. *Task 8 header tests and the hostile-page checklist.*

---

### Task 1: Contracts

**Files:**
- Create: `contracts/messages/board.show.args.schema.json`, `board.update.args.schema.json`, `board.set_state.args.schema.json`, `board.focus.args.schema.json`, `board.show.receipt.schema.json`, `board.set_state.receipt.schema.json`, `board.state.schema.json`
- Create: `contracts/examples/messages/board.*.valid.json` and `board.*.invalid.json` (one of each per schema, the repo's convention)
- Modify: `contracts/node-envelope.schema.json` (add kind `board.state`), `contracts/README.md`, `server/src/jarvis_server/contracts.py` (the Python models, which the repo requires for every schema), `crates/jarvis-node/src/wire.rs` (`MESSAGE_KINDS`)
- Test: `server/tests/test_message_contracts.py`, `crates/jarvis-node/tests/shared_vectors.rs`

*Changed while building Task 1, to follow the repo's conventions: one receipt schema per action family (as `pc.*.receipt` is), in place of a separate evidence schema; no separate `contracts/board/panel.schema.json`, because `board.show.args` is the panel on the wire.*

**Interfaces (produced, the wire shapes every later task uses):**
- `board.show` args: `{panel_id: uuid, revision: int ≥ 1, kind: "text"|"canvas", title, content, content_sha256, cost_paise: int ≥ 0, created_at: RFC 3339}`; `additionalProperties: false`.
- `board.update` args: the same shape as `board.show`: the whole panel at its new revision, so a node that has lost the panel can restore it. `kind` and `created_at` are the panel's own.
- `cost_paise` is what the panel has cost so far, all revisions together.
- `board.set_state` args: `{panel_id, state}`. `board.focus` args: `{panel_id}`.
- `board.show.receipt` (for show and update): the live form of `action.receipt`, `{reply_id, seq, result, evidence}` with `result` one of `RENDERED`, `REFUSED`, `ERROR` and `evidence` `{panel_id, revision, content_sha256, check}`. `content_sha256` is the hash the node computed. In a `REFUSED` or `ERROR` receipt the three may be null (args that gave no usable value); `RENDERED` always carries them. `check` is omitted when there is none, never null (as in the delete receipt).
- `board.set_state.receipt` (for set_state and focus): `result` `OK` or `REFUSED`, `evidence` `{panel_id, check}`; `panel_id` may be null only in `REFUSED`.
- `board.state` (node to server): `{panel_id, state, revision, at}`. Core applies them in arrival order: the last one wins. Until Task 3 the gateway answers it `unexpected_kind`.
- Python: `contracts.BoardShowArgs`, `BoardUpdateArgs`, `BoardSetStateArgs`, `BoardFocusArgs`, `BoardShowReceipt`, `BoardSetStateReceipt`, `BoardState`, `PanelKind`, `PanelState`, `BOARD_TITLE_MAX = 120`, `BOARD_CONTENT_MAX_BYTES = 30_000`, `board_content_sha256(content) -> str`. The models also enforce the byte limit and that the hash is the content's.

*Changed after the Task 1 independent review: control characters refused in content and title (Postgres cannot store NUL, and they are six bytes each on the wire); the limit lowered to 30,000 bytes; evidence fields nullable in a refusal; `board.update` carries the whole panel; integer upper bounds.*

- [ ] **Step 1: Write the failing tests.** In `test_message_contracts.py`: every new example validates against its schema; the `.invalid.json` example fails; content of 30,001 characters fails; a title of 121 characters fails; `kind: "html"` fails; `board.state` is an accepted envelope kind.
- [ ] **Step 2: Run them.** `cd server && uv run pytest tests/test_message_contracts.py -q`. Expected: FAIL, schema files missing.
- [ ] **Step 3: Write the schemas and examples** to the shapes above. JSON Schema cannot count bytes, so the schemas cap `content` at `maxLength: 30000` characters and Tasks 2 and 5 enforce the byte limit in code; say so in the README.
- [ ] **Step 4: Run.** Expected: PASS, and `git diff --exit-code contracts/signing-vectors` is clean.
- [ ] **Step 5: Commit** on local branch `board-slice-1`: `contracts: Board panel, actions, rendering receipt and panel-state message`. Also add this plan as `.10x/plans/2026-11-26-board-slice-1.md`.

### Task 2: Server panel store

**Files:**
- Create: `server/migrations/versions/0016_board.py`, `server/src/jarvis_server/core/board.py`
- Modify: `server/src/jarvis_server/policy.py` (tiers and node kinds for the five action kinds), `server/src/jarvis_server/smoke.py` and `server/tests/test_acceptance.py` (schema `board` in their schema lists)
- Test: `server/tests/test_board_store.py`, `server/tests/test_policy.py`, `server/tests/test_comm_db.py`, `server/tests/test_retention.py` (`test_0016_down_and_up`)

*Changed while building Task 2: Jarvis's handle for a panel is `p<number>`, a number stored on the panel and stable for its life (handles by list position shift when a panel is added between a prompt and its tool call); `listing` gives every panel that is not closed plus the ten most recently closed, since closed panels are kept for ever; `get` and `Panel.args()` added. After the independent review: a locking read no longer reports a panel as unknown after waiting behind an update; non-text input is a bad panel; a panel's session must be its person's.*

**Interfaces (produced):**
- Tables `board.panel (id, person_id, number, session_id, kind, title, state, revision, rendered_revision, cost_paise, created_at, updated_at, state_changed_at, state_seen_at)` with `UNIQUE (person_id, number)` (`state_seen_at`, added in Task 3: when a state was last set or reported, even one that changed nothing), and `board.panel_revision (panel_id, revision, title, content, content_sha256, created_at)`, primary key `(panel_id, revision)`. `jarvis_app` may SELECT, INSERT and UPDATE `panel`, and SELECT and INSERT `panel_revision`: no DELETE on either, no UPDATE of a revision. `jarvis_gateway` has no access. `conv.inbound.kind` gains `board.state`, queued as job `board.state`. The downgrade refuses once a panel or a `board.state` message exists.
- `board.create(conn, *, person_id, session_id, kind, title, content, cost_paise) -> Panel`. `session_id` may be None; otherwise it must be a session of that person (`ValueError`). It takes a per-person advisory lock until the caller's transaction ends.
- `board.update(conn, *, person_id, panel_id, title, content, cost_paise) -> Panel` (new revision; `cost_paise` is added to the panel's total; raises `UnknownPanel`)
- `board.set_state(conn, *, person_id, panel_id, state) -> Panel` (`ValueError` for an unknown state)
- `board.mark_rendered(conn, *, person_id, panel_id, revision) -> None` (`ValueError` for a revision the panel never had; never steps back)
- `board.get(conn, person_id, panel_id, *, lock=False) -> Panel`
- `board.listing(conn, person_id) -> list[PanelLine]`: every panel that is not closed, plus the `CLOSED_LISTED = 10` most recently closed, newest panel first; each with `handle` (`p<number>`), `panel_id`, `kind`, `title`, `state`.
- `board.resolve(conn, person_id, handle) -> UUID` (raises `UnknownPanel`)
- `board.open_content(conn, person_id, *, budget_chars=CONTENT_BUDGET) -> list[tuple[PanelLine, str]]` (`CONTENT_BUDGET = 12_000`): content of open and maximised panels, newest first, cut to the budget.
- `board.check_content(kind, title, content) -> None` raises `TooLarge` (a `BadPanel`) or `BadPanel`: the contract's limits, and anything that is not text.
- `@dataclass Panel(panel_id, kind, title, state, revision, content, content_sha256, cost_paise, created_at)` with `Panel.args() -> dict` (the `board.show` / `board.update` args). `created_at` is sent with its offset and, when not zero, a fraction of a second (`2026-11-26T19:15:29.743239+05:30`): the node must parse both.
- Policy: ruling R12-39 in the spec records the five kinds.

- [ ] **Step 1: Write the failing tests:** `create_then_listing_is_newest_first_with_stable_handles`; `update_adds_a_revision_and_keeps_the_old_one`; `another_persons_panel_is_unknown`; `content_over_30000_bytes_is_too_large` (a string of 15,001 two-byte characters); `bad_panels_are_refused_and_not_stored`; `set_state_closed_keeps_the_panel`; `listing_keeps_only_the_ten_most_recently_closed`; `mark_rendered_records_the_revision`; `open_content_is_newest_first_within_the_budget`; `a_writer_that_waited_behind_an_update_still_finds_the_panel`; `gateway_role_cannot_reach_panels`; `app_role_cannot_delete_or_rewrite_a_revision`; policy: the five kinds are READ_ONLY and DESKTOP only; `inbound_board_state_is_recorded_and_queued`; `test_0016_down_and_up`.
- [ ] **Step 2: Run.** `uv run pytest tests/test_board_store.py tests/test_policy.py -q`. Expected: FAIL, module missing.
- [ ] **Step 3: Implement the migration and `board.py`.**
- [ ] **Step 4: Run the four files.** Expected: PASS.
- [ ] **Step 5: Commit:** `server: Board panel store (migration 0016) and policy tiers`.

### Task 3: Voice-turn tools, the panel list, receipts and panel state

**Files:**
- Create: `server/src/jarvis_server/core/board_node.py` (receipts for Board actions; job `board.state`)
- Modify: `server/src/jarvis_server/core/prompt.py` (tools, `TOOL_ACTIONS`, system prompt, panel list), `server/src/jarvis_server/core/turn.py` (`_board` handles the three kinds), `server/src/jarvis_server/brain/types.py` (`BrainRequest.board: list[str]`), `server/src/jarvis_server/brain/claude.py` (the list in the prompt, labelled as data), `server/src/jarvis_server/core/actions.py` (a live receipt of a `board.*` action goes to `board_node`), `server/src/jarvis_server/core/board.py` (`report_state`), `server/src/jarvis_server/gateway/session.py` (accept the `board.state` kind from a node)
- Test: `server/tests/test_turn_board.py` (new), `server/tests/test_gateway_handshake.py`

*Changed while building Task 3: the receipt and `board.state` handlers are in a new module, `board_node.py` (in `board.py` they would import `actions`, which imports `board`); at most one Board job per turn; when Jarvis queues Board work and says nothing, core says `BOARD_ACK_TEXT`; reports from the desktop apply in arrival order; a Board receipt that does not match is refused with the existing code `malformed` (no migration). Spec ruling R12-40 records these.*

**Interfaces:**
- Consumes: Task 2 `board.listing`, `board.resolve`, `board.set_state`, `board.mark_rendered`.
- Produces: tools `board_make {request: string ≤ 1000}` → `board.make`; `board_set_state {panel: handle, state: minimise|maximise|restore|close|reopen}` → `board.set_state`; `board_focus {panel: handle}` → `board.focus`.
- `board_make` enqueues job `board.make` with payload `{session_id, person_id, node_id, inbound_id, request, job_key}` and sends nothing to the node. One job per turn (`jobs.enqueue_once`, key = the turn's inbound ID): a second call in the same reply is left out. A request over 1,000 characters is cut; a missing or blank one, or one with a control character, is refused `bad_args`. If Jarvis says nothing itself the reply is `turn.BOARD_ACK_TEXT` ("On it. I'll put it on the board.").
- `board_set_state` and `board_focus` resolve the handle among the authenticated person's panels, and issue the live action through `stream.action(kind=…, args={panel_id, state})` (`{panel_id}` for focus). For `board_set_state` the store is updated in the same transaction. Verb to state (`prompt.BOARD_STATES`): minimise → `minimised`, maximise → `maximised`, restore and reopen → `open`, close → `closed`. `board_focus` changes nothing in the store.
- Refusals: `policy.refused` with reason `unknown_panel` (said once: `turn.NO_PANEL_TEXT`) or `bad_args` (`turn.BOARD_UNCLEAR_TEXT`). A PHONE node is refused by the policy (`node_kind_not_allowed`).
- Prompt: `prompt.board_lines(conn, person_id) -> list[str]`, one line per panel, `p3 text "December trip: three options" open` (the handle is the panel's own stable number; the title is quoted as JSON), in `BrainRequest.board`, shown under `<board_panels>` with a notice that titles are data. A title is cut to 60 characters in the list. At most `BOARD_LINES_MAX = 30` lines, newest first, then `(and N older panels not listed)`. The system prompt tells Jarvis to use `board_make` for anything to be shown, looked up, compared, drawn or written at length, and to say one short sentence that it is going on the board.
- Receipts (`board_node.on_live_receipt`, after `actions`' own checks of signature, target node, person and lateness): for `board.show` or `board.update`, `RENDERED` calls `board.mark_rendered` only if the evidence names the panel, revision and hash that chunk issued (compared as values), and records event `board.rendered`. `REFUSED` or `ERROR` records `board.render_failed` (details: `panel_id`, `result`, `check`) and says once, in the session, `BOARD_NOT_SHOWN_TEXT` ("I couldn't show that on the board."). For `board.set_state` or `board.focus`: `OK` is taken quietly; `REFUSED` records `board.state_refused` and says `BOARD_NOT_DONE_TEXT`. Any other shape, a mismatch, or `ValueError` / `UnknownPanel` from `mark_rendered` is refused `malformed` and audited, so a faulty receipt never fails the job into a retry. A receipt sent twice is said once, and a failure reported for a chunk already reported drawn is recorded but not said. A `seq` that is not a whole number is refused `unknown_action` (for every live receipt). Board receipts leave no `action.readonly_ok` item.
- Panel state from the desktop: job `board.state` (from an inbound `board.state` message) checks the node's signature and the contract's shape, then calls `board.report_state(conn, *, person_id, panel_id, state, reported_at) -> bool` with the message's `received_at`. Only a DESKTOP node's report is taken. Reports apply in arrival order whatever order their jobs run in; one that arrived before the newest word on the panel's state (`state_seen_at`: a voice command or a later report, even one that changed nothing) is left out. `board.set_state` dates its change with the clock, not the transaction's start. An unknown panel or another person's is dropped and logged (outcome `REFUSED`). A panel that is only locked by a running Board job is not unknown: the job waits for it.
- *After the independent review:* the newest-word marker (`state_seen_at`); DESKTOP-only reports; no failure spoken after a drawn report; the `seq` check; control characters in a request; titles cut in the list.
- Not in this task: if the desktop refuses a `board.set_state` (it has lost the panel), the server's record keeps the new state. Putting the two back in step is the resync that P-5 leaves for later.

- [ ] **Step 1: Write the failing tests** (fake brain, as `test_turn.py` does): `tool_table_has_the_three_board_tools`; `board_make_queues_one_job_and_no_action`; `board_make_alone_is_acknowledged`; `a_turn_queues_at_most_one_board_job`; `board_make_without_a_request_is_refused`; `set_state_issues_a_signed_action_with_the_panel_id` (each verb); `focus_issues_an_action_and_changes_nothing`; `unknown_panel_reference_is_refused` (event `policy.refused` reason `unknown_panel`, said once, store unchanged); `an_unknown_state_word_is_refused`; `phone_node_is_refused_board_tools`; `panel_list_is_in_the_request_newest_first`; `panel_list_is_capped_at_thirty_lines`; `rendered_receipt_marks_the_revision`; `rendered_receipt_for_another_panel_marks_nothing`; `refused_render_is_said_once_and_not_marked`; `a_late_rendered_receipt_marks_nothing`; `state_receipts`; `board_state_from_the_node_updates_the_panel`; `board_state_for_another_persons_panel_is_dropped`; `reported_states_apply_in_arrival_order_whatever_order_the_jobs_run`; `forged_or_malformed_board_state_is_refused`; gateway: `board_state_from_a_node_is_recorded_and_queued`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full server suite, lint and types. Expected: PASS, existing turn tests unchanged.
- [ ] **Step 5: Commit:** `server: Board tools in the voice turn, panel list, rendering receipts and panel state`.

### Task 4: The Board job (Claude with web search, panels out, headline spoken)

**Files:**
- Create: `server/src/jarvis_server/core/board_job.py` (jobs `board.make` and `board.headline`)
- Modify: `server/src/jarvis_server/brain/types.py` (`BrainRequest.server_tools`; `Brain.searches`, `sources`, `truncated`), `server/src/jarvis_server/brain/claude.py` (send server tools; skip `server_tool_use` and `web_search_tool_result` blocks; read `usage.server_tool_use.web_search_requests`; `pause_turn` ends the call; a tool call cut by the output limit), `server/src/jarvis_server/brain/fake.py`, `server/config/prices.toml` and `server/src/jarvis_server/brain/prices.py` (price per search), `server/src/jarvis_server/core/board_node.py` (`release_headline`), `server/src/jarvis_server/core/turn.py` (`provider()`, `memory()`), `server/src/jarvis_server/core_main.py` (registers the jobs)
- Test: `server/tests/test_board_job.py` (new), `server/tests/test_brain_claude.py`, `server/tests/test_budget.py`

*Changed while building Task 4 (spec ruling R12-41):*
- *The headline is its own job, `board.headline`, spoken only after the desktop reports a panel drawn. The plan had it as the last chunk of the job's reply; the receipts are handled by the same job loop, so the job cannot wait for them, and saying "it's on the board" before it is would be a claim, not a receipt.*
- *The headline comes from a `headline` field of each tool call. With one call per job, Claude cannot speak after a tool call.*
- *`turn.py` was not refactored: the job has its own, simpler reserve, call and settle (no holds), and the voice turn is untouched.*
- *The job's cost is put on the first panel it stores, not the last.*
- *Added: a plain-text answer with no tool call becomes a text panel; a tool call cut by the output limit no longer loses the whole call's cost as "indeterminate"; pages searched are listed when a text panel links none of them.*

**API facts confirmed on 26 Nov 2026** (platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool):
- Tool types: `web_search_20250305` (basic), `web_search_20260209` (dynamic filtering, run from code execution), `web_search_20260318` (adds response inclusion). The job uses the basic one.
- `max_uses` limits searches in a turn; failed searches count; over the limit gives a `web_search_tool_result_error` with `max_uses_exceeded` inside a 200 response.
- `usage.server_tool_use.web_search_requests` is the number billed. $10 per 1,000 searches; a failed search is not billed. Result pages are billed as input tokens.
- `stop_reason: "pause_turn"`: a long turn the API paused; resuming needs a second request.
- Citations: when API output is shown directly to end users, citations to the original source must be included.
- Web search is on for an organisation unless an administrator turned it off in the Console; if off, the request fails with a 400.
- Artifacts are a feature of Claude's apps, not of the Messages API. Claude Code's artifact page rules (code.claude.com/docs/en/artifacts, "Page constraints") are the model for `BOARD_PROMPT`'s canvas rules: one self-contained page, inline CSS and JavaScript, images as data URIs, no backend, no downloads, in-page anchors, and SVG or HTML and CSS preferred to embedded images.

**Interfaces:**
- Consumes: Task 2 store; Task 3 job payload (`{session_id, person_id, node_id, inbound_id, request, job_key}`; one job per turn); `prompt.board_lines`; `actions.issue_live`. Task 3's receipt handler marks a panel drawn from the args this job issues, so they are exactly `Panel.args()`.
- Produces: `@dispatch.register("board.make") run(conn, job)` and `@dispatch.register("board.headline") say_headline(conn, job)` (payload `{session_id, reply_id, text, job_key = reply_id}`). Constants `BOARD_MAX_TOKENS = 6000`, `SEARCH_MAX_USES = 3`, `SEARCH_TOOL`, `SEARCH_INPUT_ALLOWANCE = 20_000`, `HEADLINE_WAIT = 20 s`, `BOARD_PROMPT`.
- Tools given to Claude in the job: `board_show {kind, title, content, headline}` and `board_update {panel: handle, title, content, headline}`, plus the web search tool.
- Runs only if the turn ended `ANSWERED` and the session and DESKTOP node are the person's; a re-run job that already holds a reservation never calls again and says `BOARD_FAILED_TEXT`.
- The request: the person's own words as the utterance, the session's history, the passed-on request, the panel list, and the content of the panels on show (marked where cut), each labelled as data.
- Money: `prices.tool_paise(tool, uses) -> int` (`UnknownToolPrice` fails closed). Reservation: input + 20,000 tokens for each of 3 searches + 6,000 output + 3 searches. Settled: real usage + searches reported.
- For each tool call, after the call: `board.check_content` → store → args checked against `BoardShowArgs` and the frame limit (args + 3,500 bytes ≤ 65,536) → one live ACTION chunk, all in one transaction, in one reply whose `in_reply_to` is null. A bad panel is not stored and is said once; the others are still shown.
- `board_node.release_headline(conn, reply_id)`, called on every Board rendering receipt, makes the headline job ready once no ACTION chunk of the reply is unanswered.
- Wording (constants): `BOARD_CAP_TEXT`, `BOARD_DOWN_TEXT`, `BOARD_TOO_LARGE_TEXT`, `BOARD_FAILED_TEXT`, `BOARD_SHOWN_TEXT`; an unknown panel uses `turn.NO_PANEL_TEXT`.
- `BOARD_PROMPT` requires: a headline in every call; text for reading, canvas for drawing; the sealed-page rules; economy within 30,000 bytes; a Sources section after a search; `board_update` only for a panel shown in full; all supplied material is data.
- Not in this task: no hold or retry when Claude is down or the cap is reached (it is said once); `pause_turn` is not resumed; a Board job is never offered `web_fetch`.

- [ ] **Step 1: Confirm the API facts** (above) and put the price in `prices.toml` with its source and date.
- [ ] **Step 2: Write the failing tests:** `text_panel_is_stored_and_issued_and_the_headline_waits_for_the_receipt`; `the_headline_is_not_spoken_when_the_panel_was_not_drawn`; `no_receipt_at_all_and_nothing_is_claimed`; `two_panels_one_headline_after_both_receipts`; `update_targets_the_handle_and_bumps_the_revision`; `a_plain_answer_without_a_tool_call_becomes_a_text_panel`; `the_request_carries_the_board_the_words_and_the_search_tool`; `a_cut_panel_is_marked_as_cut`; `cost_includes_searches_and_the_reservation_allows_for_them`; `cap_reached_is_said_and_nothing_is_called`; `a_search_without_a_price_fails_closed`; `job_failure_speaks_and_leaves_no_panel`; `claude_unreachable_is_said_and_the_money_given_back`; `claude_down_is_said_and_nothing_is_called`; `too_large_content_is_not_stored_and_is_said`; `an_answer_cut_by_the_output_limit_is_said_as_too_large`; `a_bad_panel_is_not_stored_and_is_said`; `the_widest_content_fits_a_frame_and_one_that_would_not_is_refused`; `pages_searched_are_listed_when_the_panel_names_none`; `the_job_runs_only_for_an_answered_turn`; `a_job_for_someone_elses_session_or_node_is_dropped`; `a_rerun_job_never_calls_claude_twice`; parser: `server_tools_are_sent_after_the_client_tools`, `parser_skips_server_tool_blocks_and_counts_searches`, `a_tool_call_cut_by_max_tokens_is_a_completed_call`, `pause_turn_ends_the_call`; prices: `web_search_has_a_price_and_a_missing_one_fails_closed`.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** The egress proxy needs no change (the search runs at Anthropic).
- [ ] **Step 5: Run the full server suite and the lint and type checks.** Expected: PASS.
- [ ] **Step 6: Commit:** `server: Board job with web search, budget and spoken headline`.

### Task 5: Desktop node: Board actions, local store, receipts

**Files:**
- Create: `crates/jarvis-node/src/board.rs`
- Modify: `crates/jarvis-node/src/node.rs` (route `board.*`; `NodeHandle::board_rendered`, `NodeHandle::board_state`; `UiEvent::Board`), `crates/jarvis-node/src/server_brain.rs` (skip `board.*`, as `pc.*` is skipped), `crates/jarvis-node/src/bin/jarvis-node-probe.rs` (print Board events; `--render-next`, `--refuse-render`)
- Test: `crates/jarvis-node/tests/board.rs`

**Interfaces:**
- Consumes: `check_node_action`; Task 1 schemas through `tests/support::assert_conforms`.
- Produces:
  - `enum PanelKind { Text, Canvas }`, `enum PanelState { Open, Minimised, Maximised, Closed }`
  - `struct Panel { panel_id: Uuid, kind, title, content, content_sha256, revision: u32, state, cost_paise: u64, created_at }`
  - `enum BoardEvent { Show(Panel), Update(Panel), State { panel_id, state }, Focus { panel_id } }`
  - `board::parse(action_kind: &str, args: &Value) -> Result<BoardEvent, &'static str>`: checks `too_large`, `hash_mismatch`, `unknown_kind`, `malformed`.
  - `BoardStore::open(dir: &Path)`, `apply(&mut self, &BoardEvent) -> Result<(), &'static str>` (`stale_revision`; an `Update` for a panel the store does not hold adds it; `unknown_panel` for a state or focus of one it does not hold), `panels()`, `clear_closed() -> usize`, `append_turn(ConversationEntry)`, `conversation() -> Vec<ConversationEntry>`. Files `board.json` (written like `state.rs`: temp, sync, rename) and `conversation.jsonl` (append; the last 2,000 entries are loaded).
  - `struct ConversationEntry { at, who: Person|Jarvis, text, how: Voice|Typed|Spoken, panels: Vec<Uuid> }`
  - `NodeHandle::board_rendered(panel_id, revision, ok: bool)` sends the pending receipt; `NodeHandle::board_state(panel_id, state)` updates the store and sends `board.state`.
- From the Task 3 review: `server_brain` must stay silent for `board.*` (today it answers `REFUSED` `unknown_action_kind`, which core refuses as malformed, and a chunk would get two receipts). The node defines what a state word does to a panel in another state (minimise on a closed panel reopens it minimised) and what focus does to a closed or minimised panel; core passes both through.
- Routing: a `board.*` ACTION chunk for this node, of any reply, is checked, parsed and applied. Show and update then wait up to 10 s for `board_rendered`; none gives `ERROR` with check `render_timeout`. Set_state and focus are answered `OK` at once. A failed check or parse gives `REFUSED` with that check and changes nothing.

- [ ] **Step 1: Write the failing tests:** `show_is_stored_and_emitted_then_rendered_receipt_conforms`; `bad_show_is_refused_and_not_stored` (over the limit; hash mismatch; kind `html`; each its own check); `update_with_an_older_revision_is_stale`; `set_state_for_an_unknown_panel_is_refused`; `update_for_a_lost_panel_restores_it`; `title_length_is_counted_in_code_points`; `refusal_of_malformed_args_has_null_evidence_and_conforms`; `no_render_report_gives_render_timeout` (paused time); `store_survives_reopen_and_a_torn_write`; `click_state_change_sends_board_state_that_conforms`; `server_brain_ignores_board_actions`; `conversation_keeps_order_and_panel_links`.
- [ ] **Step 2: Run** `cargo test -p jarvis-node --test board`. Expected: FAIL to compile.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings`. Expected: PASS.
- [ ] **Step 5: Commit:** `jarvis-node: Board actions, local panel store and rendering receipts`.

### Task 6: Board window and panel shell

**Files:**
- Create: `app/src-tauri/src/board.rs`, `app/src-tauri/capabilities/board.json`, `app/board.html`, `app/src/board/main.ts`, `app/src/board/layout.ts`, `app/src/board/panels.ts`, `app/src/board/board.css`, `app/src/board/layout.test.ts`
- Modify: `app/src-tauri/src/lib.rs` (register commands; forward `UiEvent::Board`), `app/vite.config.ts` (second page), `app/package.json` (`vitest`)

**Interfaces:**
- Consumes: Task 5 `BoardStore`, `BoardEvent`, `NodeHandle::board_rendered`, `NodeHandle::board_state`.
- Produces (Rust): `fn pick_placement(monitors: &[MonitorInfo], main: usize) -> Placement` with `enum Placement { Borderless { monitor: usize }, Normal }`; `board::ensure_open(&AppHandle)`; commands `board_snapshot() -> BoardSnapshot { panels, conversation, spend_today_paise }`, `board_set_state(panel_id, state)`, `board_rendered(panel_id, revision, ok)`, `board_clear_closed() -> usize`. Event `jarvis://board` carries a `BoardEvent`.
- Produces (TypeScript): `tile(panels: {id, state}[], width, height) -> Rect[]` (one maximised panel takes everything; otherwise open panels in a grid of at most 2 columns, newest first; minimised and closed panels get no rectangle); `renderPanel(panel, frame)` with the shared header: title, kind label, and three real buttons labelled "Minimise panel", "Maximise panel" / "Restore panel", "Close panel"; the dock; the Recently closed list with Reopen; a "Clear closed panels" button behind a confirmation.
- Closing the Board window hides it; the app, voice and main window carry on. A `Show` event calls `ensure_open` first.
- The top bar's spend is the sum of `cost_paise` of panels created today (IST).

- [ ] **Step 1: Write the failing tests.** Rust, in `board.rs`: `placement_prefers_the_other_monitor`; `placement_falls_back_to_one_monitor`; `spend_today_sums_todays_panels_only`. Vitest: `tile_puts_two_open_panels_side_by_side`; `tile_gives_a_maximised_panel_everything`; `tile_skips_minimised_and_closed`; `header_has_three_labelled_buttons`.
- [ ] **Step 2: Run** `cargo test -p jarvis-app board` and `cd app && npx vitest run`. Expected: FAIL.
- [ ] **Step 3: Implement,** following the mock's layout: top bar, conversation column 360 px on the left (filled in Task 9), panel area, dock. A panel body is an empty frame until Tasks 7 and 8.
- [ ] **Step 4: Run** both test commands and `npm run build`. Expected: PASS and a clean build. A screenshot of the empty Board from a Windows run goes in the task report.
- [ ] **Step 5: Commit:** `desktop: Board window, tiling and panel controls`.

### Task 7: Text frame

**Files:**
- Create: `app/src/board/text.ts`, `app/src/board/text.test.ts`
- Modify: `app/package.json` (`markdown-it`, `highlight.js`), `app/src-tauri/Cargo.toml` and `lib.rs` (`tauri-plugin-opener`), `app/src-tauri/capabilities/board.json`

**Interfaces:**
- Consumes: Task 6 `renderPanel`.
- Produces: `renderText(content: string) -> string` (safe HTML): `markdown-it` with `html: false`, tables on, code blocks through `highlight.js`. Links are shown with their address; a click opens the system browser only for `https:`; any other scheme is plain text. After the frame is in the page, the Board calls `board_rendered(panel_id, revision, true)`; a render error calls it with `false` and shows "This panel couldn't be shown".

- [ ] **Step 1: Write the failing tests:** `raw_html_in_content_is_shown_as_text` (`<script>` and `<img onerror>` never become elements); `tables_and_code_blocks_render`; `only_https_links_are_clickable` (`javascript:`, `file:` and `http:` are not); `render_failure_reports_false`.
- [ ] **Step 2: Run** `npx vitest run`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** vitest and `npm run build`. Expected: PASS.
- [ ] **Step 5: Commit:** `desktop: Text frame`.

### Task 8: Sealed Canvas frame

**Files:**
- Create: `app/src-tauri/src/canvas.rs`, `app/src/board/canvas.ts`, `app/src/board/canvas.test.ts`, `.10x/reviews/2026-11-26-board-canvas-hostile-page.md` (the checklist and the test page's source)
- Modify: `app/src-tauri/src/lib.rs` (register the scheme), `app/src-tauri/tauri.conf.json` (`frame-src` gains the canvas origin only)

**Interfaces:**
- Consumes: Task 5 `BoardStore`.
- Produces: URI scheme `jarvis-canvas`, path `/<panel_id>/<revision>`, answered by `canvas::respond(store, path) -> Response`: the stored content for a canvas panel at exactly that revision, otherwise 404. Response headers, exactly:
  - `Content-Type: text/html; charset=utf-8`
  - `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; sandbox allow-scripts`
  - `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`
- `canvasFrame(panel) -> HTMLIFrameElement` with `sandbox="allow-scripts"` and nothing else, `referrerpolicy="no-referrer"`, no `allow` attribute. The header and its three buttons are outside the frame. Closing or minimising a Canvas panel removes its frame from the page. Rendered is reported on the frame's `load`; no `load` within 5 s reports `false`.
- No Tauri capability names the canvas origin, so the page has no app interface.

- [ ] **Step 1: Write the failing tests.** Rust: `response_has_the_exact_policy_headers`; `text_panels_and_unknown_revisions_are_404`. Vitest: `frame_sandbox_is_allow_scripts_only`; `closing_removes_the_frame`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Rust and vitest tests. Expected: PASS.
- [ ] **Step 5: Hostile-page checklist on Windows** (CI artefact build on Manj's PC, with his go-ahead; results in the task report). A fictional page that tries each of these, and each must fail: a `fetch` and an image to an outside address; reading `window.parent.document` and `window.top.location`; `window.__TAURI__` and `window.__TAURI_INTERNALS__`; `window.open`; a form post; `localStorage`. And an endless loop: the Board's other panels and the conversation column stay responsive, and Close ends it. If the loop freezes the Board, stop and report under the operating rule (the fallback is a separate webview per Canvas panel).
- [ ] **Step 6: Commit:** `desktop: sealed Canvas frame on its own origin`.

### Task 9: Conversation frame, end-to-end test, live check

**Files:**
- Create: `app/src/board/conversation.ts`, `app/src/board/conversation.test.ts`, `server/tests/test_rust_node_board.py`
- Modify: `app/src-tauri/src/lib.rs` and `board.rs` (record heard, typed and spoken lines with `append_turn`; command `board_say(text)` calling the same code as the existing `say` command), `README.md` ("The Board"), `.10x/handoff.md`, `.10x/status.md`

**Interfaces:**
- Consumes: Task 5 `ConversationEntry`; Task 6 `BoardSnapshot`.
- Produces: `conversationModel(entries, panels)`: entries in order; each Jarvis entry lists the panels made in that reply, shown as buttons that focus the panel; "Jarvis is working…" shows from a `board_make` acknowledgement until the next reply that answers no utterance (the headline, or a failure sentence) or 3 minutes. A labelled input, "Type instead of speaking".

- [ ] **Step 1: Write the failing tests.** Vitest: `entries_keep_order_and_mark_voice_or_typed`; `panel_markers_focus_their_panel`; `working_line_ends_on_the_headline`. In `test_rust_node_board.py` (skipped unless `JARVIS_NODE_PROBE` is set; the fake brain and the gateway fixture of `test_rust_node.py`): `test_request_becomes_a_rendered_panel` (say a fictional request → acknowledgement → job → `board.show` at the probe → `--render-next` → the server's `rendered_revision` equals the revision, then the headline arrives as its own reply, from job `board.headline`); `test_refused_render_is_not_marked_rendered`; `test_voice_minimise_reaches_the_node_and_the_store`; `test_click_close_reaches_the_server`; `test_restart_restores_panels_and_conversation`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run everything:** the full server suite with the probe built, `cargo test --workspace`, clippy, `npx vitest run`, `npm run build`. Expected: PASS. Existing tests for the orb, reply line, news wall and approvals are unchanged and green.
- [ ] **Step 5: Commit:** `desktop: Conversation frame; Board end-to-end tests`.
- [ ] **Step 6: Live check on Manj's PC, by hand, each server step with his approval.** Order: install the desktop build first (it understands `board.*`), then deploy the server (migration 0016).
  1. With two monitors, the Board opens on monitor 2; with one, as a normal window. Closing it leaves JARVIS running.
  2. A fictional request for a comparison: acknowledgement within 3 s, a Text panel with sources, then the headline. The server shows the panel as rendered.
  3. A fictional request for a chart: a Canvas panel.
  4. By voice and by click: minimise, maximise, restore, close, bring back. After a click, the next request shows Jarvis knows.
  5. "Add a column to the comparison": the panel updates in place.
  6. Type a request in the Conversation column.
  7. Restart the app: panels and conversation are back.
  8. Orb, "Hey Jarvis", the reply line, the news wall and a test-folder delete with approval all work as before.
  9. Record the cost of steps 2, 3 and 5 from the Board's top bar, for Manj.

---

## Acceptance map (the brief's slice 1 list)

| Brief | Where |
|---|---|
| 1 Opens on monitor 2 or as a normal window; closing keeps JARVIS | Task 6 placement tests; Task 9 live 1 |
| 2 Spoken headline and a Text panel; rendered is recorded | Tasks 4, 5, 7; Task 9 `test_request_becomes_a_rendered_panel` (P-1, P-2 change the timing) |
| 3 Canvas panel that cannot reach network, files or app | Task 8 |
| 4 Conversation shows both sides, survives a restart, typed request works | Tasks 5 and 9 |
| 5 Tiled, and back after a restart in their last state | Tasks 5 and 6; `test_restart_restores_panels_and_conversation` |
| 6 Minimise, maximise, close by click and by voice; Jarvis knows after a click | Tasks 3, 5, 6; Task 9 |
| 7 Jarvis can read a panel and change it | Tasks 2 and 4 (P-4) |
| 8 Existing behaviour unchanged | Global Constraints; Task 9 step 4 and live 8 |
| 9 Sources shown; spend recorded and shown | Task 4; Task 6 top bar (P-7) |

## Not in this plan
Notes (slice 2); streaming panels; content fetched by ID; server-to-desktop resync; Media, Cards, Document and Drawing frames; moving the news wall; a charting library; clicks inside a Canvas reaching Jarvis.
