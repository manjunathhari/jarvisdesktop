# Security review: Phase 2 (core and nodes)

**Date:** 2026-11-24 · **Branch:** `phase-2-server`, `d646791..69bd671` (85 commits, Tasks 1–14) · **Spec:** Revision 12 (§20, R12-1 to R12-33) · **Reviewer:** independent security agent (read-only; QA covers correctness separately)

## Scope reviewed

**In depth (line by line):**
- Migrations 0008 to 0013: every `gw`, `conv`, `comm`, `ledger` and `ops` function, the triggers, the grants and the downgrade guards.
- The live database. I built a scratch database from the migrations and queried `pg_proc`, `information_schema.role_table_grants`, `column_privileges`, the schema and database ACLs and the role attributes, then dropped it.
- Gateway: `gateway/session.py` (handshake, path A and path B, the established loop), `gateway/app.py`, `gateway/keys.py` and `gateway/tailscale.py`.
- Core: `core/approvals.py`, `core/actions.py`, `core/keys.py`, `core/identity.py` and `policy.py`.
- `signing.py`, `nodes.py` and `brain/claude.py`.
- The tool paths of `core/turn.py` and `core/prompt.py`.
- `tests/fakes/node_rules.py`, which is the reference that Plan B's Rust node copies.
- Deploy: `deploy/compose.yaml` (with its inline configs), `deploy/gateway-ts/entrypoint.sh`, `deploy/egress-proxy/squid.conf`, `deploy/harden/harden.sh`, `Dockerfile` and `.github/workflows/server.yml`.

**Read for specific questions only:**
- `gateway/relay.py`: delivery scoping, ACTION chunks, resume, and the reply-to-person mapping.
- `core_main.py`: secrets, output and logging.
- `core/dispatch.py` and `jobs.fail`.
- `smoke.py`: the Phase 2 rows.
- `tests/fakes/fake_node.py`: keyset role and target handling.
- `contracts.py`: model strictness and patterns.
- `contracts/signing-vectors/authz-cases.json`: case names.
- `deploy/README.md` §8.5.
- The test map.

**Not reviewed:**
- The rest of `turn.py`, including the budget and brain-health paths (correctness belongs to QA; the brain-key leak is covered under check 6).
- `scheduler_main.py`.
- `summary.py`.
- Test bodies beyond their names in the test map.
- The Rust node, which does not exist yet (Plan B).

**Throwaway checks** (under `/tmp`):
- The scratch-database queries above.
- A psycopg check showing that `str(e)` carries `DETAIL: Failing row contains (…)` (S-5).

I did not re-run the suite. I did not contact the live server.

## Threat model (short)

**Assets:**
- The authority to run IRREVERSIBLE actions on a node. In Phase 2 that is recycling one test-folder file.
- The node trust anchors: pinned core and gateway keys, and enrolment tokens.
- The signing keys, the Anthropic key and the database passwords.
- The permanent audit trail.
- Conversation, outbox and memory content.

**Adversaries:**
1. A tailnet device that Manj's ACL lets reach `jarvis-gw:8443`, but that is not an enrolled node.
2. An enrolled node, buggy or compromised, holding a valid node key.
3. A compromised gateway process. It is the only component that parses untrusted WebSocket frames, so it is the most exposed. It holds `jarvis_gateway` and `gateway_signing_key`.
4. Prompt injection reaching Claude through `screen_summary`, news text or memory.
5. A compromised neighbour on the shared Hostinger host, or a supply-chain change to a pinned image (Phase 0 S2).
6. Leakage through the repository, logs or exceptions.

**Out of scope:**
- Root on the host.
- A compromised core, which holds the core key by design.
- Tailscale itself.
- A same-user process on the desktop (§9.4 states this).

## What holds (verified)

| Control | Evidence |
|---|---|
| The gateway has exactly 12 EXECUTE grants: the 6 `gw` functions (R12-2) plus `conv.record_inbound`, `conv.record_chunk_received`, `conv.record_resume_out_of_range`, `comm.record_sent`, `comm.record_received` and `comm.acknowledge` | Scratch DB `pg_proc.proacl` |
| The gateway's table access is SELECT on exactly the §4.1 and R12-9 columns: `gw.node` (6 columns plus `key_state`), `conv.session` (`id`, `person_id`, `closed_at`), `conv.stream_chunk`, `conv.stream_cursor`, `comm.outbox`, `comm.delivery` and `gw.trusted_key` (public keys only). It has no USAGE on `ledger`, `audit` or `ops`, and no grant on `gw.enrolment` or the refusal tally | `role_table_grants`, `column_privileges`, `pg_namespace.nspacl` |
| All 28 SECURITY DEFINER functions are owned by `jarvis_owner` and use `search_path=pg_catalog, pg_temp` with PUBLIC revoked. Each is granted to one role only. The internal helpers (`gw.tally_refusal`, `gw.record_clock_skew`) and the guard triggers grant EXECUTE to nobody | `pg_proc` (prosecdef, proconfig, proacl) |
| `ops.retention_sweep`: SECURITY DEFINER, pinned path, `jarvis_app` only. It refuses `p_now > now()`. It never deletes open connections, ACTIVE reservations or QUEUED/RUNNING jobs, and it writes one RETENTION audit row per table | 0012 lines 161–256; 0013 |
| `ledger.refuse_decision` and `ledger.refuse_receipt`: fixed code lists, `jarvis_app` only, no side effects. `conv.record_resume_out_of_range`: fenced first, the person comes from the connection, `numeric` bounds, an advisory-lock suppression window, and no caller content | 0010 lines 256–311; 0009 lines 530–578 |
| No role is superuser or has CREATEROLE, CREATEDB or BYPASSRLS. PUBLIC has no CONNECT on `postgres`, `template1` or the database | `pg_roles`, `pg_database.datacl` |
| Identity (P2-S2): `conv.record_inbound` takes node and person from `gw.current_node(conn)`, never from the message. Core reads `conv.inbound.person_id`. Policy check 1 re-reads `gw.node`. Memory `viewer` and `by` are `memory_slug(person_id)`. Payload models are `extra="forbid"` | 0009 lines 292–345; `turn.py` lines 182–211; `policy.py` lines 173–178; `actions.py` lines 215–219 |
| Strict fencing: every `conv` and `comm` connection function calls `gw.current_node` first, and the lock order is node row, then connection rows | 0008 and 0009 |
| Handshake (§6.1): `whois` runs before the challenge; the nonce is 32 bytes from the OS RNG; there is one attempt, with a constant-time compare and `seq` = 1. The envelope is parsed strictly, with duplicate JSON keys and NaN or Infinity refused. On path A the signature is checked against the *registered* key, then revoked, identity and `key_state`. Path B proves possession of the carried key and passes the connection's `whois` identity, never the message's. A STALE node gets the challenge, then 4409, with no welcome and no connection row | `session.py` lines 341–506 |
| Node-side trust reference: CORE and GATEWAY pins are kept apart (R6-3). The gateway key is pinned only by the enrolment fingerprint, and the core key only by the `welcome.enrolment` fingerprint. A keyset is core-signed, expires at `valid_until + 30 s` and must match its args; the role decides the pin set and the target must be null or self. The shared cases include `reject-gateway-key-under-core-key-id` and `reject-challenge-signed-by-core-key`. Base64url decoding is strict (canonical re-encode) | `node_rules.py`; `fake_node.py` lines 560–590; `authz-cases.json`; `signing.py` lines 56–70 |
| Server-side core trust: cards are verified only against `role = 'CORE'` keys (ACTIVE or retired-pending). `key_id = fingerprint(public)` is enforced when private keys load. The legacy secret is used only for the bootstrap key, and only when its fingerprint equals the ACTIVE key_id. No private key touches the database | `core/keys.py` lines 119–135; `signing.py` lines 158–181 |
| Approvals: `card_sig` must be byte-equal to the stored card's signature, and the stored card must verify under a trusted core key with `kind = approval.card` and the same `approval_id`. The person must match both the row and the card's signed `person_id`. The node must not be revoked, the decision must be PENDING and the approval in date, all checked under `FOR UPDATE`. Consumption happens only through `ledger.consume_approval`. The guard trigger blocks every other path, and `jarvis_app` cannot UPDATE `consumed_at` or `execution_key` at column level, so the GUC bypass in the guard can't be abused. The action is targeted at the requesting node in SQL (`record_sent` / `received` / `acknowledge`) as well as in the relay | `approvals.py` lines 242–414; 0010 lines 92–208; 0009 lines 351–482 |
| Receipts: one per `execution_key`, and the `ledger.action` row is taken *before* the task moves. Only the requesting node may send it. The file ID and SHA-256 must match the approval. A fresh listing must omit the file ID, and a listing that repeats a name or file ID fails closed. Late, malformed and unknown receipts become audited REJECTED rows | `actions.py` lines 180–197 and 441–535 |
| Policy: default-deny tier table; Claude's wire names are mapped through `TOOL_ACTIONS` before policy runs; IRREVERSIBLE always becomes `NeedsApproval`; file names follow the §9.4 pattern on the server too | `policy.py`, `turn.py` lines 501–541, `actions.py` lines 169–177 |
| Prompt injection: `screen_summary` and memory sit in labelled sections marked as untrusted data, with `<` and `>` escaped and memory flattened to one line per item. The deferred Task 8 item is fixed | `claude.py` lines 72–107 |
| Egress (static): gateway UID 10001 gets exactly the R12-33 rules. ACCEPT on `tailscale0` for ESTABLISHED,RELATED; ACCEPT on TCP to `10.214.14.10:5432`; DROP for everything else, including loopback (no Docker DNS), the proxy and new tailnet connections. The IPv6 chain has no Postgres rule, and the sidecar refuses to start if IPv6 is on and `ip6tables` fails. The gateway binds only its tailnet IPv4 address, uses a fixed Postgres IP outside the dynamic `ip_range`, and its healthcheck requires the rules | `entrypoint.sh`, compose lines 33–35, 170–236 and 274–282, `app.py` lines 183–203 |
| Squid allows only `CONNECT` to `api.anthropic.com:443` (with `-n`, and CONNECT to a raw IP fails to match), keeps no cache, and its healthcheck fails closed if the config is missing. Core is on `jarvis_internal` only (internal network), with `trust_env=False` and an explicit proxy | `squid.conf`, compose lines 104–132 and 238–263, `claude.py` line 178 |
| Compose: no `ports:`, no `network_mode: host`, no `privileged`. App containers run `read_only`, `cap_drop: ALL` and `no-new-privileges`. The sidecar has only `NET_ADMIN` and `NET_RAW`. The gateway gets only `pg_gateway_password` and `gateway_signing_key`, and is the only service besides the sidecar with the socket volume. Core alone gets `core_signing_key` and `anthropic_api_key`. Third-party images are pinned by digest | compose |
| Secrets: `harden.sh` creates everything `root:10001 0440` under umask 0337, never regenerates and never prints. The `.pub` files are 32 raw bytes, checked. No real key or token is committed (diff scan). The API key travels only in the request header. `BrainCallFailed` is raised outside the `except` block, so no `httpx` request (header or body) is carried as `__context__` (the Task 8 fix). Key loaders name files, never key bytes | `harden.sh` lines 32–100, `claude.py` lines 193–209, `signing.py` lines 151–181 |
| Input: a 1 MiB uvicorn ceiling and a 65 536-byte gateway limit; the first frame is size-checked before parsing; `seq` is exact; the nonce is single-use; `resume` is limited to 64 entries and applied only within the stored range (never widened), with JCS capping signed integers at 2^53 − 1; the SQL side is `numeric` and rejects NaN or fractions. Audit-flood protection (R12-3) has a per-key tally plus a ceiling of 30 per window, keyed on server-side identifiers only | `session.py` lines 93 and 390–419, `app.py` line 54, `relay.py` lines 479–509, 0008 lines 125–167 |

## Findings

| # | Severity | Finding | Decision |
|---|---|---|---|
| S-1 | **Important** | A compromised gateway can approve an IRREVERSIBLE action without the person | Fix before merge, or an explicit owner ruling (below) |
| S-2 | Low | The gateway's egress firewall depends on an unpinned UID and fails open if the UID changes | Fix (cheap) |
| S-3 | Low | Marking a node STALE doesn't close its open connection | Defer |
| S-4 | Low | An authenticated node can grow the permanent audit trail without bound | Defer |
| S-5 | Low | Job failures log and store exception text that can contain row content | Fix (cheap) |
| S-6 | Low | The gateway can read every historical outbox body, including remembered memory text | Defer to the dedicated server |
| S-7 | Low | Claude-chosen text is written to memory as STATED, which is actionable | Before the real-memory switch |
| S-8 | Low | Claude's read-only tool arguments are core-signed without schema validation | Defer |
| S-9 | Low | The shared cases lack keyset-target and keyset-role cases, which Plan B relies on | Before Plan B |
| S-10 | Low | Post-handshake payloads are validated in lax mode, and the raw payload is stored | Defer |
| S-11 | Info | The live egress proof (P2-S5 and P2-S7 live) is still outstanding; CI guards are partial | Before enrolling a real node |

### S-1 (Important): approval decisions are authenticated by the gateway alone

- **Where:**
  - `migrations/versions/0009_conv_comm.py:292–345` stores only `payload`, not the node's signed envelope.
  - `migrations/versions/0008_gateway_nodes.py:314` lets `gw.open_connection(p_node_id)` open a connection for any node, with no proof of possession.
  - `0009:265` grants the gateway SELECT on `comm.outbox`, including `authz`, so it can read `card_sig`.
  - `server/src/jarvis_server/core/approvals.py:321–346` takes `card_sig` and `decision` from `conv.inbound.payload` and checks no node signature.
- **Threat:** an attacker who gets code execution in the gateway process (the only component that parses untrusted frames) or its `jarvis_gateway` credentials.
- **How:**
  1. The attacker reads a pending card's `sig` from `comm.outbox.authz`.
  2. On the real node's connection, or on one it opens itself with `gw.open_connection(<node>)`, it calls `conv.record_inbound(conn, uuid, 'approval.decision', {"approval_id": …, "decision": "APPROVED", "card_sig": …})`.
  3. Core's checks all pass: the signature matches, the person comes from the node row, the node is not revoked, the approval is PENDING and in date. Core consumes the approval and signs a `node.action` for the requesting desktop.
  4. The attacker withholds the card from the desktop, delivers the action (or lets the relay deliver it), and the desktop executes it, because the action is genuinely core-signed for that node.
  5. The attacker can start the whole flow too, by injecting an `utterance` that leads Claude to propose the delete, and let the real desktop answer the listing.
- **Impact:** the "the right person's node" guarantee in §9.3 becomes "the gateway says the right person's node". The gateway is inside the trusted base for IRREVERSIBLE actions, against the layered intent of §7.1 and R6-3, where a gateway key never authorises an action. The same gap lets a compromised gateway forge `action.receipt` and listings, so a task can be marked COMPLETED and verified when nothing was recycled. **Phase 2 blast radius:** one approved test-folder file goes to the Recycle Bin (recoverable). It grows with every later IRREVERSIBLE kind.
- **Fix:**
  1. Have the gateway pass the node's complete signed envelope to `conv.record_inbound`, and store it, for example `conv.inbound.envelope jsonb` (0009 in place, which needs an owner ruling).
  2. In `on_decision`, and in the approved-receipt and verification-listing paths, core re-verifies the `node-msg` signature with `gw.node.public_key` and `key_id = fingerprint(public_key)`. It also checks that the envelope's `node_id` equals the inbound's `node_id`, that `kind` matches, that `message_id` equals the inbound ID, and that the envelope `payload` equals the stored payload.
  3. Fold in the deferred Task 11 guard while there: refuse any `decision` other than APPROVED or DENIED.
  4. Replay is already bounded by `message_id` uniqueness, the card-signature binding and PENDING-only decisions.
- **If the owner prefers to defer:** record it in §20 as an accepted Phase 2 limitation, with a deadline of "before any IRREVERSIBLE kind beyond `pc.delete_test_file`, and before Phase 3".

### S-2 (Low): the gateway's egress firewall fails open if its UID changes

- **Where:** `deploy/gateway-ts/entrypoint.sh:32–33` and the compose `gateway` service (lines 174–192).
- **The gap:** the rules match only `--uid-owner 10001`. The gateway's UID comes from the image's `USER jarvis` (`Dockerfile`); compose sets no `user:` and the gateway never checks its own UID.
- **Threat and impact:** a future image change, or a `docker compose run --user …`, runs the gateway with unrestricted egress in the sidecar's namespace, which has an internet route through `jarvis_ts_egress`. P2-S5 would silently stop holding.
- **Fix:** set `user: "10001:10001"` on `gateway`, and make `jarvis-gateway` exit if `os.getuid() != 10001`. Better still, send every non-root UID through the chain (`-m owner ! --uid-owner 0`), so the default is closed.

### S-3 (Low): a node marked STALE keeps its open connection

- **Where:** `core/keys.py:339–348` and `gateway/relay.py:611`; `gw.current_node` doesn't check `key_state`.
- **The gap:** `activate_ready` marks nodes STALE, but only a gateway-key rotation recycles connections (`close_rotated`). After a core-key rotation, a STALE node stays connected until its next reconnect. It can still send utterances, decisions and receipts, and it receives items signed under a key it doesn't hold. "A stale node gets only the challenge, then 4409" holds per handshake, not per connection.
- **Impact:** small. STALE is a trust-sync state, not a revocation, and the node can't verify new items, so it prompts for re-enrolment.
- **Fix:** on `NOTIFY jarvis_keys`, re-read `key_state` for the open links and close STALE ones.

### S-4 (Low): an authenticated node can grow the permanent audit trail without bound

- **Where:** 0009 (`record_sent`, `record_received` and `acknowledge` writing `unknown_item`; `record_inbound` writing `id_conflict` and `unknown_kind`), `ledger.refuse_decision`, `ledger.refuse_receipt`, and CONNECT/DISCONNECT on every reconnect.
- **The gap:** R12-3 bounds only handshake refusals. A node with a valid key can loop over `ack` with random outbox IDs, or decisions with random approval IDs, writing one append-only audit row (plus an URGENT `approval.decision_refused` item) per message. Reconnect loops do the same with CONNECT/DISCONNECT. There is no per-connection rate limit.
- **Impact:** disk growth and noise in the evidence trail. The node can be revoked.
- **Fix:** a per-node tally for post-handshake REJECTED rows, mirroring R12-3, or a per-connection message-rate cap in the gateway that closes the connection with `rate_limited`.

### S-5 (Low): job failures log and store exception text with row content

- **Where:** `core/dispatch.py:77–78` and `core_main.py:109`.
- **The gap:** `str(e)` goes to the log and into `ops.job.last_error`, which is kept 30 days. For psycopg errors, `str(e)` includes `DETAIL: Failing row contains (…)`. I confirmed this in `/tmp`. Any constraint violation in a turn, memory or outbox write would copy utterance, reply or memory text into logs and the job table, against the Phase 1 rule of no content in logs.
- **Fix:** log and store `type(e).__name__` plus `e.sqlstate` (or a code) only.

### S-6 (Low): the gateway can read all historical outbox bodies, including memory text

- **Where:** `0009:265`, together with `actions.py:225–228` (the `memory.written` body is `Remembered: <content>`).
- **The gap:** the outbox is kept indefinitely, and the gateway can SELECT every row. A compromised gateway can read the whole notification history, including remembered facts. P2-S1 ("can't read memory") holds for the memory store, but its content leaks through this one item.
- **Fix:** before real data, either use row-level security or a view that limits the gateway to non-terminal or recent items, or keep content out of the `memory.written` body (use the memory ID only).

### S-7 (Low): Claude-chosen text is stored as STATED memory

- **Where:** `core/turn.py:564–580` and `core/actions.py:215–219`.
- **The gap:** any `memory_write` that Claude makes is stored STATED and "by" the person, and STATED entries pass `assert_actionable` (policy check 5). An instruction hidden in `screen_summary` or in news text, if Claude follows it despite the labels, can plant an actionable "stated" fact. This is spec-compliant (§12), but §12 also says Claude only proposes INFERRED entries.
- **Fix:** before the real-memory switch, either store these as INFERRED and let the person confirm them, or accept only content grounded in the current utterance.

### S-8 (Low): read-only tool arguments are signed without schema validation

- **Where:** `core/turn.py:537–541`.
- **The gap:** `{"tool": name, "input": call.args}` is signed by core and sent to the desktop exactly as Claude produced it. The `input_schema` (enums such as `close_panel.which`, integer story numbers) is never enforced on the server, so the desktop modules are the only validation.
- **Fix:** validate `call.args` against the `ToolSpec.input_schema`, with no additional properties, before signing.

### S-9 (Low): the shared cases are missing two keyset rules

- **Where:** `contracts/signing-vectors/authz-cases.json`.
- **The gap:** the keyset target rule (R12-27) and the rule that `role` picks the pin set (R6-3) exist only in `fake_node.py:567–573`, not in `check_keyset` or the shared cases. Plan B's Rust node builds against the cases.
- **Fix:** add `reject-keyset-other-target` and a case where a keyset with `role: gateway` never enters core trust.

### S-10 (Low): post-handshake payloads are validated in lax mode

- **Where:** `gateway/session.py:190–194`; this is deferred minors Task 1 #7 and Task 6.
- **The gap:** `model_validate` runs in lax mode, so `"seq": "3"` passes, and then the *raw* payload is stored. Every consumer I traced fails closed today: the listing poll doesn't match, and core re-validates receipts.
- **Fix:** use `strict=True`, as the envelope already does.

### S-11 (Info): live egress proof outstanding; CI guards partial

- **Live proof:** P2-S5 and P2-S7 have only static halves in this range (test map, "not authorised in Task 15"). They must pass on the deployed stack before a real device enrols.
- **CI guards:** the CI guard catches `ports:` only (deferred Task 14 M6). Add `network_mode: host`, `privileged: true` and any `cap_add` outside an allow-list. The `harden.sh` mode test skips when not run as root (M7): run it under `sudo` in CI.

## Deferred-list triage (security-relevant)

**Resolved in this range (no action):**
- Task 1: the gateway-key-under-core-key-id case now exists (`reject-gateway-key-under-core-key-id`).
- Task 1: `check_keyset` now enforces `valid_until`.
- Task 1 M1: `_raw_key` is now strict base64url.
- Task 1 M3: FakeNode routes the keyset role to its pin set (the shared case is still missing: S-9).
- Task 2: unknown `conn_id` now gives JF001 everywhere.
- Task 4: the guard triggers have PUBLIC revoked (checked in `pg_proc`).
- Task 4: `decided_at` / `decided_by_node_id` are now enforced by CHECK `approval_decided_by_node_and_time`.
- Task 8: `screen_summary` is now labelled as untrusted.
- Task 13: the job-dedup index is in (R12-32).
- Task 14 M1, M2 and M4: verified in compose and `harden.sh`.

**Must fix before merge:** none of the deferred items on its own. The Task 11 item "decision handler trusts the gateway schema for APPROVED/DENIED" should be folded into the S-1 fix; on its own it fails closed (a CHECK violation, then the job fails).

**Can stay deferred:**

| Item | Why it can wait |
|---|---|
| Task 1: `canonical()` accepts floats | Core never signs floats. Fix before Plan B to avoid divergence between implementations |
| Task 1 / Task 6: lax payload models | S-10 |
| Task 1 M2: challenge `node_id` not required to be nil | Node side, harmless |
| Task 4: PENDING → EXPIRED allowed before `expires_at` | Fails safe |
| Task 6: GatewayKeys doesn't check `core_key_id == fingerprint` | `init_keys` and `announce` compute fingerprints; the node pins by fingerprint |
| Task 7: `_guarded` swallows non-psycopg errors at INFO | Observability; could hide a fault, not a bypass |
| Task 7: per-link queues unbounded | A node can only slow its own connection |
| Task 7: SendTimeout during the handshake | Liveness |
| Task 11: the reconnect summary ignores `target_node_id` | Same person only; the signed authz is still target-checked by the node |
| Task 11: `jarvis_app` can't lock outbox rows | Extra audit row only |
| Task 12: a retired-pending core key verifies until a manual `retire` | Recommend limiting it to activation + 24 h; the compromise runbook re-enrols anyway |
| Task 12: no check that the secret is present at activation | Availability |
| Task 14 M3: squid's RFC1918 source ACL | Fine with R12-33 in place |
| Task 14 M6 / M7 | S-11 |
| Task 14 M10: static tests only | S-11 |
| Task 6 rulings (bind check, 64 KiB frame, `ts:StableID` identity, post-handshake closes) and the Task 7 JCS 2^53 − 1 note | Accepted; sound |

## Check-by-check verdict

| # | Check | Verdict | Notes |
|---|---|---|---|
| 1 | Trust | **Pass** | Pins come only from out-of-band fingerprints. Keysets are core-signed, targeted, expiring and must match their args. Gateway keys never enter CORE trust, on the server (`role = 'CORE'`) or the node (separate pin sets, shared cases). A STALE node gets the challenge, then 4409. The Rust node is not reviewable yet; S-9 for the shared cases |
| 2 | Authorisation of irreversible actions | **Concern** | The mechanics hold: an exact `card_sig`, the right person, a non-revoked node, PENDING and in date under a lock, exactly-once consumption, one receipt per key bound to file ID and hash, and verification that fails closed. But a decision's authenticity rests on the gateway alone (S-1) |
| 3 | Roles, grants, SECURITY DEFINER | **Pass** | Exactly 12 functions and the specified columns. No audit write, no memory, no ledger, audit or ops. All 28 SECURITY DEFINER functions have a pinned path, PUBLIC revoked and one grantee, including `ops.retention_sweep`, `ledger.refuse_decision`, `ledger.refuse_receipt`, `conv.record_resume_out_of_range` and `gw.*` |
| 4 | Identity (P2-S2) | **Pass** | Person and viewer always come from the fenced node row, never from the payload or from Claude's arguments. (S-1 is about who can claim to be the node, not about payload identity) |
| 5 | Egress | **Pass (static)** | The iptables rules match R12-33 exactly, IPv6 included and fail-closed. Core reaches only the proxy; squid allows only `api.anthropic.com:443`. No ports, host network or privilege. The live proof is pending (S-11), and the UID coupling fails open (S-2) |
| 6 | Secrets and keys | **Pass** | Nothing is committed, printed or logged. The Task 8 exception-context fix holds. `harden.sh` modes are correct. Keys are named `<name>_<key_id>`, and the legacy secret is used only when its fingerprint matches. (S-5 is about content, not secrets) |
| 7 | Input handling | **Pass** | Frame limits, a strict envelope, exact `seq`, a single-use nonce, resume within the stored range only (no widening, JCS and `numeric` bounds), and the R12-3 tally. Minor: S-4, S-10 |
| 8 | Prompt injection | **Pass** | Memory and `screen_summary` are labelled and escaped. Tool calls are mapped and policy-checked under default deny. Claude can't set person, viewer, `by` or subject. Minor: S-7, S-8 |

## Declined to judge

- **Tailscale's own netfilter rules versus `JARVIS_GW_OUT`:** whether `tailscaled` in kernel mode adds OUTPUT rules that run before ours. That needs the live container; the P2-S5 live check covers it.
- **The local API's permissions for UID 10001:** whether `tailscaled` gives a non-root peer read-only access (enough for `whois`), so that a compromised gateway can't run `serve`/`funnel` or change prefs. This is Tailscale's behaviour, not code in this range. Worth one live check.
- **The `connect()` behaviour of the read-only socket mount:** runtime behaviour, already exercised by the Task 14 live runner.
- **Desktop-side path safety, DPAPI and the journal (§9.4, §13):** Plan B, not in this range.
- **Fingerprint strength (80 bits):** a second preimage is the relevant attack, and 2^80 is adequate for out-of-band pinning in a household. No change recommended.
- **Budget, brain-health and turn-hold correctness:** QA's domain. I reviewed only the paths that carry secrets.
- **The CONNECT and DISCONNECT rows that `gw.open_connection` writes for a node with a valid key:** unbounded by the spec's own choice (§4.1, "Not tallied"). Folded into S-4.
- **Pre-existing Phase 0 and Phase 1 deploy items:** the `backup` image is tag-pinned and holds `pg_owner_password`; `postgres` has no `cap_drop`. Unchanged by this range; Phase 0 S2 carries them.
- **Smoke-node cleanup if init crashes mid-smoke:** a non-revoked node with a discarded key is harmless (nobody holds the key). Operational.
- **Replay of a node envelope after `conv.inbound` is pruned at 30 days:** it needs a fresh authenticated connection, which means the node's private key. Not a realistic threat outside S-1.

## Assessment

**Ready to merge? With fixes.**

There are no Critical findings. The database surface, identity handling, key trust and egress rules are tight and match Revision 12, and the IRREVERSIBLE path is carefully guarded against everything except a compromised gateway.

S-1 is the one structural gap. The only component that faces the network can stand in for a person's approval, and core cannot tell. The fix is bounded: store the signed envelope and verify it in core.

**What has to happen:**

1. **S-1:** fix it before merge, or get an explicit owner ruling in §20 accepting it for Phase 2 only, with the deadline stated in S-1.
2. **Strongly recommended now, both one-line changes:**
   - S-2: pin the gateway UID, or match all non-root UIDs.
   - S-5: no exception text in logs or `last_error`.
3. **Before a real device enrols on the deployed stack:** the live P2-S5 and P2-S7 checks (S-11).
4. **Before the real-memory switch:** S-6 and S-7.
5. **Before Plan B:** S-9 and the float-canonicalisation minor.
