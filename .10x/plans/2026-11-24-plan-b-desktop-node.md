# Plan B (the Rust desktop node) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Windows desktop app into a Phase 2 node. It talks to the Jarvis server through the gateway instead of to Claude directly. It shows approval cards, and it runs the two test-folder actions safely, with deletes going to the Recycle Bin.

**Architecture:** A new crate, `crates/jarvis-node`, holds everything that isn't UI. Most of it is platform-neutral and tested on Linux: the signing rules, the handshake as a sans-IO state machine, the WebSocket driver, the durable journal, the outbox inbox, `ServerBrain`, the approval-card model, and the delete executor. Two Windows-only parts sit behind traits, `KeyStore` (DPAPI) and `FolderOps` (the §9.4 file handling), and are tested on the `windows-latest` runner and on Manj's PC. The Tauri app picks `ClaudeBrain` or `ServerBrain` from a local `node.toml`, and only adds an enrolment screen, an approval card and a notice strip. Panels, voice and modules are unchanged.

**Tech Stack:** Rust (workspace edition 2021), tokio, `tokio-tungstenite` (plain `ws://` over the tailnet), `serde_json_canonicalizer` (R12-35), `ed25519-dalek` 2, `sha2`, `data-encoding` (base32), `base64` 0.22, `uuid`, `chrono`, the `windows` crate (Win32 file, security, COM shell and DPAPI APIs), `jsonschema` (dev only); Tauri 2 and TypeScript for the UI; for Task 1 and the end-to-end test, Python 3.12 with uv and pytest, and Node 22 for the vector generator.

**Spec:** `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md`, Revision 12. The scope is §13. The rules come from §6.1, §7, §8.2–8.3, §9.3–9.4 and §14, and from rulings R12-6, R12-10, R12-25 to R12-29 and R12-34. Read the spec alongside this plan. The Python reference node is `server/tests/fakes/{fake_node.py,node_rules.py,live_node.py}`; where this plan says "as FakeNode does", that file is the behaviour to match.

**Out of this plan:** `jarvis-core person add` and the deploy-guide fixes (a separate small server PR, from the deploy handover's follow-ups); Track D11; the deferred security lows S-3 to S-8, S-10 and S-5.

## Operating rule

As in the Phase 2 plan, shared interfaces belong to the spec and the contracts. If the code shows that a contract, case or ruling is wrong or missing something, stop, write the mismatch and a proposed change in the task report, and wait for Manj's decision. One task at a time. An independent review and its fixes come after each task, and then work stops for Manj. Work stays local: no push, merge, PR or deploy without Manj's separate approval.

## Global Constraints

- **The repo is public:** no secrets, gateway address, tailnet IPs, server IDs or enrolment codes in any committed file. `node.toml` and everything the node stores live in the app's local data folder, never in the repo.
- **Rust CI is unchanged:** `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets -- -D warnings` and `cargo test --workspace` must pass on `ubuntu-latest` and `windows-latest`.
- **Signed bytes:** `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`. The purposes are `node-msg`, `gw-transport` and `core-authz`. JCS is `serde_json_canonicalizer` (R12-35, replacing `serde_jcs`); object keys sort by UTF-16 code units; Ed25519 signatures are base64url without padding.
- **Fingerprint:** the first 10 bytes of SHA-256 of the raw 32-byte public key, as lower-case base32 without padding (16 characters).
- **Envelope `key_id` (R12-6):** the node signs `node-msg` under `fingerprint(own public key)`. Gateway envelopes are verified under the `key_id` its `challenge` announced.
- **Timestamps:** RFC 3339 with an explicit offset. One without an offset is malformed. Clock tolerance is 30 s.
- **`seq`:** per sender and per connection, starting at 1 and going up by exactly 1. A gap, repeat or decrease from the peer closes the connection without a reply.
- **Contracts are the authority (R12-25):** every payload the node sends is checked against `contracts/messages/<kind>.schema.json` in tests. `contracts/signing-vectors/*.json` are read-only to Rust and are never generated or rewritten by it.
- **Trust:** CORE and GATEWAY pins are separate maps. A key of one role is never accepted for the other (R6-3), and a `keyset` with `role: gateway` never enters core trust.
- **Close codes (R12-29):** reconnect after any close except 4409, with backoff of 1, 2, 4, 8, 16 and then 30 s. Re-enrol only on 4409, or on a gateway or core signature under a `key_id` the node never pinned or no longer trusts.
- **Heartbeat:** answer every `ping` with `pong`. 3 missed 30 s heartbeats (no frame for 95 s) means the link is down, so close and reconnect. The reader never waits on app work (R12-10: the server times sends out at 10 s).
- **Keyset overlap:** 24 h from the signed `issued_at`. Ack the keyset's `outbox_id` only after it verifies and the new trust is saved (R12-26).
- **Deletes:** only to the Recycle Bin, via `IFileOperation` with `FOFX_RECYCLEONDELETE`, never permanent. Names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`, and Windows device names (CON, PRN, AUX, NUL, COM1–9, LPT1–9, with or without an extension) are refused.
- **Irreversible actions:** journal STARTED is written and fsync'd before step 1 of §9.4. A key already in the journal is never executed again; its stored receipt is re-sent.
- **Windows security behaviour** (DPAPI, reparse points, junctions, DACLs, the Recycle Bin) is proven by tests that run on `windows-latest` and by the live P2-A1 check on Manj's PC. Linux runs only prove the platform-neutral logic.
- **Live tests:** scripted, fictional phrases and a throwaway test folder only.
- **Latency:** first spoken sentence within 3 s of the end of speech (§6.2).

## Review Focus

1. **Server unreachable** (tailnet down, gateway restarting, PC just woken): the app starts, local commands work, and a spoken request is answered "I can't reach the Jarvis server" within 1 s, not after a timeout. *Test: Task 6 `not_connected_fails_fast`.*
2. **Power cut mid-write:** a journal whose last line is torn is repaired by dropping that line. A corrupt line in the middle fails closed: the node runs no action until it is fixed. *Test: Task 3 `torn_final_line_is_dropped` and `corrupt_middle_line_fails_closed`.*
3. **Odd files in the test folder** (spaces, non-ASCII names, `.hidden`, a subfolder, a symlink): the listing leaves them out and still succeeds. One odd file never fails the whole listing. *Test: Task 8 `listing_omits_invalid_names_and_non_regular_files`, and the Windows `listing_skips_links_and_folders`.*
4. **Two approval cards at once, or "approve" said with none showing:** voice approval applies only when exactly one card is pending. With two, Jarvis asks Manj to tap the one he means; with none, "approve" goes to the brain as ordinary speech. *Test: Task 7 `voice_decision_needs_exactly_one_card`.*
5. **The test folder isn't configured, is missing, is on a network or FAT drive, or is itself a link:** every action gets the receipt REFUSED with a check code, and the app keeps running. *Test: Task 8 `no_folder_configured_refuses`, and the Windows `refuses_folder_that_is_a_junction` and `refuses_non_ntfs_or_missing_folder`.*

## Decisions (approved by Manj, 24 Nov 2026)

- **D-1 (Task 8): after a crash with STARTED in the journal and the file still in place (never moved), send REFUSED with check `interrupted_before_staging` and leave the file alone.** An irreversible action is never retried after a restart without a new approval. The server marks the task FAILED, and Manj can ask again.
- **D-2 (Task 6): `result: "OK"` on a read-only module receipt means the node verified the action and handed it to the coordinator. It does not mean the panel rendered.** The code comment on the receipt and its log line (`readonly_action handed_to_coordinator`) both say so. Panel failures are spoken locally, as today.
- **D-3 (Task 9): the end-to-end test is `server/tests/test_rust_node.py`, driving `jarvis-node-probe` against the real Python gateway and core.** The probe is a thin shell over the real `jarvis-node` runtime: the same `node::start`, session, journal, inbox, approval and executor code the app uses. Only the UI (JSON lines on stdout) and the `FolderOps` (the memory folder) are replaced. It never has a protocol path of its own.
- **Recovery table (Task 8, Manj's review).** The journal stage and what `locate` finds give exactly one outcome. Every outcome except Done is journalled as Done with its receipt before the receipt is sent, so a second `recover()` re-sends it and nothing more.

  | Journal | `locate` | Outcome |
  |---|---|---|
  | Done | any | Re-send the stored receipt; touch nothing |
  | Started | InFolder | REFUSED `interrupted_before_staging` (D-1); the file is left alone |
  | Started | Staged | The crash came after the move and before STAGED was journalled. First journal Staged. Then run step 4 in full with every §9.4 check: the staging folder isn't a reparse point and its DACL is unchanged; the file's ID, size and SHA-256, read through a handle opened with `FILE_SHARE_DELETE` only, equal the approved args; its final path is `<staging>\<execution_key>\<name>`. Any failure → REFUSED with that check, and the file stays in staging. This continues the same approved execution, the same `execution_key` under the approval that issued it, and issues nothing new (§9.4 "On restart") |
  | Staged | Staged | The same step 4, with the same checks |
  | Staged | InRecycleBin | RECYCLED. The crash came after the recycle and before Done |
  | Started | InRecycleBin | ERROR `outcome_unknown`. This node never recycles before Staged is fsync'd, so something else moved it. Don't report success |
  | Started or Staged | Unknown, or InFolder after Staged | ERROR `outcome_unknown` |

---

### Task 1: S-9, shared keyset target and role cases (server side)

**Files:**
- Modify: `contracts/signing-vectors/generate.mjs` (`keysetCase` and the case list)
- Regenerate: `contracts/signing-vectors/authz-cases.json`. `vectors.json` must come out byte-identical.
- Modify: `contracts/signing-vectors/README.md` (the new case fields)
- Modify: `server/tests/fakes/node_rules.py`: `check_keyset` gains `me`; new `apply_keyset`
- Modify: `server/tests/fakes/fake_node.py`: `_keyset` uses `check_keyset(me=…)` and `apply_keyset`
- Modify: `server/tests/test_node_rules.py` (`_run`, `REQUIRED`), plus any other caller of `check_keyset` (`tests/test_key_rotation.py`)
- Modify: `.10x/status.md` and `.10x/handoff.md`: the Phase 2 deploy state (from `claude/phase-2-deploy-handover.md`, with no addresses) and "Plan B in progress"

**Interfaces:**
- Produces, as the Python reference that Rust matches in Task 2:
  - `check_keyset(payload, authz, *, trusted, me: UUID, now) -> dict`. Reasons, in order: `malformed`, `untrusted_key_id`, `bad_signature`, `wrong_kind`, `wrong_target` (when `target_node_id` is neither null nor `me`), `expired`, `keyset_mismatch`.
  - `apply_keyset(obj, *, trusted_core, pinned_gateway, key_expiry) -> Literal["core", "gateway"]`: the logic moved unchanged from `FakeNode._keyset`, where `args.role` picks the pin set.
- Every `keyset` case gains `receiving_node_id`. Every accepted `keyset` case gains `pins_as` (`"core"` or `"gateway"`).

- [ ] **Step 1: Add the cases in `generate.mjs`.** `keysetCase` takes `args` (default `NEW_CORE_KEY`), `receiving_node_id` (default `ME`) and `pins_as`. Add:
  - `reject-keyset-other-target`: `target_node_id: OTHER_NODE` → reject, `wrong_target`;
  - `accept-keyset-for-this-node`: `target_node_id: ME` → accept, `pins_as: "core"`;
  - `accept-keyset-gateway-role`: args `{role: "gateway", key_id: <gateway test key label>, public_key: …}` → accept, `pins_as: "gateway"`.
- [ ] **Step 2: Regenerate and check that nothing else moved.**
  Run: `node contracts/signing-vectors/generate.mjs && git diff --stat contracts/signing-vectors`
  Expected: only `authz-cases.json` changes, and `git diff --exit-code contracts/signing-vectors/vectors.json` exits 0. Every existing case keeps its `sig`.
- [ ] **Step 3: Write the failing tests.** In `test_node_rules.py`, `_run` passes `me=UUID(case["receiving_node_id"])` to `check_keyset`. For an accepted case, it applies the result to fresh pins `{core: trusted_keys, gateway: {}}` and asserts:

  ```python
  role = apply_keyset(obj, trusted_core=core, pinned_gateway=gw, key_expiry={})
  assert role == case["pins_as"]
  new = case["payload"]["key_id"]
  assert (new in core) == (role == "core") and (new in gw) == (role == "gateway")
  ```

  Add the three new names to `REQUIRED`.
- [ ] **Step 4: Run them and see them fail.**
  Run: `cd server && uv run pytest tests/test_node_rules.py -q`
  Expected: FAIL, because `check_keyset()` doesn't accept `me` yet.
- [ ] **Step 5: Implement `check_keyset(me=…)` and `apply_keyset`.** Then make `FakeNode._keyset` call them, so its own target check and role routing are removed, not duplicated.
- [ ] **Step 6: Run the Python tests that cover this.**
  Run: `cd server && uv run ruff check && uv run ruff format --check && uv run mypy src tests && uv run pytest tests/test_node_rules.py tests/test_message_contracts.py tests/test_key_rotation.py -q`. Postgres tests need the CI service; if it isn't available locally, run the database-free ones and say so in the report.
  Expected: PASS.
- [ ] **Step 7: Commit** on a local branch `plan-b`, without pushing:
  `git commit -m "contracts: shared keyset target and role cases (S-9)"`

### Task 2: Rust signing foundation (P2-S4, part 1)

**Files:**
- Modify: `Cargo.toml`: add the member `crates/jarvis-node` and the workspace dependencies listed in Tech Stack
- Create: `crates/jarvis-node/Cargo.toml`, `src/lib.rs`, `src/sign.rs`, `src/wire.rs`, `src/rules.rs`
- Test: `crates/jarvis-node/tests/shared_vectors.rs`, `tests/authz_cases.rs`, `tests/support/mod.rs`

**Interfaces:**
- Produces, in `sign.rs`:
  - `enum Purpose { NodeMsg, GwTransport, CoreAuthz }` and `fn as_str(&self) -> &'static str`
  - `canonical(&Value) -> Result<Vec<u8>, SignError>`
  - `signed_bytes(Purpose, key_id: &str, &Value) -> Result<Vec<u8>, SignError>`
  - `sign(&SigningKey, Purpose, &str, &Value) -> Result<String, SignError>`
  - `verify(&[u8; 32], Purpose, &str, &Value, sig: &str) -> bool`
  - `sign_envelope(&SigningKey, Purpose, &str, &mut Value) -> Result<(), SignError>`, which sets `sig` over every field except `sig`
  - `verify_envelope(&[u8; 32], Purpose, &str, &Value) -> bool`
  - `fingerprint(&[u8; 32]) -> String`
  - `b64url(&[u8]) -> String` and `b64url_decode(&str, len: usize) -> Option<Vec<u8>>`. The decoder is strict: no padding, and non-zero trailing bits are rejected.
- Produces, in `wire.rs`:
  - `parse_ts(&str) -> Option<DateTime<FixedOffset>>`, which needs an explicit offset
  - `enum AuthzKind { NodeAction, ApprovalCard, Keyset }`, serialised as `node.action`, `approval.card` and `keyset`
  - `struct AuthzObject`, with every field of §7.1 typed and `deny_unknown_fields`; `valid_until` and `issued_at` are `DateTime<FixedOffset>`
- Produces, in `rules.rs`:
  - `struct Rejected(pub &'static str)`, whose reason strings are exactly the Python ones
  - `type CoreKeys = BTreeMap<String, [u8; 32]>` and `const CLOCK_TOLERANCE: chrono::Duration` (30 s)
  - `check_authz(&Value, &CoreKeys) -> Result<AuthzObject, Rejected>`: `malformed`, `untrusted_key_id`, `bad_signature`. It verifies over `authz["object"]` exactly as received, never a re-serialised struct.
  - `check_node_action(&Value, &CoreKeys, me: Uuid, now: DateTime<FixedOffset>) -> Result<AuthzObject, Rejected>`
  - `check_keyset(payload: &Value, authz: &Value, &CoreKeys, me: Uuid, now) -> Result<AuthzObject, Rejected>`, with the reason order of Task 1
- Produces, in `tests/support/mod.rs`:
  - `cases() -> Vec<Value>` and `vectors() -> Vec<Value>`, read from `../../contracts/signing-vectors/`
  - `assert_conforms(kind: &str, payload: &Value)`: validates against `contracts/messages/<kind>.schema.json`, with sibling schemas registered by `$id`
  - `test_key(label) -> SigningKey`, taken from a vector's `seed_hex`

- [ ] **Step 1: Write the failing tests.**
  - `every_signing_vector_matches`: for each vector, `canonical` equals `expected_canonical_utf8`, `hex(signed_bytes)` equals `expected_signed_bytes_hex`, `sign` (from `seed_hex`) equals `expected_sig`, and `verify` is true.
  - `verify_rejects_other_purpose_and_key_id`: vector `same-object-two-purposes:node-msg`'s signature fails under `CoreAuthz`, and also under the other key's `key_id`.
  - `fingerprint_matches_shared_case`: `fingerprint(challenge public_key)` equals `enrol_fingerprint` in `accept-challenge-enrol-by-fingerprint`.
  - `b64url_is_strict`: padding, `+`/`/`, a wrong length and non-zero trailing bits all decode to `None`.
  - `node_action_and_keyset_cases`: for every case with `check` of `node_action` or `keyset`, accept means `Ok`, and reject means `Err(Rejected(case.reason))`. An accepted keyset's object equals `case.authz.object`.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node`
  Expected: FAIL to compile, because the functions don't exist yet.
- [ ] **Step 3: Implement `sign.rs`, `wire.rs` and `rules.rs` against the interfaces above.** If the JCS library fails any vector, stop and report under the operating rule. Don't patch around it. (It happened: `serde_jcs` failed key sorting, and Manj ruled R12-35.)
- [ ] **Step 4: Run the tests and clippy.**
  Run: `cargo test -p jarvis-node && cargo clippy -p jarvis-node --all-targets -- -D warnings`
  Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "jarvis-node: JCS signing, shared vectors and authz cases"`

### Task 3: Local journal, node state and key storage

**Files:**
- Create: `crates/jarvis-node/src/journal.rs`, `src/state.rs`, `src/trust.rs` (data and pruning only), `src/keystore.rs`
- Test: inline `#[cfg(test)]` modules, plus `tests/keystore_windows.rs` (`#![cfg(windows)]`)

**Interfaces:**
- Produces, in `journal.rs`, one JSON object per line. Each record is appended, written, then `File::sync_data()`.
  - `enum Record { Seen{outbox_id}, Cleared{outbox_id}, Started{execution_key, args: FileFacts, at}, Staged{execution_key}, Done{execution_key, receipt: Value} }`, with the tag `"t"` set to `seen`, `cleared`, `started`, `staged` or `done`. `FileFacts` is defined here and also used by Task 8.
  - `Journal::open(&Path) -> Result<Journal, JournalError>` and `Journal::append(&mut self, &Record) -> Result<(), JournalError>`
  - `Journal::state(&self) -> &JournalState`, holding `seen: HashSet<Uuid>`, `cleared: HashSet<Uuid>` and `executions: HashMap<Uuid, Stage>`, with `enum Stage { Started(FileFacts), Staged(FileFacts), Done(Value) }`
  - `JournalError::{Io, Corrupt{line: usize}, OutOfOrder{execution_key}}`. Stages move only Started → Staged → Done. A second Started for the same key is `OutOfOrder`.
- Produces, in `trust.rs`:
  - `struct PinnedKey { public_key: [u8; 32], expires_at: Option<DateTime<FixedOffset>> }`
  - `struct Trust { core: BTreeMap<String, PinnedKey>, gateway: BTreeMap<String, PinnedKey> }`
  - `Trust::prune(&mut self, now)`, which drops keys with `now > expires_at`, as `FakeNode._prune` does
  - `Trust::core_keys(&self, now) -> CoreKeys` and `Trust::gateway_key(&self, key_id, now) -> Option<[u8; 32]>`
- Produces, in `state.rs`:
  - `struct NodeState { node_id: Option<Uuid>, trust: Trust }`
  - `state::load(&Path) -> Result<Option<NodeState>, StateError>`
  - `state::save(&Path, &NodeState) -> Result<(), StateError>`: write to `node-state.json.tmp`, `sync_all`, rename, then sync the folder
- Produces, in `keystore.rs`:
  - `trait KeyStore: Send + Sync { fn load(&self) -> Result<Option<SigningKey>, KeyStoreError>; fn save(&self, &SigningKey) -> Result<(), KeyStoreError>; fn delete(&self) -> Result<(), KeyStoreError>; }`
  - `MemoryKeyStore`, under `cfg(any(test, feature = "test-support"))`
  - `#[cfg(windows)] DpapiKeyStore::new(path)`: `CryptProtectData` / `CryptUnprotectData`, user scope, `CRYPTPROTECT_UI_FORBIDDEN`, optional entropy `b"jarvis-node-key-v1"`, and a blob file `node-key.dpapi`
- Adds the crate feature `test-support` and the dev-dependency `jarvis-node = { path = ".", features = ["test-support"] }`.

- [ ] **Step 1: Write the failing tests.**
  - `appends_survive_reopen`
  - `torn_final_line_is_dropped`: append two records, then write a partial third with no newline; reopening keeps two, truncates the file to the last newline, and the next append works.
  - `corrupt_middle_line_fails_closed`: `Corrupt{line: 2}`.
  - `stages_only_move_forward`
  - `save_is_atomic_and_round_trips`: no `.tmp` file is left, and loading equals what was saved.
  - `missing_state_is_none`
  - `pruned_keys_are_not_trusted`
  - On Windows: `dpapi_round_trip`, and `dpapi_blob_does_not_contain_the_seed`, which checks the file bytes don't contain the 32-byte seed.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node journal state trust keystore`
  Expected: FAIL to compile.
- [ ] **Step 3: Implement the four modules against the interfaces above.**
- [ ] **Step 4: Run the tests.**
  Run: `cargo test -p jarvis-node`. In CI, the Windows tests run on `windows-latest`.
  Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "jarvis-node: fsync'd journal, atomic node state, DPAPI key store"`

### Task 4: Gateway connection (P2-S4, part 2)

**Files:**
- Create: `crates/jarvis-node/src/code.rs`, `src/session.rs`, `src/client.rs`
- Modify: `crates/jarvis-node/src/rules.rs`: add `check_challenge`, `check_enrolment_welcome` and `SeqChecker`
- Test: `tests/authz_cases.rs` (the remaining checks), `tests/session.rs`, `tests/client.rs`, and a fake gateway in `tests/support/gateway.rs` (a tokio-tungstenite server that signs with `test_key`)

**Interfaces:**
- Consumes: Task 2's `sign`, `rules` and `wire`; Task 3's `Trust`, `NodeState`, `state` and `KeyStore`.
- Produces, in `code.rs`:
  - `struct EnrolmentCode { token, core_fp, gateway_fp }`
  - `EnrolmentCode::parse(&str) -> Result<Self, CodeError>`, for `token.corefp.gwfp`, where the token is `[a-z2-7]{26}` and each fingerprint is `[a-z2-7]{16}`, as `jarvis_server.nodes.parse_enrolment_code` defines
- Produces, in `rules.rs`, with the same contracts and reason order as `node_rules.py`:
  - `check_challenge(&Value, pinned: Option<&BTreeMap<String, [u8; 32]>>, enrol_fp: Option<&str>) -> Result<Value, Rejected>`
  - `check_enrolment_welcome(&Value, core_fp: &str) -> Result<(Uuid, String, [u8; 32]), Rejected>`
  - `SeqChecker::new()` and `SeqChecker::expect(&mut self, i64) -> Result<(), Rejected>`
- Produces, in `session.rs`, a sans-IO state machine:
  - `enum Start { Enrol(EnrolmentCode), Hello { resume: Vec<Value> } }`
  - `Session::new(key: SigningKey, state: NodeState, start: Start) -> Session`
  - `Session::on_frame(&mut self, text: &str, now) -> Result<Vec<Output>, Stop>`
  - `Session::send(&mut self, kind: &str, payload: Value, now) -> Result<String, Stop>`, which is allowed only after `Ready`
  - `Session::state(&self) -> &NodeState`
  - `enum Output { Send(String), Ready { session_id: Option<Uuid>, summary: Vec<Value>, enrolled: bool }, Frame { kind: String, payload: Value }, ClockOff(i64) }`
  - `struct Stop { reason: &'static str, reenrol: bool }`
  - `after_close(code: u16) -> AfterClose`, where `enum AfterClose { Reconnect, Reenrol }`, and `backoff(attempt: u32) -> Duration`
  - Behaviour, as FakeNode does: the first frame must be a valid `challenge` (otherwise `Stop` with no `Send`); then send `hello` or `enrol`. After an enrolment, `welcome` must pass `check_enrolment_welcome`, and only then are `node_id` and the CORE and GATEWAY pins set. Every later frame is `seq`-checked and verified under the challenge's key. A key that is no longer trusted gives `Stop{reenrol: true}`; a bad signature under a trusted key gives `Stop{reenrol: false}`. `ping` gets an automatic `pong`. `clock_skew_ms` = local now minus `challenge.issued_at`, and `ClockOff` is sent when it is over 30 000.
- Produces, in `client.rs`, the driver:
  - `struct ClientConfig { gateway: String /* host:port */, data_dir: PathBuf, heartbeat_timeout: Duration /* 95 s */ }`
  - `client::spawn(cfg, Arc<dyn KeyStore>, frames: mpsc::UnboundedSender<Output>) -> Client`, which forwards only `Ready`, `Frame` and `ClockOff`; it writes `Send` itself
  - `Client::send(&self, kind: &'static str, payload: Value) -> Result<(), NotConnected>`
  - `Client::enrol(&self, code: &str) -> Result<(), EnrolError>`
  - `Client::status(&self) -> watch::Receiver<LinkStatus>`, where `enum LinkStatus { NotEnrolled, Connecting, Connected, NeedsReenrol, ClockOff(i64) }`
  - The reader task only parses, verifies and forwards. Nothing in it waits on the consumer.
  - On a failed enrolment, or `Stop{reenrol: true}`, the key and state are deleted as §7.2 step 5 says. Reconnecting keeps them.

- [ ] **Step 1: Write the failing tests.**
  - `challenge_welcome_and_seq_cases`: every shared case with `check` of `challenge`, `enrolment_welcome` or `seq`.
  - `every_shared_case_is_run`: every `check` value in `authz-cases.json` is handled by some Rust test, and the total equals the file's length.
  - `code_parse_accepts_and_rejects`.
  - Enrolment and handshake:
    - `enrol_pins_keys_from_code`: the `enrol` payload `assert_conforms("enrol", …)` and has `seq` = 1 and the nil `node_id`;
    - `enrol_core_mismatch_discards_everything`: nothing is saved and the key store is empty;
    - `hello_uses_pinned_gateway_key`;
    - `anything_but_a_valid_challenge_first_closes_silently`.
  - Signatures and trust:
    - `bad_signature_under_trusted_key_reconnects`;
    - `untrusted_gateway_key_asks_reenrol`.
  - `seq`:
    - `outgoing_seq_starts_at_1_and_increments`;
    - `peer_seq_gap_repeat_or_decrease_stops`.
  - Heartbeat and clock:
    - `ping_gets_pong`;
    - `clock_off_reported_over_30s`.
  - Close codes and backoff:
    - `after_close`: 4409 → `Reenrol`; 4401, 1000, 1006 and 4400 → `Reconnect`;
    - `backoff_is_1_2_4_8_16_then_30`.
  - Against the fake gateway:
    - `client_reconnects_after_4401`;
    - `client_stops_on_4409_and_reports_needs_reenrol`;
    - `missed_heartbeats_reconnect` (with a 300 ms timeout);
    - `slow_consumer_does_not_stall_pong` (R12-10): the frame consumer is blocked for 5 s, and a `ping` sent during that time still gets its `pong` within 1 s.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node --test authz_cases --test session --test client`
  Expected: FAIL to compile.
- [ ] **Step 3: Implement `code.rs`, the three rules, `session.rs` and `client.rs`.**
- [ ] **Step 4: Run all of `jarvis-node` and clippy.**
  Run: `cargo test -p jarvis-node && cargo clippy -p jarvis-node --all-targets -- -D warnings`
  Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "jarvis-node: handshake, enrolment, pins, seq, heartbeat and reconnect"`

### Task 5: Durable delivery and key rotation (P2-E13, Rust half)

**Files:**
- Create: `crates/jarvis-node/src/delivery.rs` and `src/node.rs` (the runtime that routes frames)
- Modify: `crates/jarvis-node/src/trust.rs`: add `apply_keyset`
- Modify: `crates/jarvis-node/src/session.rs`: handle `keyset` frames
- Test: `tests/e13_replay.rs`, `tests/delivery.rs`, `tests/keyset.rs`

**Interfaces:**
- Consumes: Task 3's `Journal`; Task 4's `Client`, `Output::Frame` and `check_authz`.
- Produces, in `delivery.rs`:
  - `Inbox::new(Arc<Mutex<Journal>>)`
  - `Inbox::on_item(&mut self, outbox_id: Uuid) -> Result<ItemFate, JournalError>`, where `enum ItemFate { Show, AlreadySeen, Cleared }`. `Seen` is journalled before `Show` is returned.
  - `Inbox::on_cleared(&mut self, outbox_id: Uuid) -> Result<(), JournalError>`
- Produces, in `trust.rs`:
  - `Trust::apply_keyset(&mut self, obj: &AuthzObject) -> KeyRole`, where `enum KeyRole { Core, Gateway }`. It is `apply_keyset` from Task 1's Python, including the 24 h overlap from `issued_at`, and a re-send changes nothing.
- `session.rs`, on a `keyset` frame:
  - it runs `check_keyset` with `payload` = `{role, key_id, public_key}` only;
  - an untrusted `key_id` gives `Stop{reenrol: true}`;
  - otherwise it emits `Output::TrustChanged(NodeState)`, and then `Send(received)` and `Send(ack)` for the `outbox_id`. The driver saves the state before writing either frame.
- Produces, in `node.rs`:
  - `struct NodeDeps { keystore: Arc<dyn KeyStore>, ui: mpsc::Sender<UiEvent> }`. Task 8 adds `folder: Option<Arc<dyn FolderOps>>`.
  - `node::start(ClientConfig, NodeDeps) -> NodeHandle`, where `NodeHandle` is `Clone`
  - `NodeHandle::status()`, `NodeHandle::send(kind, payload)`, `NodeHandle::enrol(code)`
  - `NodeHandle::shown(outbox_id)`, which sends `ack`
  - `NodeHandle::subscribe_replies() -> broadcast::Receiver<ReplyChunk>`, where `struct ReplyChunk { reply_id: Uuid, seq: i64, kind: ChunkKind, final_: bool }` and `enum ChunkKind { Speech(String), Action(AuthzObject) }`
  - `enum UiEvent { Notice{outbox_id, priority, kind, body}, NoticeCleared{outbox_id, reason}, Summary(Vec<Value>), Link(LinkStatus) }`. Task 7 adds `ApprovalCard` and `ApprovalCleared`.
- Routing, as FakeNode does:
  - `Ready` gives `UiEvent::Link(Connected)` and `UiEvent::Summary(summary)`. Summary lines are shown in priority order (§8.3), and each `outbox_id` in them goes through `Inbox::on_item`, so it is never shown twice.
  - `outbox.item` always gets `received`, whatever its fate. If the fate is `Show` and there's no `authz`, it becomes `UiEvent::Notice`, unless the priority is SILENT, which is history only and is acked at once.
  - `outbox.cleared` gives `on_cleared` and `NoticeCleared`.
  - `reply.chunk` gets `received {reply_id, seq}` and is broadcast. An ACTION chunk must first pass `check_node_action`; if it fails, it isn't broadcast, and the node sends the receipt REFUSED with evidence `{check: reason}`.
  - Items with `authz`, and `node.action` frames, go to Tasks 7 and 8. Until then they are refused and logged.

- [ ] **Step 1: Write the failing tests.**
  - `e13_trace_replays_clean`: for each run in `server/tests/fakes/traces/e13.jsonl`, and for each node A and B, a fresh `Inbox` on a temporary journal is fed that node's frames twice in order, reopening the `Journal` from disk before each frame. For every `outbox_id`, `Show` comes at most once, and never after its `Cleared`.
  - `item_shown_once_across_restart`.
  - `cleared_before_item_never_shows`.
  - `received_sent_for_every_item_even_when_seen`.
  - `silent_item_is_acked_not_shown`.
  - Keysets, with a fake gateway signing them using `test_key`:
    - `core_keyset_adds_key_with_24h_overlap`;
    - `gateway_keyset_never_enters_core_trust`: a `node.action` then signed by that key under its `key_id` is rejected `untrusted_key_id`;
    - `keyset_acked_only_after_verify_and_save`: if saving fails, or the keyset is bad, no `received` or `ack` is sent;
    - `keyset_other_target_rejected`.
  - `sent_payloads_conform`: `received` and `ack` go through `assert_conforms`.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node --test e13_replay --test delivery --test keyset`
  Expected: FAIL.
- [ ] **Step 3: Implement `delivery.rs`, `apply_keyset`, the keyset path in the session, and `node.rs`.**
- [ ] **Step 4: Run the tests.**
  Run: `cargo test -p jarvis-node`
  Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "jarvis-node: seen-list, received/ack, cleared items, key rotation (P2-E13 node half)"`

### Task 6: ServerBrain

**Files:**
- Create: `crates/jarvis-node/src/server_brain.rs`
- Modify: `crates/jarvis-node/Cargo.toml`: depend on `jarvis-core`
- Test: `tests/server_brain.rs`

**Interfaces:**
- Consumes: `jarvis_core::brain::{Brain, BrainRequest, BrainEvent, BrainError}`; Task 5's `NodeHandle`.
- Produces:
  - `#[async_trait] trait Link: Send + Sync { fn connected(&self) -> bool; fn subscribe(&self) -> broadcast::Receiver<ReplyChunk>; async fn send(&self, kind: &'static str, payload: Value) -> Result<(), NotConnected>; }`, implemented for `NodeHandle`
  - `ServerBrain::new(Arc<dyn Link>)` and `impl Brain for ServerBrain`
  - `const SERVER_TOOLS: &[(&str, &str)]`: the seven desktop tool → action-kind pairs from `server/src/jarvis_server/core/prompt.py` `TOOL_ACTIONS`, which are `show_news`, `open_story` and `summarise_story` → `module.news`; `play_live_tv` and `next_channel` → `module.live_tv`; `close_panel` → `screen.close_panel`; and `clear_screen` → `screen.clear`.
- Behaviour of `think` (corrected 25 Nov: live actions arrive as `node.action {authz, seq, in_reply_to}`, i.e. `ReplyEvent::Action { seq, in_reply_to, authz }`, never as ACTION reply chunks, per ruling G4.2; `seq` is R12-36):
  - If not connected, return `Unavailable("I can't reach the Jarvis server")` at once. A blank utterance is a `Protocol` error and sends nothing.
  - Otherwise subscribe first, then send `utterance {text, screen_summary}`. `text` is clipped to 4000 characters and `screen_summary` to 2000, and a blank `screen_summary` is sent as null. History and tools are not sent, because the server owns them.
  - Follow the reply that answers this utterance: the first event whose `in_reply_to` is the utterance envelope's `message_id` (R12-37) picks the `reply_id`; every other reply is ignored (another device's, core's own `speak()` results with null, re-sent old ones, a second reply naming the same utterance). The first event seen may be past seq 1 (a dropped ACTION chunk).
    - Within the followed reply, an event at or below the last `seq` seen is a re-send and is dropped (never spoken or run twice).
  - Each SPEECH chunk becomes `BrainEvent::Speech` immediately; empty text is skipped.
  - Each live action of the followed reply:
    - `pc.*` actions are left to Task 8's executor, with no receipt;
    - `module.*` / `screen.*` actions with args exactly `{tool, input?}`, where `SERVER_TOOLS` maps `tool` to that `action_kind`, become `BrainEvent::Action(Action::new(tool, input))`, followed by the receipt `{reply_id, seq, result: "OK", evidence: {}}` (D-2: verified and handed to the coordinator, not rendered);
    - otherwise the receipt is REFUSED, with `{check}` = `unknown_tool`, `malformed_args`, `unknown_action_kind`, or `not_delivered` (the caller has gone).
  - `think` returns on `final`. 20 s (`SILENCE`) with no event of the followed reply gives `Unavailable("the Jarvis server didn't answer")`. A broadcast `Lagged` gives `Unavailable("lost part of the Jarvis server's reply")`.
  - In the app, `jarvis-core`'s `BRAIN_TIMEOUT` (8 s) caps the whole `think`, so a reply whose `final` lands more than 8 s after the send is cut, and `SILENCE` never fires there (R12-37: kept for now; Task 9 measures it).

- [ ] **Step 1: Write the failing tests** (with a fake `Link`; paused tokio time where timing matters).
  - `speech_streams_in_order_until_final`.
  - `first_speech_forwarded_before_final`: `tx` holds the first Speech while the final chunk hasn't been sent yet.
  - `module_action_maps_tool_and_sends_ok_receipt`: the receipt passes `assert_conforms("action.receipt", …)`.
  - `tool_kind_mismatch_refused`.
  - `other_replies_are_ignored`, `the_answer_is_followed_when_its_first_event_never_arrives`, `an_earlier_turns_reply_re_sent_is_ignored`.
  - `not_connected_fails_fast`: returns `Unavailable` within 100 ms and sends nothing.
  - `silence_times_out`.
  - `utterance_conforms_and_omits_history`.
  - `tool_table_matches_server`: parse the `TOOL_ACTIONS` block in `prompt.py` for `"x": "y"` pairs, keep the `module.` and `screen.` ones, and assert they equal `SERVER_TOOLS`.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node --test server_brain`
  Expected: FAIL.
- [ ] **Step 3: Implement `server_brain.rs`.**
- [ ] **Step 4: Run the tests.**
  Run: `cargo test --workspace`
  Expected: PASS, with `jarvis-core` tests unchanged.
- [ ] **Step 5: Commit:** `git commit -m "jarvis-node: ServerBrain on the existing Brain trait"`

### Task 7: Approval interface

**Files:**
- Create: `crates/jarvis-node/src/approval.rs`
- Modify: `crates/jarvis-node/src/node.rs`: route `approval.card` items, add `NodeHandle::decide`, and add `UiEvent::ApprovalCard` and `ApprovalCleared`
- Modify: `app/src-tauri/src/lib.rs`: the Tauri events `approval-card` and `approval-cleared`, the command `decide_approval(outbox_id, approve)`, and voice approve/deny before an utterance reaches the coordinator
- Modify: `crates/jarvis-core/src/coordinator.rs`: make `normalise` `pub` (no behaviour change)
- Modify: `app/src/main.ts` and `app/src/style.css`: the card overlay and the notice strip
- Test: `tests/approval.rs`; app tests in `app/src-tauri/src/lib.rs` `#[cfg(test)]`

**Interfaces:**
- Consumes: `check_authz`; Task 5's routing and `Inbox`.
- Produces:
  - `struct ApprovalCard { outbox_id, approval_id, summary: String, valid_until: DateTime<FixedOffset>, card_sig: String }`
  - `ApprovalCard::from_item(item: &Value, &CoreKeys, me: Uuid, now) -> Result<ApprovalCard, Rejected>`. The authz kind must be `approval.card`, the target must be `me`, `approval_id` must be set, and `now ≤ valid_until + 30 s`.
  - `ApprovalCard::seconds_left(&self, now) -> i64`, which is 0 at or after `valid_until`
  - `ApprovalCard::decision(&self, Decision, now) -> Result<Value, Expired>`, returning `{approval_id, decision: "APPROVED" | "DENIED", card_sig}`, with `enum Decision { Approve, Deny }`
  - `NodeHandle::decide(outbox_id, Decision) -> Result<(), DecideError>`, with `DecideError::{Expired, Gone, NotConnected}`. On success it sends `approval.decision` and then `ack {outbox_id}`, and keeps the card showing "Sent — waiting for Jarvis" until the server clears it.
  - `pending_voice_decision(text: &str, pending: usize) -> VoiceDecision`, where `enum VoiceDecision { NotADecision, Decide(Decision), AskToTap }`. After `normalise`, the phrases are exactly `approve` and `approve it`, or `deny` and `deny it`.
- UI:
  - The card shows the summary, a countdown and Approve / Deny buttons; the buttons are disabled at 0.
  - After the server answers, the card is replaced by what the server says: an `outbox.cleared` for the card, then a notice such as the decision refusal (`approval.decision_refused`) or the spoken result. The node never decides from a cached copy (P2-E11).

- [ ] **Step 1: Write the failing tests.**
  - `card_from_signed_item`: signed in the test with `test_key("core-1")`.
  - `card_for_other_node_or_untrusted_key_refused`.
  - `seconds_left_counts_down_to_zero`.
  - `decision_after_expiry_is_not_sent`.
  - `decision_carries_card_sig_and_conforms`.
  - `decision_then_ack_in_that_order`.
  - `cleared_card_cannot_be_decided`.
  - `two_cards_shown_separately`.
  - `voice_decision_needs_exactly_one_card`: 0 cards and "approve" → `NotADecision`; 1 card → `Decide(Approve)`; 2 cards → `AskToTap`; "approve the news" → `NotADecision`.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo test -p jarvis-node --test approval && cargo test -p jarvis-app`
  Expected: FAIL.
- [ ] **Step 3: Implement `approval.rs`, the routing, the Tauri events and command, the voice check and the UI.**
- [ ] **Step 4: Run the tests, then build the UI.**
  Run: `cargo test --workspace && (cd app && npm ci && npm run build)`
  Expected: PASS and a clean build. A screenshot of the card and its countdown from a Windows run (Manj's PC or a CI artefact) goes in the task report.
- [ ] **Step 5: Commit:** `git commit -m "desktop: approval card with countdown, tap and voice decisions"`

### Task 8: PC actions (P2-A7, P2-A8, P2-A10)

**Files:**
- Create: `crates/jarvis-node/src/folder/mod.rs` (the name rule, the `FolderOps` trait and the `Executor`), `src/folder/memory.rs` (`cfg(any(test, feature = "test-support"))`), and `src/folder/windows.rs` (`cfg(windows)`)
- Modify: `crates/jarvis-node/src/node.rs`: route ACTION chunks with `pc.list_test_folder` and approved `node.action` items to the `Executor`, run `recover()` before the first connection, and add `NodeDeps.folder`
- Test: `tests/executor.rs` (Linux and Windows, with the memory folder) and `tests/folder_windows.rs` (`#![cfg(windows)]`)

**Interfaces:**
- Consumes: `Journal` and `FileFacts` (Task 3); `check_node_action` (Task 2); Task 5's routing.
- Produces:
  - `valid_name(&str) -> bool`, exactly the Global Constraints rule
  - `trait FolderOps: Send + Sync`:
    - `fn check_folder(&self) -> Result<(), Outcome>;`
    - `fn list(&self) -> Result<Vec<FileFacts>, Outcome>;`
    - `fn stage(&self, &FileFacts, key: Uuid) -> Result<(), Outcome>;`
    - `fn recycle_staged(&self, &FileFacts, key: Uuid) -> Result<DateTime<FixedOffset>, Outcome>;`
    - `fn locate(&self, &FileFacts, key: Uuid) -> Located;`
  - `enum Outcome { Mismatch(&'static str), NotFound, Refused(&'static str), Error(&'static str) }`
  - `enum Located { InFolder, Staged, InRecycleBin, Unknown }`
  - `Executor::new(Arc<dyn FolderOps>, Arc<Mutex<Journal>>)`
  - `Executor::list_receipt(&self, reply_id, seq) -> Value`, whose files are capped at 1000
  - `Executor::delete(&self, obj: &AuthzObject) -> Value`, which does rule 5, then journals Started → `stage` → Staged → `recycle_staged` → Done
  - `Executor::recover(&self) -> Vec<Value>`, the receipts to send. It follows the Recovery table under Decisions exactly. `recycle_staged` re-reads size and SHA-256 through its handle, so the Started + Staged row checks identity before any recycle.
  - Receipts follow `contracts/messages/pc.*.receipt.schema.json`. The delete receipt carries `{execution_key, result, evidence: {file_id, before_sha256, recycled_at, check}}`, sent as `action.receipt`. After sending it, the node acks the item's `outbox_id`.
- `windows.rs` implements §9.4 step for step:
  - `check_folder`: the folder opened with `FILE_FLAG_OPEN_REPARSE_POINT` isn't a reparse point; `GetVolumeInformationByHandleW` gives `NTFS`; `GetDriveTypeW` gives `DRIVE_FIXED`.
  - `file_id`: `GetFileInformationByHandleEx(FileIdInfo)`, as 16 hex digits `:` 32 hex digits.
  - `stage`: `CreateFileW(GENERIC_READ | DELETE, share 0, FILE_FLAG_OPEN_REPARSE_POINT)`; check the final path, file ID, size and SHA-256 through that handle; rename by handle (`FileRenameInfoEx`) into `.jarvis-pending\<key>\`, which is created with `CreateDirectoryW` and given a protected DACL (signed-in user and SYSTEM, inheritance off) that is read back.
  - `recycle_staged`: re-check that the staging folder isn't a reparse point and its DACL is unchanged; re-open the file with `FILE_SHARE_DELETE` only and confirm its ID and path; call `IFileOperation` with `FOFX_RECYCLEONDELETE` while still holding the handle; then confirm through the handle that the final path is under `$Recycle.Bin`. Fail closed exactly as §9.4 says.
  - `locate`: finds the object by file ID (`OpenFileById` on the volume) and reads its final path.
- Test hook: under `test-support`, `JARVIS_NODE_CRASH_AT=moved` makes `Executor::delete` call `std::process::abort()` after the rename and before Staged is journalled, and `JARVIS_NODE_CRASH_AT=staged` aborts right after the Staged record is written.

- [ ] **Step 1: Write the failing tests with the memory folder.**
  - `name_rule`:
    - accepted: `a.txt` and `A1-b_c.d`;
    - refused: an empty name, `.hidden`, `..`, `a/b`, `a\b`, `a:b`, `C:x`, `con`, `CON.txt`, `nul`, `com1.log`, `lpt9`, a 101-character name and `é.txt`.
  - `listing_omits_invalid_names_and_non_regular_files`.
  - `listing_receipt_conforms`.
  - `no_folder_configured_refuses`.
  - `delete_journals_started_staged_done_in_order`.
  - `mismatch_leaves_file_in_place` (P2-A6, node half): a different SHA-256, and separately the same content with a different file ID.
  - `rule5_needs_approval_and_unused_key`.
  - `duplicate_action_resends_stored_receipt` (P2-A5, node half).
  - Recovery:
    - one test per row of the Recovery table: `recover_done_resends`, `recover_started_unmoved_refuses`, `recover_started_but_staged_journals_staged_then_recycles`, `recover_staged_redoes_step_4`, `recover_staged_in_recycle_bin_reports_recycled`, `recover_started_in_recycle_bin_is_error`, `recover_unknown_is_error`;
    - `recover_started_but_staged_with_wrong_identity_refuses`: a different SHA-256, and separately a different file ID, gives REFUSED, the file stays in staging, and `recycle_staged` is never called;
    - `recover_twice_sends_the_same_receipt` for every row.
  - `delete_receipt_conforms`.
- [ ] **Step 2: Write the failing Windows tests** in `tests/folder_windows.rs`, each in its own temporary NTFS folder.
  - `lists_files_with_file_ids`.
  - `listing_skips_links_and_folders`.
  - P2-A7, with junctions made by `mklink /J`, symlinks by `std::os::windows::fs`, and `reject_traversal` through the name rule:
    - `refuses_folder_that_is_a_junction`;
    - `refuses_folder_that_is_a_symlink`;
    - `refuses_file_symlink`;
    - `refuses_non_ntfs_or_missing_folder`, using a missing path and, where the runner has one, a non-fixed drive.
  - `delete_goes_to_recycle_bin`: `locate` gives `InRecycleBin`, and the file isn't in the folder.
  - `swapped_file_is_mismatch`.
  - P2-A10, where each test calls `stage`, tampers with the staging, then calls `recycle_staged` and expects `Refused` with the file still in staging:
    - `staging_replaced_by_junction_refused`;
    - `staging_dacl_changed_refused`;
    - `staged_file_swapped_refused`.
  - `p2_a8_kill_between_staged_and_done`: the test re-runs its own binary (`std::env::current_exe()`, with `--exact child_crash_delete --ignored`) under `JARVIS_NODE_CRASH_AT=staged`, sees it abort, opens the same journal, and asserts that `recover()` returns exactly one RECYCLED receipt, the file is in the Recycle Bin, and a second `recover()` returns the same stored receipt. Its twin, `p2_a8_kill_between_move_and_staged`, runs the same flow with `JARVIS_NODE_CRASH_AT=moved` (the journal says Started and the file is in staging) and must reach the same result through the Started + Staged row. A second variant swaps the staged file before recovery and must give REFUSED, with nothing recycled.
  - Where symlink creation needs a privilege the machine lacks, the test prints why and returns. When the `CI` environment variable is set, it must not skip.
- [ ] **Step 3: Run them and see them fail.**
  Run: `cargo test -p jarvis-node --test executor`. The Windows tests run in CI, or on Manj's PC with `cargo test -p jarvis-node --test folder_windows`.
  Expected: FAIL.
- [ ] **Step 4: Implement `folder/mod.rs`, `memory.rs`, `windows.rs` and the routing.**
- [ ] **Step 5: Run the tests on both systems.**
  Run: `cargo test --workspace`. This change needs a `windows-latest` CI run, so Manj approves a push of the local branch, or runs it on his PC.
  Expected: PASS on Linux and Windows, with no Windows test skipped in CI.
- [ ] **Step 6: Commit:** `git commit -m "jarvis-node: test-folder listing and Recycle Bin delete per §9.4 (P2-A7/A8/A10)"`

### Task 9: Integration and cutover (P2-A1, latency)

**Files:**
- Create: `crates/jarvis-node/src/bin/jarvis-node-probe.rs` (`required-features = ["test-support"]`)
- Create: `app/src-tauri/src/node_config.rs`
- Modify: `app/src-tauri/src/lib.rs`: choose the brain, start the node, the enrolment commands, `forget_claude_key`, and timing logs
- Modify: `app/src/main.ts`: the enrolment screen and the link status
- Create: `server/tests/test_rust_node.py`
- Modify: `.github/workflows/server.yml`: install Rust and build the probe before pytest
- Modify: `README.md`: "Connecting the desktop to the Jarvis server", with no addresses

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `struct NodeConfig { brain: BrainChoice, gateway: Option<String>, test_folder: Option<PathBuf> }` and `enum BrainChoice { Claude, Server }`
  - `node_config::load(dir: &Path) -> Result<NodeConfig, String>`, reading `node.toml` in `app_local_data_dir`. If the file is missing, the brain is `Claude` (today's behaviour). `brain = "server"` needs `gateway` as `host:port`. `test_folder` must be absolute.
  - The Tauri commands:
    - `node_status() -> String`;
    - `enrol(code: String) -> Result<(), String>`, where the code is never logged;
    - `forget_claude_key() -> Result<(), String>`.
  - In server mode, `key::load` is never called.
  - The probe: `jarvis-node-probe --gateway HOST:PORT --state-dir DIR (--enrol CODE | --say TEXT | --approve-next | --memory-folder NAME=TEXT)…`. It prints one JSON line per event.

- [ ] **Step 1: Write the failing tests.**
  - `node_config` tests: a missing file → `Claude`; server without a gateway → error; a relative folder → error.
  - In `test_rust_node.py`, reusing `test_live_node.py`'s uvicorn gateway fixture, the fake brain, and the `jarvis-core node enrol` path; the test is skipped unless `JARVIS_NODE_PROBE` names the built binary:
    - `test_rust_node_turn`: enrol → `say` a fictional phrase → SPEECH chunks → final.
    - `test_rust_node_approval_flow`:
      - the fake brain proposes `pc_delete_test_file("old-notes.txt")`;
      - the probe answers the listing from its memory folder;
      - the card arrives, `--approve-next` approves it, and the probe sends the delete receipt;
      - core verifies it with a fresh listing and records a verified `ledger.outcome`.
    - `test_rust_node_reconnects_and_never_reshows`: kill and restart the probe; an item it has seen is not shown again.
    - `test_rust_node_refuses_gateway_only_action`: an action with only a gateway signature is refused.
- [ ] **Step 2: Run them and see them fail.**
  Run: `cargo build -p jarvis-node --features test-support --bin jarvis-node-probe && cd server && JARVIS_NODE_PROBE=../target/debug/jarvis-node-probe uv run pytest tests/test_rust_node.py -q`
  Expected: FAIL.
- [ ] **Step 3: Implement the probe, `node_config.rs`, the app wiring, the enrolment screen, `forget_claude_key` and the CI step.**
- [ ] **Step 4: Add the timing log.** Log `turn_timing heard_ms=… first_speech_ms=…` per turn. Measure from the end of speech, using the VAD segment end in `jarvis-ears` (log only, no behaviour change), to the start of the first `Speak` audio.
- [ ] **Step 5: Run everything.**
  Run: `cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings && (cd app && npm run build)`, then the pytest from Step 2.
  Expected: PASS.
- [ ] **Step 6: Commit:** `git commit -m "desktop: ServerBrain cutover, enrolment screen, Rust node end-to-end tests"`
- [ ] **Step 7: Live P2-A1 on Manj's PC, by hand and with his approval for each server step.**
  1. Install the build from `release.yml`.
  2. Create a throwaway `test_folder` holding `old-notes.txt` with fictional text, and write `node.toml` with `brain = "server"`.
  3. Manj runs `jarvis-core node enrol --person manj --kind DESKTOP --name "<name>"` on the server and types the code into the enrolment screen.
  4. Say a fictional request to delete `old-notes.txt` → the card appears → approve by voice → check the file is in the Recycle Bin → Jarvis speaks the verified result → Manj checks the audit trail on the server.
  5. Then, for latency, run 5 scripted fictional phrases and record each `first_speech_ms`. The target is ≤ 3 000 ms for every turn. A miss goes to Manj with its hop timings (§6.2); the parked pooled-client and Sonnet options are his decision.
  6. Only after Manj accepts Phase 2: run `forget_claude_key`, and remove the key from Credential Manager.

---

## Acceptance map

| Gate | Where |
|---|---|
| P2-S4 (Rust half) | Task 2 `every_signing_vector_matches` and `node_action_and_keyset_cases`; Task 4 `challenge_welcome_and_seq_cases` and `every_shared_case_is_run`; Task 9 `test_rust_node_refuses_gateway_only_action` |
| P2-E13 (Rust half) | Task 5 `e13_trace_replays_clean` |
| P2-A7 | Task 8 name rule, junction, symlink and traversal tests (Windows) |
| P2-A8 | Task 8 `p2_a8_kill_between_staged_and_done` and the recovery tests |
| P2-A10 | Task 8 staging-tamper tests (Windows) |
| P2-A1 | Task 9 `test_rust_node_approval_flow` (protocol), and Step 7 live on Manj's PC (Windows file handling, voice and the audit trail) |
| Latency ≤ 3 s | Task 9 Step 4 timing log and the Step 7 measurement |
