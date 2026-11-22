# Phase 2 server (Jarvis core and gateway) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the server side of Phase 2: the shared contracts and signing, the gateway, the event-driven core with conversations, the Claude router with budget reservations, policy and approvals, the Atlas interface, memory integration, and the deployment changes. All of it is proven against a Python fake node.

**Architecture:** A new `gateway` process (FastAPI WebSocket) and the existing `core` and `scheduler` processes communicate only through Postgres rows, with NOTIFY used only to wake them (ADR-011). All work enters as `ops.job` rows (ADR-013). The gateway holds no Claude key and no memory, and changes state only through SECURITY DEFINER functions.

**Tech Stack:** Python 3.12, uv, psycopg 3 (sync in core, async in the gateway), Alembic, FastAPI and uvicorn, `rfc8785`, `cryptography` (Ed25519), `httpx` (Claude), pytest, ruff, mypy; Postgres 17; Docker Compose; `tailscale/tailscale` sidecar.

**Plan revision:** 10 (22 Nov 2026). Revision 10 makes `ledger.consume_approval` an audited structured refusal and states the trigger is a backstop (spec R10-1, R10-2). That changes Task 4 and the Task 11 caller rule. Revision 9 applies Manj's rulings on the Task 3 review (spec R9-1 to R9-6): stream-cursor isolation by session (Tasks 3, 7, 10 and 11), strict event details, `supersede` / `expire` outcomes, `id_conflict`, `unknown_item`, the `stream_cursor` grant, the stricter Task 2 mixed-load test, and two Task 7 notes. Revision 8 (corrected after Manj's review: the exact set of functions that do and don't call `current_node` is now explicit). Revision 8 applies Manj's rulings on the Task 2 review (spec R8-1, R8-2): strict fencing with one lock order and race tests in both orders (Tasks 2, 3 and 7), CONNECT / DISCONNECT audit, and five smaller points in Task 2. Revision 7 (corrected after Manj's review: Task 2 Step 3 now returns structured refusals instead of raising). Revision 7 applies Manj's rulings on Task 2 (spec R7-1 to R7-6): audited refusals return a structured result and the caller commits before raising, audited enrolment creation, the five-function `gw` surface, `last_ping_at`, the `JF001` fencing error and the 0008 rollback rule. They change Tasks 2 and 3. Revision 6 (corrected after Manj's review: Task 6's enrolment test ends with `welcome` and makes no second `hello`, and Task 12's key-source statements now separate enrolment from ordinary welcomes and rotation). Revision 6 adds Manj's handshake corrections (spec R6-6 to R6-8): the enrolment key-pinning `welcome` block, separate `hello` and `enrol` validation paths, per-sender `seq`, and stale-node refusal after the challenge. They change Task 1, Task 6 and Task 12's tests. Revision 5 applied Manj's Task 1 rulings (spec Revision 6): the `challenge` kind and nonce handshake, key-announcement expiry, gateway/core key separation cases and the timestamp rule. They change Task 1 (contracts and shared cases) and Task 6 (handshake tests). Revision 4 fixed the `resume` rule in Task 7 (validate, then fall back to the acknowledged cursor; no clamping) and points to spec Revision 5. Revision 3 added the offline-node re-enrolment rule, heartbeat fencing, bounded `resume`, and three small corrections. Revision 2, after Manj's review, added: durable stream cursor, connection fencing, key rotation as Task 12, independent signing vectors, explicit S1/S4/S6 tests, init smoke limited to database-only rows, and the other additions in the review.

**Spec:** `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md`, **Revision 10** (R10-1, R10-2, on top of the approved Revisions 4 to 9). Implementers work from Revision 10 only. Section references (§) below point there. Read it alongside this plan.

**Out of this plan (separate plans):**
- **Plan B:** the Rust desktop node (spec §13).
- **Track D11:** the dedicated-server move (spec §19). It needs its own short design first.

This plan's tests use a Python fake node (Task 6) in place of the desktop.

## Operating rule for implementers

**Shared interfaces are owned by this plan.** The SQL function signatures, grants, message schemas, signing rules, reservation states and approval consumption are written here, and an implementer must not redefine them unilaterally. If implementation shows an interface is wrong or missing something:
1. stop;
2. write the mismatch and a proposed change in the task's report;
3. wait for an owner decision.

Only then change the contract. The plan and the spec are updated in the same commit.

## Global Constraints

- The repo is public: no secrets, IP addresses, hostnames of the VPS, or server IDs in any committed file.
- Python ≥ 3.12; `uv sync --locked`; `ruff check`, `ruff format --check` and `mypy` must pass, as in CI today.
- Migrations continue the sequence at `0008`, run as `jarvis_owner`, and only move forward.
- Every SECURITY DEFINER function: `SET search_path = pg_catalog, pg_temp`; schema-qualified references; owned by `jarvis_owner`; `REVOKE ALL … FROM PUBLIC`; EXECUTE granted to exactly one role (§4.1).
- **No content in `audit.event` or `comm.event`:** codes and IDs only (§5.3).
- **Times:**
  - "Today" and "this month" for budgets use `Asia/Kolkata`;
  - signed time fields are RFC 3339 strings;
  - signed amounts are integers in paise (§7.1).
- **Signed bytes:** `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`, Ed25519, base64url without padding (§7.1).
- **Fixed values:**

  | Value | Setting |
  |---|---|
  | Approval expiry | 10 min |
  | Approval re-surface | 2 min before expiry |
  | Live action validity | 2 min |
  | Node clock tolerance | 30 s |
  | Heartbeat | 30 s; 3 missed = offline |
  | Re-send sweep | every 60 s, for items whose last send is > 2 min old |
  | Revocation re-check | every 60 s |
  | Session idle | 30 min |
  | Prompt history | last 20 messages |
  | Enrolment token | 128-bit, 15 min, single use |
  | Brain DOWN | after 2 consecutive failures |
  | `brain.probe` | every 2 min |
  | Cap hold | 1 h, retry every 5 min |
  | Budget warning / stop | 80% / 100% of Rs 5,000 |
  | Listing timeout | 10 s |

- **Retention:**

  | Data | Kept |
  |---|---|
  | `conv.stream_chunk` | 24 h after final |
  | `conv.inbound` | 30 days |
  | `comm.delivery`, `gw.node_connection` | 90 days |
  | brain usage and reservations | 13 months |
  | everything else | indefinitely (§5.7) |

- **Compose:** no service may have `ports:`.
- Fictional people only in tests, smoke and live checks (D11).

## Review Focus

1. **Budget days and months follow Indian time.** A turn at 00:10 IST on the 1st counts towards the new month and the new day, even though it is still the previous day in UTC. The daily ceiling on the last day of the month equals the whole remainder. *Test: Task 9.*
2. **A model missing from the price table fails closed.** The router refuses the call and records an error event. It never reserves Rs 0 and goes ahead. *Test: Task 9.*
3. **The same node connecting twice** (the app opened twice, or a reconnect before the old socket times out). The newest connection wins, the older one is closed with reason `superseded`, and no outbox item is delivered twice to that node. *Test: Task 7.*
4. **Oversized or malformed node messages:** over 64 KB, invalid JSON, an unknown `kind`, or a missing field. The gateway replies with an `error` message and drops the message without writing a row, and the connection stays open. A bad signature or `seq` still closes the connection (§7.1). *Test: Task 6.*
5. **Non-ASCII text in signed objects** (Kannada or Hindi names, emoji in an utterance) signs and verifies identically in Python and Rust. *Test: the shared vectors in Task 1.*

---

### Task 1: Contracts and signing

**Files:**
- Modify: `contracts/node-envelope.schema.json` (add required `seq` (integer ≥ 1) and `sig` (string); remove the STUB note; `kind` becomes an enum of the 17 kinds below; `node_id` is a UUID, and the nil UUID is allowed only for `challenge` and a node-less `enrol` (spec §6.1))
- Modify: `contracts/approval.schema.json` (add the §5.5 columns; remove the STUB note)
- Create: `contracts/messages/<kind>.schema.json`, one per kind: `challenge`, `enrol`, `hello`, `welcome`, `utterance`, `reply.chunk`, `outbox.item`, `outbox.cleared`, `received`, `ack`, `approval.decision`, `node.action`, `action.receipt`, `keyset`, `ping`, `pong`, `error` (payload shapes from the §7.1 table; `error` = `{code, detail}`; `received` = `{outbox_id}` **or** `{reply_id, seq}`, which acknowledges every chunk up to `seq`; `hello` = `{nonce, resume: [{reply_id, last_seq}], clock_skew_ms?}`, where `resume` is the node's own record of what it has shown and `last_seq` is any integer (spec R6-5); `challenge` = `{nonce (43-char base64url), issued_at, expires_at, key_id, public_key}`; `enrol` = `{token, public_key, nonce}`; `welcome` gains an optional `enrolment` = `{node_id, core_key_id, core_public_key}` (spec R6-6); the envelope's `seq` is per sender per connection, starting at 1 and going up by exactly 1 (spec R6-8); all timestamps are RFC 3339 with an offset (spec R6-4))
- Create: `contracts/messages/authz.schema.json` (`{object, key_id, sig}`; `object` fields exactly as listed in §7.1)
- Create: `contracts/examples/messages/<kind>.valid.json` and `.invalid.json`
- Create: `contracts/signing-vectors/generate.mjs` (the independent generator; no server code)
- Create: `contracts/signing-vectors/package.json` and `package-lock.json` (pinned `canonicalize`)
- Create: `contracts/signing-vectors/vectors.json` (the generator's output, committed and reviewed)
- Create: `contracts/signing-vectors/authz-cases.json` (accept and reject cases for the node rules in §7.1; shared with Plan B)
- Create: `contracts/signing-vectors/README.md` (how the vectors were made, and that they are never regenerated by the code under test)
- Create: `server/src/jarvis_server/signing.py`
- Modify: `server/src/jarvis_server/contracts.py` (pydantic models for each message and `Authz`; register them in a new `MESSAGE_MODELS` dict)
- Modify: `server/pyproject.toml` (add `rfc8785`, `cryptography`)
- Create: `server/tests/fakes/__init__.py`, `server/tests/fakes/node_rules.py`
- Test: `server/tests/test_signing.py`, `server/tests/test_message_contracts.py`, `server/tests/test_node_rules.py`

**Interfaces:**
- Produces, in `jarvis_server.signing`:
  - `class Purpose(StrEnum)`: `NODE_MSG = "node-msg"`, `GW_TRANSPORT = "gw-transport"`, `CORE_AUTHZ = "core-authz"`
  - `canonical(obj: Mapping[str, Any]) -> bytes`
  - `signed_bytes(purpose: Purpose, key_id: str, obj: Mapping[str, Any]) -> bytes`
  - `sign(purpose: Purpose, key: Ed25519PrivateKey, key_id: str, obj: Mapping[str, Any]) -> str`
  - `verify(purpose: Purpose, public_key: bytes, key_id: str, obj: Mapping[str, Any], sig: str) -> bool` (never raises on a bad signature)
  - `sign_envelope(purpose: Purpose, key: Ed25519PrivateKey, key_id: str, env: dict[str, Any]) -> dict[str, Any]` (returns `env` with `sig` set over every other field)
  - `verify_envelope(purpose: Purpose, public_key: bytes, key_id: str, env: Mapping[str, Any]) -> bool`
  - `make_authz(key: Ed25519PrivateKey, key_id: str, obj: Mapping[str, Any]) -> dict[str, Any]`
  - `check_authz(authz: Mapping[str, Any], trusted: Mapping[str, bytes]) -> dict[str, Any]` (returns the object; raises `AuthzInvalid` if `key_id` isn't trusted or the signature fails). `trusted` must hold **only CORE keys**: callers never pass gateway keys, which is what makes R6-3 hold
  - `new_nonce() -> str` (32 bytes from `secrets.token_bytes`, base64url without padding: 43 characters)
  - `fingerprint(public_key: bytes) -> str` (the first 10 bytes of SHA-256, as lower-case base32 without padding: 16 characters)
  - `load_private_key(pem_path: Path) -> Ed25519PrivateKey`, `public_bytes(key: Ed25519PrivateKey) -> bytes` (raw 32 bytes)
- Produces, in `contracts.py`: `MESSAGE_MODELS: dict[str, type[BaseModel]]` and `Authz`

- [ ] **Step 1: Generate the golden vectors independently of the code under test**

The expected values come from a separate implementation, so a defect in `signing.py` can't produce matching wrong answers.
- **The generator:** `generate.mjs` runs on Node.js 22. It uses the npm package `canonicalize`, the JCS implementation by an RFC 8785 author, pinned in `package-lock.json`, and Node's built-in `crypto.sign(null, bytes, ed25519Key)`. It builds the signed bytes exactly as written in Global Constraints. It imports nothing from `server/` or `crates/`.
- **The output:** `vectors.json` is a list of cases: `{name, purpose, key_id, seed_hex, object, expected_canonical_utf8, expected_signed_bytes_hex, expected_sig}`. It needs at least these cases:
  - `rfc8785-appendix`: the canonicalisation input and output published in RFC 8785, checked against the RFC text, not against our code;
  - `ascii-simple`;
  - `nested-keys-unsorted` (keys out of order at two levels);
  - `kannada-and-emoji` (`"text": "ನಮಸ್ಕಾರ 🙏"`);
  - `hindi`;
  - `integers-only` (paise and sequence numbers);
  - `same-object-two-purposes` (one object signed as `node-msg` and as `core-authz`, giving different signatures);
  - `same-object-two-key-ids`.
- **`authz-cases.json`:** `{name, trusted_keys, receiving_node_id, now, authz, expect: "accept" | "reject", reason}`. It needs at least these cases:
  - `accept-valid`;
  - `reject-gateway-key-only`: signed with the gateway key using purpose `core-authz`;
  - `reject-wrong-purpose`;
  - `reject-other-target-node`: core-signed, but for another node;
  - `reject-expired`: `valid_until` + 31 s ≤ now;
  - `accept-within-tolerance`: `valid_until` + 29 s = now;
  - `reject-untrusted-key-id`;
  - `reject-tampered-args`;
  - `reject-gateway-key-under-core-key-id`: signed with the **gateway's** private key, purpose `core-authz`, `key_id` = the trusted core key's ID → reject `bad_signature` (spec R6-3);
  - keyset cases (spec R6-2), checked by `check_keyset`: `accept-keyset` (inside validity), `reject-keyset-expired` (`now` = `valid_until` + 31 s), `accept-keyset-within-tolerance` (`now` = `valid_until` + 29 s), `reject-keyset-payload-mismatch`, `reject-naive-valid-until` (and its keyset twin);
  - enrolment-pinning cases, checked by a new reference `check_enrolment_welcome(welcome_payload: Mapping[str, Any], *, core_fingerprint: str) -> tuple[UUID, str, bytes]` in `node_rules.py`: `accept-enrolment-welcome`, `reject-enrolment-core-fingerprint-mismatch`, `reject-enrolment-block-missing`;
  - sequence cases, checked by a new reference `SeqChecker` in `node_rules.py` (`expect(peer_seq: int) -> None`, raising `Rejected('bad_seq')`): `accept-consecutive`, `reject-gap`, `reject-repeat`, `reject-decrease`, `reject-first-not-1`;
  - challenge cases, checked by a new reference `check_challenge(env, *, pinned_gateway: Mapping[str, bytes] | None, enrol_fingerprint: str | None) -> dict` in `node_rules.py`: `accept-challenge`, `accept-challenge-enrol-by-fingerprint`, `reject-challenge-unpinned-key`, `reject-challenge-fingerprint-mismatch`, `reject-challenge-bad-signature`, `reject-challenge-signed-by-core-key`, `reject-challenge-naive-time`.

  These are produced by the same generator.
- **Review:** the task's reviewer checks by hand:
  - the `rfc8785-appendix` case against the RFC;
  - one signature with `openssl pkeyutl -verify -rawin` over `expected_signed_bytes_hex`;
  - that the generator imports nothing from the repo.

  Python and Rust (Plan B) both verify these committed files and never regenerate them. CI re-runs the generator and fails if the output differs from the committed file (Step 6).

- [ ] **Step 2: Write the failing tests**

```python
# server/tests/test_signing.py
def test_vectors_match(case):  # parametrised over vectors.json
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(case["seed_hex"]))
    assert canonical(case["object"]).decode() == case["expected_canonical_utf8"]
    assert sign(Purpose(case["purpose"]), key, case["key_id"], case["object"]) == case["expected_sig"]

def test_purpose_is_bound():
    sig = sign(Purpose.GW_TRANSPORT, key, "gw-1", obj)
    assert not verify(Purpose.CORE_AUTHZ, pub, "gw-1", obj, sig)

def test_key_id_is_bound():
    sig = sign(Purpose.CORE_AUTHZ, key, "core-1", obj)
    assert not verify(Purpose.CORE_AUTHZ, pub, "core-2", obj, sig)

def test_envelope_sig_covers_every_field():
    env = sign_envelope(Purpose.NODE_MSG, key, "n-1", base_env)
    for field in ("message_id", "node_id", "seq", "kind", "payload", "sent_at"):
        tampered = {**env, field: altered(env[field])}
        assert not verify_envelope(Purpose.NODE_MSG, pub, "n-1", tampered)

def test_check_authz_rejects_untrusted_key_id():
    authz = make_authz(key, "core-1", obj)
    with pytest.raises(AuthzInvalid):
        check_authz(authz, {"core-2": pub})

def test_fingerprint_is_16_chars_and_stable():
    assert fingerprint(pub) == fingerprint(pub) and len(fingerprint(pub)) == 16

def test_signed_bytes_match_vectors(case):   # signed_bytes(...) .hex() == case["expected_signed_bytes_hex"]
def test_verify_accepts_every_vector(case):  # verify(...) with the public key derived from seed_hex is True
```

The node-side authorisation rules are written once, as a reference checker in `server/tests/fakes/node_rules.py`: `check_node_action(authz, *, trusted, me: UUID, now: datetime) -> None`, which raises `Rejected(reason)`. Task 6's fake node uses it. Test it against every case in `authz-cases.json`:

```python
def test_node_rules_match_authz_cases(case):  # parametrised; accept ↔ no raise; reject ↔ Rejected(reason == case.reason)
```

`test_message_contracts.py` follows the pattern in `test_contracts.py`:
- every `contracts/messages/*.schema.json` has a model in `MESSAGE_MODELS`;
- the schemas are valid Draft 2020-12;
- valid examples pass both the schema and the model, and invalid ones fail both.

- [ ] **Step 3: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_signing.py tests/test_message_contracts.py tests/test_node_rules.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis_server.signing`)

- [ ] **Step 4: Implement `signing.py`, the schemas, the examples and the models**

`canonical` is `rfc8785.dumps`. `signed_bytes` concatenates exactly as in Global Constraints.

- [ ] **Step 5: Run the tests to check they pass**

Run: `cd server && uv run pytest tests/test_signing.py tests/test_message_contracts.py tests/test_node_rules.py tests/test_contracts.py -v`
Expected: all PASS

- [ ] **Step 6: Add the CI check that the vectors are reproducible**

In `.github/workflows/server.yml`, add a step that runs `actions/setup-node` (22), then `npm ci && node generate.mjs > /tmp/v.json` in `contracts/signing-vectors`. It then runs `diff` against both committed files, and fails if either differs.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/server.yml contracts server/tests/fakes/node_rules.py server/src/jarvis_server/signing.py server/src/jarvis_server/contracts.py server/pyproject.toml server/uv.lock server/tests/test_signing.py server/tests/test_message_contracts.py
git commit -m "Phase 2: final node envelope, message contracts, JCS signing and shared vectors"
```

---

### Task 2: Gateway role and node registry (migration 0008)

**Files:**
- Modify: `server/src/jarvis_server/bootstrap.py` (`GATEWAY_ROLE = "jarvis_gateway"`; `bootstrap(…, gateway_password: str)` creates it LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE, with CONNECT on the database)
- Modify: `server/tests/conftest.py` (a third password; `Dsns.gateway`; a `gateway` fixture connecting as `jarvis_gateway`)
- Modify: `server/src/jarvis_server/core_main.py` `_bootstrap()` and `config.py` (`gateway_dsn()`, which reads secret `pg_gateway_password`)
- Create: `server/migrations/versions/0008_gateway_nodes.py`
- Create: `server/src/jarvis_server/dbcall.py` (the audited-call helper; spec R7-1)
- Modify: `server/src/jarvis_server/contracts.py` and `contracts/audit-event.schema.json` (audit enums gain `entity_type` `node` and the six new actions, which existing contract tests require to match the database)
- Create: `server/src/jarvis_server/nodes.py` (core-side node admin)
- Modify: `server/src/jarvis_server/core_main.py` (subcommands `node enrol --person <slug> --kind DESKTOP|PHONE --name <text>` and `node revoke <node_id>`)
- Test: `server/tests/test_nodes.py`

**Interfaces:**
- Produces (SQL):
  - schema `gw`, with `gw.node`, `gw.node_connection` and `gw.enrolment` (columns as in §5.1 and §7.2);
  - unique on `gw.node.public_key`, and a partial unique on `tailscale_identity WHERE revoked_at IS NULL`;
  - `gw.create_enrolment(p_token_hash bytea, p_person_id uuid, p_kind text, p_name text) RETURNS uuid` (EXECUTE for `jarvis_app` only; `expires_at` = now() + 15 min set inside the function, never by the caller; writes an `ENROL_CREATE` audit row; spec R7-2);
  - `gw.enrol_node(p_token text, p_public_key bytea, p_tailscale_identity text) RETURNS TABLE (node_id uuid, refusal text)`: success → `(new id, NULL)`; refusal → `(NULL, 'bad_token' | 'key_in_use' | 'identity_in_use')`, with a REJECTED audit row in the same transaction. It never raises for a refusal (spec R7-1);
  - `gw.open_connection(p_node_id uuid) RETURNS TABLE (conn_id bigint, refusal text)`: refusal `'unknown_node' | 'revoked'` (audited `CONNECT_REFUSED`); otherwise, in one transaction it closes any open connection of that node (`closed_at = now()`, reason `superseded`) and opens the new one. At most one open connection per node, enforced by a partial unique index `UNIQUE (node_id) WHERE closed_at IS NULL`;
  - `gw.close_connection(p_conn_id bigint, p_reason text) RETURNS boolean`: does **not** call `current_node`. Its own locks are node `FOR SHARE`, then the connection `FOR UPDATE`, and it checks the connection while holding them. It returns true and writes `DISCONNECT` when it closes; it returns false, with no second `DISCONNECT`, if the connection was already closed. Revocation doesn't prevent closing;
  - `gw.current_node(p_conn_id bigint) RETURNS uuid`: the fence, called first by every `conv` and `comm` function that takes a connection ID (Tasks 3 and 7). The four `gw` connection functions (`enrol_node`, `open_connection`, `close_connection`, `touch_connection`) do **not** call it (spec §4.1, R8-1). It returns the node only if the connection is open and the node is not revoked. Otherwise it raises `RAISE EXCEPTION 'connection_fenced' USING ERRCODE = 'JF001'` (spec R7-5), with no audit row of its own. The gateway also calls it directly for its 60 s revocation re-check;
  - `gw.touch_connection(p_conn_id bigint) RETURNS void`: the heartbeat. It does **not** call `current_node`. It takes node `FOR NO KEY UPDATE`, then the connection `FOR NO KEY UPDATE`, then updates `gw.node.last_seen_at` and `gw.node_connection.last_ping_at`, each `UPDATE … WHERE` repeating `revoked_at IS NULL` and `closed_at IS NULL`. If the connection is superseded, closed or revoked, it raises `connection_fenced` (`JF001`), so it can't update anything;
  - `gw.revoke_node(p_node_id uuid) RETURNS TABLE (applied boolean, refusal text)` (EXECUTE for `jarvis_app` only): `(true, NULL)`, or `(false, 'unknown_node' | 'already_revoked')`, audited either way;
  - `audit.event` accepts `entity_type = 'node'` and actions `ENROL_CREATE`, `ENROL`, `REVOKE`, `CONNECT_REFUSED`, `CONNECT`, `DISCONNECT` (spec R8-2):
    - **`CONNECT`** comes from `open_connection`, with details `node_id`, `conn_id` and `superseded_conn_id`;
    - **`DISCONNECT`** comes from `close_connection`, from `open_connection` for the connection it supersedes (reason `superseded`), and from `revoke_node` for each connection it closes (reason `revoked`), with details `node_id`, `conn_id` and `reason`.

    Each is written in the same transaction as the change, holds IDs and codes only, and heartbeats aren't audited;
  - **refused-enrolment audit rows** use the enrolment's ID as `entity_id` whenever the token matched an enrolment (used or expired), and the nil UUID only when no enrolment matches;
  - **argument checks:** `gw.enrol_node` refuses a `p_token` that isn't exactly the token length (26 base32 characters) with `invalid_parameter_value`, like the other argument checks;
  - **strict fencing (spec R8-1):** the locks and lock order in spec §4.1:

    | Function | Node row | Connection row(s) |
    |---|---|---|
    | `current_node` | `FOR SHARE` | `FOR SHARE` |
    | `touch_connection` | `FOR NO KEY UPDATE` | `FOR NO KEY UPDATE` |
    | `close_connection` | `FOR SHARE` | `FOR UPDATE` |
    | `open_connection` | `FOR UPDATE` | `FOR UPDATE` (the node's open connection, if any) |
    | `revoke_node` | `FOR UPDATE` | `FOR UPDATE` (all open connections) |

    Always node row first, then connection rows. `touch_connection` also repeats `closed_at IS NULL` and `revoked_at IS NULL` in both of its `UPDATE … WHERE` clauses, and raises `JF001` if either matches no row;
  - **gateway EXECUTE:** exactly these five: `gw.enrol_node`, `gw.open_connection`, `gw.close_connection`, `gw.touch_connection`, `gw.current_node` (spec R7-3);
  - **rollback:** `downgrade()` refuses, with a clear error and the schema left intact, if any `audit.event` row has `entity_type = 'node'`, or any `gw.node` or `gw.enrolment` row exists. Otherwise it removes everything 0008 added, including grants; the cluster-wide role stays (spec R7-6).
- Produces (Python), in `jarvis_server.dbcall` (spec R7-1):
  - `class Refused(Exception)` with `reason: str`, plus subclasses `EnrolmentRefused`, `RevokeRefused` and `ConnectRefused`
  - `class TransactionNotIdle(Exception)`
  - `audited_call(conn: psycopg.Connection, sql: LiteralString, params: Sequence[Any]) -> tuple[Any, ...]` (`LiteralString`, not `str`, so only literal SQL can be passed; the async twin takes the same type): raises `TransactionNotIdle` unless `conn.info.transaction_status` is IDLE; otherwise runs the call in `with conn.transaction():`, which commits on exit, and returns the row. Callers raise their `Refused` subclass **after** it returns.
  - `async audited_call_async(conn: psycopg.AsyncConnection, …)`: the same rule, for the gateway (Task 6)
  - `FENCED_SQLSTATE = "JF001"`
- Produces (Python), in `jarvis_server.nodes`:
  - `create_enrolment(conn, *, person_id: UUID, kind: str, name: str, core_fp: str, gw_fp: str) -> str` (calls `gw.create_enrolment` with the SHA-256 of a new 128-bit token; returns the enrolment code `"<token_b32>.<core_fp>.<gw_fp>"`)
  - `enrol(conn, *, token: str, public_key: bytes, tailscale_identity: str) -> UUID`: wraps `gw.enrol_node` through `audited_call`; raises `EnrolmentRefused(reason)` after the commit. Tests use it as the gateway role, and the gateway's async version in Task 6 follows the same rule.
  - `revoke(conn, node_id: UUID) -> None`: wraps `gw.revoke_node`; raises `RevokeRefused(reason)` after the commit
  - `parse_enrolment_code(code: str) -> tuple[str, str, str]`

- [ ] **Step 1: Write the failing tests**

```python
def test_enrol_node_once(app, gateway):
    code = nodes.create_enrolment(app, person_id=MANJ, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP)
    token, _, _ = nodes.parse_enrolment_code(code)
    node_id = nodes.enrol(gateway, token=token, public_key=PUB1, tailscale_identity="ts:pc")
    row = app.execute("SELECT person_id, kind FROM gw.node WHERE id=%s", (node_id,)).fetchone()
    assert row == (MANJ, "DESKTOP")

def test_token_reuse_refused_and_audited(...):           # second nodes.enrol raises EnrolmentRefused('bad_token'); then a
                                                         #   SEPARATE connection sees the audit REJECTED row (R7-1, Python boundary)
def test_expired_token_refused(...):                     # expires_at moved to the past by owner → EnrolmentRefused('bad_token');
                                                         #   audit row visible from a separate connection
def test_enrol_node_returns_structured_refusal(gateway): # direct SQL: second use → (NULL, 'bad_token'), no exception raised
def test_audited_call_refuses_open_transaction(gateway): # inside `with gateway.transaction():` → TransactionNotIdle; no row written
def test_refusal_audit_survives_caller_exception(...):   # wrapper raises after commit; the caller's own `try/except` does a
                                                         #   rollback() → audit row still present from a separate connection
def test_create_enrolment_is_audited(app, owner):        # ENROL_CREATE audit row; expires_at = now()+15 min set by the database
def test_app_cannot_insert_enrolment_directly(app):      # INSERT INTO gw.enrolment → InsufficientPrivilege (only via the function)
def test_revoke_returns_explicit_result(app):            # first → applied; second → RevokeRefused('already_revoked'), audited
def test_fenced_error_code_is_jf001(gateway):            # current_node on a closed connection → SQLSTATE JF001, not 42501
def test_gateway_execute_surface_is_exactly_five(owner): # has_function_privilege over pg_proc: only the five gw functions
def test_concurrent_use_of_one_token_one_wins(...):      # two threads, two connections; exactly one node row
def test_enrol_never_updates_existing_node(...):         # reuse PUB1 with a new token → EnrolmentRefused('key_in_use'), audited;
                                                         #   that new token is still unused; node 1 unchanged
def test_gateway_cannot_insert_node_directly(gateway):   # INSERT INTO gw.node → InsufficientPrivilege
def test_gateway_cannot_read_enrolment(gateway):         # SELECT FROM gw.enrolment → InsufficientPrivilege
def test_gateway_cannot_revoke(gateway):                 # SELECT gw.revoke_node(...) → InsufficientPrivilege
def test_gateway_cannot_change_node_key(gateway):        # P2-S6: UPDATE gw.node SET public_key=… → InsufficientPrivilege
def test_gateway_cannot_change_node_identity(gateway):   # P2-S6: UPDATE gw.node SET tailscale_identity=… → InsufficientPrivilege
def test_gateway_cannot_unrevoke_or_repoint(gateway):    # P2-S6: UPDATE gw.node SET revoked_at=NULL / person_id=… / kind=… → denied
def test_touch_connection_changes_only_last_seen(gateway, app):  # P2-S6: every other gw.node column identical before and after
def test_revoke_wins_heartbeat_refused(dsns):          # R8-1 order 1: revoke_node holds its transaction open; touch_connection in
                                                         #   a thread blocks; revoke commits → touch raises JF001; last_seen_at and
                                                         #   last_ping_at unchanged (the reviewer's probe, as a test)
def test_heartbeat_wins_revoke_waits(dsns):              # R8-1 order 2: touch_connection holds its transaction open; revoke_node in
                                                         #   a thread blocks (verified via pg_locks/pg_stat_activity wait); touch
                                                         #   commits → revoke completes; node revoked, connection closed 'revoked'
def test_supersede_wins_write_refused(dsns):             # open_connection(new) first, then a fenced write via current_node(old) → JF001
def test_write_wins_supersede_waits(dsns):               # current_node(old) held in an open transaction; open_connection(new) blocks
                                                         #   until it commits, then completes
def test_no_deadlock_under_mixed_load(dsns):             # 8 threads × 200 random calls of touch / close / open / revoke / current_node
                                                         #   on 2 nodes → no deadlock error (40P01), final state consistent
def test_connect_disconnect_audited(app, gateway):       # open → CONNECT {node_id, conn_id, superseded_conn_id: null}; second open
                                                         #   → DISCONNECT {old, 'superseded'} + CONNECT {new, superseded_conn_id: old};
                                                         #   close → DISCONNECT {reason}; revoke → REVOKE + DISCONNECT 'revoked'; no
                                                         #   audit row from touch_connection; details hold only IDs and codes
def test_connection_audit_outlives_connection_rows(owner):  # delete gw.node_connection rows (as owner, simulating retention) →
                                                         #   CONNECT/DISCONNECT rows remain
def test_refused_enrolment_audit_uses_enrolment_id(...): # used or expired token → entity_id = enrolment id; unknown token → nil UUID
def test_token_length_checked(gateway):                  # 25 or 27 chars → invalid_parameter_value; no audit row
def test_gw_connection_functions_do_not_call_current_node(owner):  # prosrc of open/close/touch/enrol contains no gw.current_node
def test_close_connection_twice_is_noop(app, gateway):   # second close → false; exactly one DISCONNECT audit row
def test_close_after_revoke_allowed(app, gateway):        # revoke closes the connection; a later close → false, no error
def test_heartbeat_from_superseded_connection_rejected(gateway, app):  # conn1 superseded by conn2 → touch_connection(conn1)
                                                         #   raises connection_fenced; last_seen_at unchanged
def test_security_definer_functions_have_fixed_search_path(owner):
    rows = owner.execute("""SELECT p.oid::regprocedure, p.proconfig FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef AND n.nspname IN ('gw','conv','comm','ledger','ops','audit')""").fetchall()
    assert rows and all(cfg and "search_path=pg_catalog, pg_temp" in cfg for _, cfg in rows)
def test_no_function_executable_by_public(owner):       # no SECURITY DEFINER function grants EXECUTE to PUBLIC
```

The two catalogue tests are named `P2-S6-*`. They cover every later migration too, so Tasks 3 and 4 inherit them automatically.

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_nodes.py tests/test_acceptance.py::test_p0_6_migrations_go_down_and_up_cleanly -v`
Expected: FAIL (`gw` schema missing; `bootstrap()` signature)

- [ ] **Step 2b: Add the rollback tests**

```python
def test_0008_down_and_up_without_node_rows():           # fresh database: downgrade to 0007 → no gw schema; upgrade → same schema
def test_0008_downgrade_refuses_once_a_node_exists(...): # enrolment or node or node audit row → downgrade raises; gw schema intact
def test_p0_6_migrations_go_down_and_up_cleanly(...):    # existing P0-6 test: its schema fingerprint now includes 'gw'
```

- [ ] **Step 3: Implement the bootstrap role, migration 0008, `dbcall.py`, `nodes.py` and the CLI**

**Interim key source:** `node enrol` takes the core fingerprint from secret `core_signing_key` and the gateway fingerprint from `gateway_signing_key.pub` (the gateway's raw public key) in the secrets directory. Both are read in one function, `core_main._key_fingerprints()`. Task 14 supplies `gateway_signing_key.pub`, and Task 12 replaces this source with `gw.trusted_key`.

**Not deployable on its own:** after this commit, `init` needs the `pg_gateway_password` secret, which only arrives with the compose and deployment task (Task 14). No deploy happens before Task 14.

`gw.enrol_node` follows §7.2 step 4 exactly, returning a structured refusal rather than raising (spec R7-1):
- **Consume the token and register the node together:** in one `BEGIN … EXCEPTION WHEN unique_violation … END` block, a single `UPDATE gw.enrolment SET used_at = now() WHERE token_hash = sha256(p_token) AND used_at IS NULL AND expires_at > now() RETURNING …`, then the node insert, using `person_id` and `kind` from the enrolment row.
- **Zero rows from the `UPDATE`:** write the REJECTED audit row and **return `(NULL, 'bad_token')`**.
- **A public-key or active-identity conflict** (the `unique_violation` handler): the block's token update is rolled back with it, so the token stays unused. Then write the REJECTED audit row and **return `(NULL, 'key_in_use')` or `(NULL, 'identity_in_use')`**, chosen by the violated constraint's name.
- **Never `RAISE` for any of these refused requests.** The audit row is written outside the rolled-back block, in the function's own transaction, so it survives. The Python wrapper (`nodes.enrol`, through `dbcall.audited_call`) commits that transaction and then raises `EnrolmentRefused(reason)` (spec R7-1).
- **Success:** write the `ENROL` audit row, set `gw.enrolment.node_id`, and return `(new node id, NULL)`.

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS, including the existing 487

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: gateway role, node registry, one-time enrolment (migration 0008)"
```

---

### Task 3: Conversations, events, outbox and delivery (migration 0009)

**Files:**
- Create: `server/migrations/versions/0009_conv_comm.py`
- Create: `server/src/jarvis_server/comm.py` (core-side writers)
- Test: `server/tests/test_comm_db.py`

**Interfaces:**
- Produces (SQL):
  - schema `conv`, with `conv.session` (partial unique index `UNIQUE (person_id) WHERE closed_at IS NULL`), `conv.inbound` (including `outcome text NULL`, which may be `ANSWERED`, `INDETERMINATE` or `REFUSED`), `conv.message`, `conv.stream_chunk` and `conv.session_ticket`, as in §5.2;
  - schema `comm`, with `comm.event` (append-only trigger), `comm.outbox` and `comm.delivery`, as in §5.3.
- Produces (SQL functions):
  - `conv.record_inbound(p_conn_id bigint, p_message_id uuid, p_kind text, p_payload jsonb) RETURNS TABLE (recorded boolean, refusal text)` (gateway EXECUTE): derives the node from `gw.current_node(p_conn_id)`, so a superseded or closed connection is **fenced** (raises `JF001`, spec R7-5); a duplicate `message_id` **from the same node** returns `(false, NULL)` with no audit row; one already recorded **by a different node** returns `(false, 'id_conflict')` with a REJECTED audit row (spec R9-4); fills `person_id` from `gw.node`; enqueues the fixed job kind by `p_kind`: `utterance` → `conv.turn`, `approval.decision` → `approval.decision`, `action.receipt` → `action.receipt`; any other kind returns `(false, 'unknown_kind')` with a REJECTED audit row (spec R7-1); NOTIFYs `jarvis_jobs`;
  - `comm.record_sent(p_conn_id bigint, p_outbox_id uuid) RETURNS boolean`, which is false if the item is terminal; a fenced connection raises `JF001` (gateway);
  - `comm.record_received(p_conn_id bigint, p_outbox_id uuid) RETURNS text` (gateway). It returns the resulting state, or `'unknown_item'` (audited) when the item is unknown or belongs to another person (spec R9-5). `comm.acknowledge` and `record_sent` refuse the same way (`record_sent` returns false);
  - `comm.acknowledge(p_conn_id bigint, p_outbox_id uuid) RETURNS text` (gateway; NOTIFY `jarvis_outbox` with the id);
  - `conv.record_chunk_received(p_conn_id bigint, p_reply_id uuid, p_seq int) RETURNS TABLE (recorded boolean, refusal text)` (gateway): sets `conv.stream_cursor.acked_seq = greatest(acked_seq, p_seq)` for `(reply_id, node)`. If the chunk's `session_id` belongs to a person other than the node's, it returns `(false, 'other_person')` with a REJECTED audit row, and creates or changes no cursor row (spec R9-1). A chunk that doesn't exist still raises `invalid_parameter_value` here; Task 7 turns that case into a no-op;
  - **`conv.stream_chunk.session_id`** (NOT NULL, FK → `conv.session`) is a new column, written by core (Tasks 10 and 11);
  - **durable stream cursor:** `conv.stream_cursor(reply_id uuid, node_id uuid, acked_seq int NOT NULL DEFAULT 0, PRIMARY KEY (reply_id, node_id))`. The gateway may SELECT it, and changes it only through the function above. It's deleted with its chunks after 24 h.

  **All gateway-facing functions take `p_conn_id`, never a node ID,** and get the node through `gw.current_node`, so a fenced connection can't record anything.

  **The Task 2 mixed-load test is tightened in this task** (`tests/test_nodes.py::test_no_deadlock_under_mixed_load`, owner ruling):
  - after every `JF001`, the test asserts that the connection really was unknown (including connection ID 0), closed, or its node revoked, checked from a separate connection after the call;
  - a revoke refusal is accepted only as `already_revoked`;
  - an open-connection refusal is accepted only when the node is revoked;
  - nothing else is tolerated.

  It must fail if a valid connection is fenced; prove that with a temporary mutation, recorded in the report.

  **Refusals follow spec R7-1:** an audited refusal is returned, never raised; fencing raises `JF001`. Python callers use `dbcall.audited_call` from Task 2.

  **Strict fencing (spec R8-1):** every function above that takes `p_conn_id` calls `gw.current_node(p_conn_id)` **first**, before any other read or write, in the same transaction. It holds its share locks to commit, and takes no other lock on `gw.node` or `gw.node_connection` (which keeps the single lock order). No function checks the connection and then writes in a separate transaction.
  - `comm.supersede(p_outbox_id uuid) RETURNS text` and `comm.expire(p_outbox_id uuid) RETURNS text` (app; NOTIFY `jarvis_outbox`): `'applied'`, or `'terminal'` when the item was already terminal, with a REJECTED audit row (spec R9-3);
  - **`comm.event.details`:** a strict `CHECK` (`jsonb_typeof(details) = 'object'` and every value a string, using a strict jsonpath, for example `NOT jsonb_path_exists(details, 'strict $.* ? (@.type() != "string")')`). It rejects arrays, nested objects, numbers, booleans and nulls (spec R9-2).

  Each function locks the row with `FOR UPDATE`, moves the state forward only, and writes an audit row.
- Produces (Python), in `jarvis_server.comm`:
  - `record_event(conn, kind: str, *, subject_type: str, subject_id: str, person_id: UUID | None, details: dict[str, str]) -> UUID`
  - `create_outbox(conn, *, person_id: UUID, event_id: UUID, priority: Priority, kind: str, body: dict[str, Any], group_key: str, expires_at: datetime | None, supersedes_id: UUID | None = None, authz: dict[str, Any] | None = None) -> UUID` (supersedes the referenced item in the same transaction; NOTIFYs `jarvis_outbox`)
  - `class Priority(StrEnum)`: URGENT, TODAY, DIGEST, SILENT
  - `class OutboxState(StrEnum)`: PENDING, DELIVERING, DELIVERED, ACKNOWLEDGED, SUPERSEDED, EXPIRED

- [ ] **Step 1: Write the failing tests**

```python
def test_inbound_duplicate_is_noop(gateway, app):            # same message_id twice from the same node → True then (False, NULL);
                                                             #   one ops.job; no audit row
def test_inbound_id_conflict_across_nodes(gateway, app):     # message_id recorded by node A, then sent by node B → (False,
                                                             #   'id_conflict'); REJECTED audit row visible from a separate connection
def test_chunk_receipt_for_other_persons_reply_refused(...): # two persons: B's node records receipt for A's reply → (False,
                                                             #   'other_person'); REJECTED audit row; A's and B's cursor rows unchanged
                                                             #   (none created for B)
def test_outbox_unknown_or_other_person_refused(...):        # record_received / acknowledge on an unknown id and on another person's
                                                             #   item → 'unknown_item', audited; record_sent → False, audited
def test_supersede_and_expire_report_outcome(app):           # first call → 'applied'; on a terminal item → 'terminal' + REJECTED audit
def test_event_details_strict(app):                          # {"k":"v"} accepted; {"k":["x"]}, {"k":[]}, {"k":{"a":"b"}}, {"k":1},
                                                             #   {"k":true}, {"k":null}, [] and "x" rejected by the CHECK
def test_inbound_person_comes_from_node_row(gateway, app):   # payload claims another person; row has node's person (P2-S2)
def test_inbound_unknown_kind_refused(gateway):              # kind 'hello' → (false, 'unknown_kind'); no conv.inbound row;
                                                             #   the REJECTED audit row is visible from a separate connection
def test_event_is_append_only(app):                          # UPDATE/DELETE comm.event → raises
def test_outbox_states_only_move_forward(gateway, app):      # ack then record_received → state stays ACKNOWLEDGED
def test_record_sent_refuses_terminal(gateway, app):         # supersede then record_sent → False
def test_ack_on_one_node_ends_delivery(gateway, app):        # node A ack → ACKNOWLEDGED; record_sent for B → False (P2-E05)
def test_retry_keeps_id_and_counts_attempts(gateway, app):   # record_sent twice for (item,A) → one delivery row, attempts=2 (P2-E04)
def test_superseded_and_expired_remain_readable(app):        # both still SELECTable with state (P2-E06)
def test_delivery_race_is_consistent(gateway, app):          # P2-E13 (database half): 1000 runs, 2 nodes, random interleaving of
                                                             #   sent/received/ack/supersede across threads; final state ∈ legal
                                                             #   set; no forward-rank violation in audit order. The display-once
                                                             #   half is in Task 7.
def test_revoke_wins_inbound_refused(dsns):                  # R8-1 order 1 for conv.record_inbound: revoke commits while
                                                             #   record_inbound waits → JF001; no conv.inbound row, no job
def test_inbound_wins_revoke_waits(dsns):                    # R8-1 order 2: record_inbound's transaction open → revoke_node waits,
                                                             #   then completes; the inbound row exists and was recorded before REVOKE
def test_supersede_wins_ack_refused(dsns):                   # same pair of orders for comm.acknowledge (one representative delivery
def test_ack_wins_supersede_waits(dsns):                     #   function); the others share the current_node call, checked by test below
def test_every_conn_function_calls_current_node_first(owner):# each conv/comm function taking p_conn_id (pg_proc.prosrc) calls
                                                             #   gw.current_node before any other statement; the four gw connection
                                                             #   functions are excluded by name and checked in Task 2
def test_fenced_connection_refused_everywhere(gateway, app): # open conn1, open conn2 for the same node → conn1 is closed
                                                             #   'superseded'; record_inbound / record_sent / record_received /
                                                             #   acknowledge / record_chunk_received with conn1 → SQLSTATE JF001
                                                             #   (connection_fenced); nothing recorded
def test_chunk_cursor_only_moves_forward(gateway, app):      # received seq 5 then 3 → acked_seq 5
def test_gateway_cannot_write_conv_message_or_ledger(gateway):  # INSERT conv.message, ledger.task → denied
def test_gateway_cannot_call_ledger_transition(gateway):    # P2-S1: SELECT ledger.transition(...) → InsufficientPrivilege
def test_gateway_cannot_write_audit(gateway):               # P2-S1: INSERT INTO audit.event … → denied; SELECT audit.record_memory(...)
                                                             #   and every other audit.* function → InsufficientPrivilege
def test_gateway_has_no_memory_function_or_table(gateway, owner): # P2-S1: the gateway role has no privilege on any object
                                                             #   in schemas other than those in §4.1 (checked via has_*_privilege
                                                             #   over pg_class/pg_proc). The volume side is in Task 14.
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_comm_db.py -v`
Expected: FAIL (schemas missing)

- [ ] **Step 3: Implement migration 0009 and `comm.py`**

Table grants exactly as in §4.1: the gateway gets SELECT on `conv.stream_chunk`, `comm.outbox` and `comm.delivery`, and EXECUTE on the functions above. The app role gets what core needs.

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: conversations, append-only events, outbox and multi-node delivery (migration 0009)"
```

---

### Task 4: Approvals and brain budget (migration 0010)

**Files:**
- Create: `server/migrations/versions/0010_approvals_budget.py`
- Test: `server/tests/test_approval_db.py`, `server/tests/test_budget_db.py`

**Interfaces:**
- Produces (SQL):
  - `ledger.approval` (columns in §5.5), with a **backstop** trigger enforcing the decision guards. It raises `check_violation` on an illegal write and writes no audit row (spec R10-2);
  - `ledger.consume_approval(p_approval_id uuid) RETURNS TABLE (execution_key uuid, refusal text)` (app EXECUTE only; SECURITY DEFINER):
    - **success:** `(key, NULL)` with a `CONSUME` audit row;
    - **refusal:** `(NULL, 'unknown_approval' | 'not_approved' | 'expired' | 'already_consumed')`, each with a REJECTED audit row in the same transaction. It never raises for a refusal (spec R10-1, R7-1);
  - `audit.event` gains `entity_type = 'approval'`, if it doesn't exist yet, and the action `CONSUME`. The contract enums and `contracts/audit-event.schema.json` are updated to match;
  - **rollback:** `downgrade()` refuses, leaving the schema intact, if any `ledger.approval`, `ops.brain_budget`, `ops.brain_reservation` or `ops.brain_usage` row, or any approval audit row, exists. Otherwise it removes everything 0010 added. There is a down/up test, and the P0-6 fingerprint covers the new tables;
  - `ops.brain_budget`, `ops.brain_reservation` (including `turn_id uuid NULL`, with the partial unique index `UNIQUE (turn_id) WHERE state IN ('ACTIVE','SETTLED','LAPSED')`) and `ops.brain_usage`, as in §5.6.

  All of these are app only, with no gateway grant.

- [ ] **Step 1: Write the failing tests**

```python
def test_decision_only_from_pending_before_expiry(app):    # APPROVED after expires_at → raises (P2-E11)
def test_consume_once(app):                                # first → (uuid, None) + CONSUME audit; second → (None, 'already_consumed')
                                                           #   + REJECTED audit row
def test_consume_refused_unless_approved_and_in_date(app):  # PENDING / DENIED / EXPIRED → (None, 'not_approved'); APPROVED but
                                                           #   past expires_at → (None, 'expired'); unknown id → (None,
                                                           #   'unknown_approval'); each audited
def test_consume_refusal_audit_survives_python_boundary(dsns):  # through dbcall.audited_call: wrapper raises after commit; a separate
                                                           #   connection sees the REJECTED row
def test_concurrent_consume_one_wins(dsns):                # two connections in threads (propagating thread errors) → exactly one
                                                           #   (key, None) and one (None, 'already_consumed'); the second call's
                                                           #   wait on the row lock is proven via pg_stat_activity
def test_trigger_is_backstop(owner_or_app):                # direct UPDATE setting execution_key, or changing decision from
                                                           #   APPROVED to DENIED → check_violation; no audit row
def test_0010_down_and_up_and_refusal(...):                # empty → down/up clean; any approval or budget row → downgrade refuses
def test_reservation_unique_per_turn_except_released(app): # ACTIVE then second ACTIVE same turn → UniqueViolation;
                                                           #   RELEASED then ACTIVE same turn → allowed
def test_gateway_has_no_access(gateway):                   # SELECT ledger.approval / ops.brain_budget → denied (P2-S1)
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_approval_db.py tests/test_budget_db.py -v`
Expected: FAIL

- [ ] **Step 3: Implement migration 0010**

`ledger.consume_approval` is the SQL in §9.4, wrapped in a function.

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: approvals with single consumption, brain budget tables (migration 0010)"
```

---

### Task 5: Core job dispatch, notification rules and reconnect summary

**Files:**
- Create: `server/src/jarvis_server/core/__init__.py`, `core/dispatch.py`, `core/rules.py`, `core/summary.py`
- Modify: `server/src/jarvis_server/core_main.py` (replace `_noop` with `dispatch.handle`; LISTEN `jarvis_jobs` to wake the poll loop early)
- Test: `server/tests/test_rules.py`, `server/tests/test_summary.py`, `server/tests/test_dispatch.py`

**Interfaces:**
- Consumes: `comm.record_event` and `comm.create_outbox` (Task 3); `jobs.claim` and `jobs.complete` (existing)
- Produces:
  - `dispatch.register(kind: str) -> Callable[[Handler], Handler]` (decorator), `dispatch.handle(conn, job: jobs.Job) -> None` (an unknown kind raises `UnknownJob`)
  - `rules.notify(conn, event_kind: str, *, person_id: UUID, event_id: UUID, context: Mapping[str, Any]) -> list[UUID]`: applies the §8.1 table and returns the outbox ids
  - `summary.reconnect_summary(conn, person_id: UUID) -> list[dict[str, Any]]`: the §8.3 ordering, built from fixed templates with no brain import (enforced by a test that greps the module's imports)
  - job kind `system.event`, payload `{event_id, event_kind, person_id, context}` → `rules.notify`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("event,expected", [
    ("approval.requested", Priority.URGENT),           # 10-min expiry → URGENT (§8.1, C1)
    ("approval.expiring", Priority.URGENT),
    ("approval.expired", Priority.TODAY),
    ("brain.down", Priority.DIGEST), ("brain.down.with_held", Priority.TODAY),
    ("brain.back", Priority.DIGEST),
    ("budget.80", Priority.TODAY), ("budget.100", Priority.URGENT),
    ("turn.cap_expired", Priority.TODAY), ("turn.indeterminate", Priority.TODAY),
    ("action.readonly_ok", Priority.SILENT),
    ("brain.config_error", Priority.TODAY),
])
def test_rule_priority(app, event, expected): ...

def test_brain_back_supersedes_down(app): ...
def test_approval_requested_over_an_hour_is_today(app):      # expiry 2 h → TODAY (Phase 3 branch)
def test_summary_order(app):                                 # mixed items → URGENT first one-by-one, TODAY grouped by
                                                             #   group_key, DIGEST one line per group, SILENT absent (P2-E07)
def test_summary_needs_no_brain():                           # 'jarvis_server.brain' not imported by core.summary (P2-E08)
def test_server_initiated_event_creates_outbox_without_node(app):  # enqueue system.event; run dispatch; outbox row (P2-E01)
def test_unknown_job_kind_fails_job(app): ...
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_rules.py tests/test_summary.py tests/test_dispatch.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `dispatch.py`, `rules.py` and `summary.py`, and wire `core_main.serve`**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: core dispatch, deterministic notification rules, reconnect summary"
```

---

### Task 6: Gateway, part 1 — handshake, enrolment, inbound, fake node

**Files:**
- Create: `server/src/jarvis_server/gateway/__init__.py`, `gateway/app.py`, `gateway/tailscale.py`, `gateway/session.py`, `gateway/keys.py`
- Create: `server/tests/fakes/fake_node.py`
- Modify: `server/pyproject.toml` (add `fastapi` and `uvicorn[standard]`; script `jarvis-gateway = "jarvis_server.gateway.app:main"`)
- Test: `server/tests/test_gateway_handshake.py`

**Interfaces:**
- Consumes: `signing.*` (Task 1); `gw.*` functions (Task 2); `conv.record_inbound` (Task 3)
- Produces:
  - `class WhoIs(Protocol): async def identity(self, peer_ip: str) -> str | None`; `class LocalApiWhoIs(WhoIs)` (Tailscale local API over the shared socket at `JARVIS_TS_SOCKET`); `class FakeWhoIs(WhoIs)` (dict-backed, tests only)
  - `create_app(*, dsn: str, whois: WhoIs, gw_key: Ed25519PrivateKey, gw_key_id: str, core_public_keys: Mapping[str, bytes], bind_check: bool = True) -> FastAPI`. The WebSocket route is `/v1/node`.
  - `gateway.app.main() -> int`: asks the sidecar for its tailnet IPv4 address and binds uvicorn only to `<that address>:8443`. It exits non-zero if the address is unknown, and never binds `0.0.0.0` (§4.2).
  - `FakeNode(client: TestClient, *, key: Ed25519PrivateKey, node_id: UUID | None, peer_ip: str, state_path: Path)` with these methods: `enrol(code: str) -> UUID`, `hello() -> dict`, `send(kind: str, payload: dict) -> UUID`, `recv(timeout: float = 2.0) -> dict`, `received(outbox_id)`, `ack(outbox_id)`, `close()`. It signs `node-msg` envelopes with an increasing `seq`, checks the gateway's `gw-transport` signature on everything it receives, and pins the keys from the enrolment code. It also:
    - applies `node_rules.check_node_action` (Task 1) to every `node.action`, recording rejections in `fake.rejected`;
    - keeps a **seen-list** (outbox IDs, plus the last shown `seq` per reply) in a JSON file passed as `state_path`, reloaded when a new `FakeNode` is built on the same path. It counts displays in `fake.displays: Counter[outbox_id]` and `fake.shown_chunks: dict[reply_id, list[int]]`, and acknowledges chunks with `received {reply_id, seq}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_challenge_is_first_message_and_signed(...):       # on connect, first message is challenge; gw-transport sig verifies
                                                             #   with the pinned gateway key; nonce 43 chars; expires_at = issued_at + 30 s
def test_nonce_unique_per_connection(...):                   # 50 connections → 50 distinct nonces
def test_hello_path_checks_unchanged_by_enrolment(...):      # hello for a registered node signed with a different key → 4401
                                                             #   bad_signature; revoked → revoked; whois ≠ row → identity_mismatch
def test_enrol_path_signed_by_other_key_refused(...):        # enrol whose envelope is signed by a key ≠ payload public_key → 4401
def test_enrol_with_registered_key_refused(...):             # payload public_key already belongs to a node → 4401 key_in_use
def test_enrol_identity_from_connection_not_message(...):    # extra 'tailscale_identity' in payload → schema error; node row
                                                             #   gets the whois identity
def test_hello_with_nil_node_id_refused(...):                # hello on path A with nil UUID → 4401
def test_enrol_welcome_carries_core_key_and_node_pins_it(...):  # FakeNode enrol → welcome.enrolment; check_enrolment_welcome ok;
                                                             #   later node.action signed by core accepted
def test_enrol_with_substituted_core_key(...):               # gateway configured with a core key ≠ the code's fingerprint →
                                                             #   FakeNode discards keys, stores no node_id, closes; no action run
def test_node_ignores_core_content_before_pinning(...):      # an approval card arriving before the enrolment block is processed
                                                             #   → refused
def test_seq_checked_per_direction(...):                     # gateway sends seq 1..5 while node sends 1..3 on one connection →
                                                             #   all accepted; a node message with seq continuing the gateway's
                                                             #   count (6) → 4401 bad_seq; FakeNode rejects a gateway message
                                                             #   with a gap
def test_hello_with_other_connections_nonce_refused(...):    # conn A's nonce sent in hello on conn B → closed 4401, audit bad_nonce
def test_hello_after_challenge_expiry_refused(...):          # injected clock +31 s → closed 4401, audit challenge_expired
def test_second_hello_on_same_connection_refused(...):       # nonce consumed: a replayed hello on the same socket → closed
def test_failed_first_reply_consumes_nonce(...):             # bad-signature hello, then a correct one on the same socket → closed
def test_message_before_hello_refused(...):                  # utterance as first message → closed 4401, audit not_first
def test_nil_node_id_only_for_challenge_and_enrol(...):      # nil UUID on utterance → error/closed
def test_clock_skew_recorded_not_trusted(...):               # hello with clock_skew_ms 120000 → event node.clock_skew; handshake proceeds
def test_node_refuses_challenge_from_unpinned_key(...):      # FakeNode with pinned key K1; gateway signs with K2 → node closes, sends nothing
def test_enrol_ends_with_welcome(gw_client, enrolment_code):  # challenge → FakeNode.enrol (pins the gateway key by the code's
                                                             #   gateway fingerprint) → welcome with an enrolment block; NO second
                                                             #   hello on this connection. Asserts: welcome.enrolment.node_id equals
                                                             #   the new gw.node row; fingerprint(core_public_key) == code's core
                                                             #   fingerprint; FakeNode pinned core_key_id → core_public_key as a CORE
                                                             #   key and now sends that node_id in envelopes; a following utterance
                                                             #   on the same connection is recorded as that node
def test_reconnect_after_enrolment_uses_hello(...):          # a new connection by the enrolled FakeNode → challenge → hello (path A)
                                                             #   → welcome with no enrolment block
def test_enrol_refuses_keys_not_matching_fingerprints(...):  # gateway configured with a different core key → node refuses
def test_unknown_tailscale_peer_closed(...):                 # FakeWhoIs returns None → close code 4401 (P2-S3)
def test_tailscale_identity_mismatch_closed(...):            # registered "ts:pc", whois "ts:other" → closed, audit CONNECT_REFUSED
def test_bad_signature_closes(...):                          # tampered payload → closed (P2-S3)
def test_replayed_seq_closes(...):                           # seq repeated → closed (P2-S3)
def test_revoked_node_refused_and_open_connection_closed(...):  # revoke → new hello refused; open conn closed within one recheck (P2-E10)
def test_utterance_recorded_and_job_enqueued(...):           # send('utterance') → conv.inbound row + ops.job conv.turn
def test_resend_same_message_id_is_noop(...):
def test_oversized_message_gets_error_and_stays_open(...):  # 70 KB payload → kind 'error', code 'too_large'; next valid message works
def test_malformed_and_unknown_kind_get_error(...):          # invalid JSON / kind 'bogus' / missing field → 'error'; no row
def test_gateway_never_binds_all_interfaces():               # main() with no tailnet address → exit code != 0; never calls uvicorn with 0.0.0.0
```

`gw_client` is a fixture that builds `create_app(dsn=dsns.gateway, whois=FakeWhoIs(...), …)` and wraps it in Starlette's `TestClient`.

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_gateway_handshake.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the gateway handshake, enrolment, inbound recording, size and shape checks, the revocation re-check loop (60 s, made shorter for tests), and `FakeNode`**

The gateway connects to the database as `jarvis_gateway` only, using a psycopg `AsyncConnectionPool`.

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: gateway handshake, enrolment, inbound recording; fake node for tests"
```

---

### Task 7: Gateway, part 2 — streaming relay, outbox delivery, duplicate connections

**Files:**
- Create: `server/src/jarvis_server/gateway/relay.py`
- Modify: `gateway/app.py`, `gateway/session.py`
- Test: `server/tests/test_gateway_delivery.py`

**Interfaces:**
- Consumes: `conv.stream_chunk` and `comm.*` (Task 3); `summary.reconnect_summary` (Task 5)
- Produces:
  - **Stream relay, with a durable cursor:**
    - The gateway LISTENs on `jarvis_stream` (payload: `reply_id`). For each connected node in the session it sends chunks with `seq > conv.stream_cursor.acked_seq` for that `(reply_id, node)`, read from the database, not from process memory.
    - The node acknowledges with `received {reply_id, seq}` → `conv.record_chunk_received`.
    - **Cursor rows are never an authority (spec R9-1).** Reconnect never derives reply IDs from `conv.stream_cursor` rows. It selects candidate replies only from the reconnecting node's own person's sessions (`conv.stream_chunk.session_id` → `conv.session.person_id`), checked independently, and only then reads the cursor for the progress of those replies. A `resume` entry naming any other reply is ignored, as already specified.
    - **A late acknowledgement for a pruned chunk** (deleted after 24 h) is a harmless no-op. This task changes `conv.record_chunk_received` so that a chunk which doesn't exist returns `(false, NULL)` with no audit row, instead of raising `invalid_parameter_value`.
    - **Typed results at the boundary.** The gateway's wrappers for `comm.record_received` and `comm.acknowledge` map the text result at once: a state name becomes `OutboxState`, and `'unknown_item'` raises `OutboxRefused('unknown_item')` after the commit. Raw text never travels further.
    - **On (re)connect**, the gateway re-sends unacknowledged chunks of the node's session replies from the last 10 minutes. For each reply it **validates the node's `resume` entry first**, then picks the start:

      | `resume` entry for this reply | Start at |
      |---|---|
      | absent | `acked_seq + 1` |
      | `last_seq` < 0 | treated as 0 → `acked_seq + 1` |
      | 0 ≤ `last_seq` ≤ `max_seq_in_db(reply_id)` | `max(acked_seq, last_seq) + 1` |
      | `last_seq` > `max_seq_in_db(reply_id)` | **entry ignored** → `acked_seq + 1`; event `node.resume_out_of_range` |
      | unknown `reply_id`, or a reply outside the node's sessions | **entry ignored**; nothing sent for it; event `node.resume_out_of_range` |

      Events hold codes and IDs only. There is no clamping to the maximum, so a node's own state can never make the gateway skip a chunk that exists.
    - The node drops any `(reply_id, seq)` it has already shown, so a chunk sent twice is shown once.
    - ACTION chunks go only to `target_node_id`, and only while `now() <= valid_until`.
    - An in-memory "last sent" value may exist to avoid busy re-sends, but it is only an optimisation. Correctness depends on the cursor alone.
  - **Outbox relay:** the gateway LISTENs on `jarvis_outbox`, runs the 60 s re-send sweep, and sends `outbox.cleared` on terminal states.
  - **Welcome:** carries `{session_id, summary: reconnect_summary(...)}`.
  - **Duplicate connection rule:** `gw.open_connection` fences the old connection in the database (Task 2), with strict ordering (spec R8-1). Every delivery (`record_sent`) and every inbound record for a socket runs in its own transaction, which calls the fenced function **before** the frame is written to the socket. A frame is written only if that call succeeded. The gateway then closes the old socket with reason `superseded`. Any message still arriving on the old socket, or any delivery the gateway tries through it, fails with `connection_fenced`. The gateway drops it and stops that socket's tasks.

- [ ] **Step 1: Write the failing tests**

```python
def test_stream_resumes_after_gateway_restart(...):     # chunks 1..3 sent and acked; app instance discarded (no memory
                                                        #   kept); new app; node reconnects; chunks 4..5 inserted →
                                                        #   node gets 4,5 only; shown_chunks == [1,2,3,4,5] (P2-E03)
def test_unacked_chunks_resent_after_restart(...):      # chunks 1..3 sent, node acked only 2; restart → node gets 3 again
                                                        #   but shows it once
def test_resume_beyond_stored_max_does_not_skip(...):   # 3 chunks stored, acked_seq 1; node resume last_seq 99 →
                                                        #   delivered seqs == [2, 3]; exactly one comm.event
                                                        #   node.resume_out_of_range for this reply_id and node
def test_resume_within_range_used(...):                 # 3 stored, acked 1, resume last_seq 2 → delivered == [3]; no event
def test_resume_negative_treated_as_zero(...):          # 3 stored, acked 1, resume last_seq -5 → delivered == [2, 3]; no event
def test_resume_for_foreign_reply_ignored(...):         # resume names another person's reply → nothing sent for it; event written
def test_reconnect_never_uses_cursor_rows_as_authority(...):  # a cursor row exists for (A's reply, B's node) planted by the owner;
                                                        #   B reconnects → nothing of A's reply is sent
def test_ack_for_pruned_chunk_is_noop(...):             # chunk deleted by retention; node acks it → (False, NULL), no audit, no error
def test_outbox_results_are_typed(...):                 # wrapper returns OutboxState; 'unknown_item' → OutboxRefused
def test_core_restart_mid_stream(...):                  # core writes chunks 1..2 then dies (lease expiry); sweep runs →
                                                        #   node keeps 1..2, gets no final chunk, gets one
                                                        #   'turn.indeterminate' outbox item; brain called once (P2-E03, P2-B4)
def test_action_chunk_only_to_target_and_before_valid_until(...):  # other node gets nothing; expired chunk never sent
def test_outbox_delivered_to_all_person_nodes(...):     # two FakeNodes; both receive the same outbox_id
def test_ack_on_a_clears_on_b(...):                     # A acks → B receives outbox.cleared reason ACKNOWLEDGED (P2-E05)
def test_offline_node_sees_acked_item_only_in_history(...):  # B offline during ack; B reconnects; summary excludes it
def test_resend_after_two_minutes_same_id(...):         # clock advanced; sweep → same outbox_id re-sent (P2-E04)
def test_outbox_survives_restart(...):                  # item PENDING; restart gateway; node connects → receives it (P2-E03)
def test_second_connection_supersedes_first(...):       # same node connects twice → first closed 'superseded';
                                                        #   pending item delivered once in total to that node  [Review Focus 3]
def test_revoke_during_delivery(...):                   # R8-1 end to end: revoke_node committed before record_sent → the item is
                                                        #   not written to the socket; record_sent committed first → revoke waits,
                                                        #   then the socket is closed within one revocation re-check
def test_frame_written_only_after_fenced_record(...):   # injected failure (JF001) in record_sent → no frame reaches the FakeNode
def test_old_socket_is_fenced(...):                     # after supersede, the old FakeNode sends an utterance, an ack and a
                                                        #   received → none recorded (no conv.inbound row, state unchanged);
                                                        #   no outbox.item is written to the old socket
def test_display_once_per_node_across_retry_and_reconnect(...):  # P2-E13 (node half): 2 FakeNodes with state files;
                                                        #   200 randomised runs of re-send sweep, disconnect/reconnect,
                                                        #   ack on the other node, supersede → for every node and outbox_id,
                                                        #   fake.displays[id] <= 1, and no node displays an item after
                                                        #   receiving outbox.cleared for it. The same scenario script is
                                                        #   written as a message trace (tests/fakes/traces/e13.jsonl) for
                                                        #   Plan B to replay against the Rust node
def test_welcome_summary_order(...):                    # P2-E07 end to end
def test_three_missed_heartbeats_close_connection(...): # node stops answering ping → closed after 3 × 30 s (injected clock);
                                                        #   gw.node_connection closed_at set, reason 'heartbeat'
```

Time-dependent tests use an injectable `now()` in `relay.py`, not `sleep`.

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_gateway_delivery.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `relay.py` and the session registry**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: gateway stream relay, outbox delivery, re-send sweep, duplicate connections"
```

---

### Task 8: Brain interface, Claude client with connect/send phases

**Files:**
- Create: `server/src/jarvis_server/brain/__init__.py`, `brain/types.py`, `brain/claude.py`, `brain/fake.py`
- Modify: `server/pyproject.toml` (add `httpx`)
- Test: `server/tests/test_brain_claude.py`

**Interfaces:**
- Produces:
  - `Turn(role: Literal['person','jarvis'], text: str)`, `MemoryLine(memory_id: str, text: str, drives_action: bool)`, `ToolSpec(name: str, description: str, input_schema: dict[str, Any])`, all frozen dataclasses in `brain/types.py`
  - `BrainRequest(system: str, history: list[Turn], utterance: str, screen_summary: str, memory: list[MemoryLine], tools: list[ToolSpec], max_tokens: int)`
  - `BrainEvent = Speech(text: str) | ToolCall(name: str, args: dict[str, Any])`
  - `class Brain(Protocol): def think(self, req: BrainRequest) -> Iterator[BrainEvent]`
  - `class CallPhase(StrEnum)`: CONNECT, SEND
  - `class BrainCallFailed(Exception)` with `phase: CallPhase` and `status: int | None`
  - `ClaudeBrain(api_key: str, model: str, *, proxy: str, client: httpx.Client | None = None)`: raises `BrainCallFailed(phase=CONNECT)` **only** for `httpx.ConnectError`, `httpx.ConnectTimeout` and `httpx.ProxyError`. Every other exception or non-2xx status, including a stream cut, raises `BrainCallFailed(phase=SEND)` (§10.1 step 4). It also exposes `usage: tuple[int, int] | None` after the stream ends.
  - `count_input_tokens(req: BrainRequest) -> int` (local, conservative: UTF-8 bytes ÷ 3, rounded up)
  - `FakeBrain(script: list[BrainEvent | BrainCallFailed])` for tests

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.ProxyError("x")])
def test_connect_phase_failures(exc):        # MockTransport raising exc → BrainCallFailed.phase == CONNECT
def test_tls_handshake_failure_is_connect_phase(tmp_path):  # a real local TLS server (self-signed cert, client verifying
                                             #   against the system store), reached by a real httpx.Client (no mock) →
                                             #   BrainCallFailed.phase == CONNECT, and the server saw zero HTTP request bytes
def test_tls_ok_then_server_closes_before_response_is_send_phase(tmp_path):  # handshake succeeds, server reads the request
                                             #   then closes → phase SEND
@pytest.mark.parametrize("status", [400, 401, 429, 500, 529])
def test_http_status_is_send_phase(status):  # → phase SEND, status recorded
def test_read_timeout_is_send_phase(): ...
def test_stream_cut_is_send_phase(): ...     # SSE ends without message_stop → SEND
def test_unclassified_exception_is_send(): ...  # RuntimeError from transport → SEND
def test_streams_speech_and_tool_calls(): ...   # recorded SSE fixture → [Speech, Speech, ToolCall]; usage set
def test_max_tokens_always_sent(): ...          # request body contains max_tokens == req.max_tokens
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_brain_claude.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `types.py`, `claude.py` (Messages API, streaming, tool use, through `proxy`) and `fake.py`**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: brain interface and Claude client with connect/send phase classification"
```

---

### Task 9: Budget reservations and brain health

**Files:**
- Create: `server/src/jarvis_server/brain/budget.py`, `brain/health.py`, `brain/prices.py`
- Create: `server/config/prices.toml` (model → input and output paise per million tokens; values left to the builder per §17, with a comment pointing to the source)
- Test: `server/tests/test_budget.py`, `server/tests/test_brain_health.py`

**Interfaces:**
- Consumes: the budget tables (Task 4); `rules.notify` (Task 5); `CallPhase` (Task 8)
- Produces, in `brain.budget`:
  - `class Owner(StrEnum)`: RESERVED, OWNED_ELSEWHERE, ANSWERED, INDETERMINATE, NO_BUDGET
  - `reserve(conn, *, job_id: int, turn_id: UUID | None, reserve_paise: int, lease_until: datetime, now: datetime) -> tuple[Owner, UUID | None]`. In one transaction it locks the `conv.inbound` row (if `turn_id` is given), then the month's `ops.brain_budget` row, in that order. It then applies the second-job table in §10.1 step 6 and the cap and daily-ceiling checks in step 2. A `UniqueViolation` is returned as OWNED_ELSEWHERE.
  - `settle(conn, reservation_id: UUID, *, actual_paise: int, usage: tuple[int, int], model: str) -> None`
  - `release(conn, reservation_id: UUID) -> None` (allowed only from ACTIVE)
  - `lapse_expired(conn, now: datetime) -> int`: for each expired ACTIVE reservation, in one transaction it lapses the reservation at its full value, sets `conv.inbound.outcome = 'INDETERMINATE'` if empty, writes event `turn.indeterminate` and calls `rules.notify`. It returns the count.
  - `daily_ceiling(budget_row, now_ist: datetime) -> int`
  - `ist_month_day(now: datetime) -> tuple[date, date]`
- Produces, in `brain.prices`: `cost_paise(model: str, input_tokens: int, output_tokens: int) -> int`. It raises `UnknownModelPrice` for a model not in the table.
- Produces, in `brain.health`: `record_failure(conn) -> bool` (true when the brain has just gone DOWN), `record_success(conn) -> bool` (true when it has just come back), `is_down(conn) -> bool`. State is kept in `ops.heartbeat` under the name `brain`.
- Job kinds: `brain.probe` and `brain.reservation_sweep`

- [ ] **Step 1: Write the failing tests**

```python
def test_twenty_concurrent_reservations_three_fit(dsns):   # P2-B3: 20 threads, room for 3 → exactly 3 RESERVED
def test_reserve_locks_turn_then_budget(app):               # second job, same turn, ACTIVE in lease → OWNED_ELSEWHERE
@pytest.mark.parametrize("existing,expected", [
    (None, Owner.RESERVED), ("RELEASED", Owner.RESERVED),
    ("ACTIVE_IN_LEASE", Owner.OWNED_ELSEWHERE), ("ACTIVE_EXPIRED", Owner.INDETERMINATE),
    ("LAPSED", Owner.INDETERMINATE), ("SETTLED", Owner.ANSWERED),
])
def test_second_job_table(app, existing, expected): ...    # §10.1 step 6 table, row by row
def test_lapse_counts_full_value_and_notifies_once(app):   # sweep twice → one LAPSED, settled += reserved, one outbox item
def test_sweep_and_late_job_race_notify_once(dsns):        # P2-B5 tail: sweep and a job concurrently → one notification
def test_80_and_100_percent(app):                          # P2-B2 thresholds → events budget.80 / budget.100
def test_daily_ceiling(app):                               # remaining 3000, 10 days left → ceiling 300
def test_ist_day_and_month_boundary():                     # 2026-11-30T18:40Z (= 00:10 IST 1 Dec) → month Dec, day 1 Dec  [Review Focus 1]
def test_last_day_ceiling_is_whole_remainder(): ...        # [Review Focus 1]
def test_unknown_model_price_fails_closed():               # cost_paise("nope", …) raises UnknownModelPrice  [Review Focus 2]
def test_down_after_two_failures_back_after_success(app): ...
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_budget.py tests/test_brain_health.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `budget.py`, `prices.py` and `health.py`, and register the two job kinds with dispatch**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: atomic per-turn budget reservations, IST day/month, lapse sweep, brain health"
```

---

### Task 10: Conversation turn

**Files:**
- Create: `server/src/jarvis_server/core/turn.py`, `core/sessions.py`, `core/prompt.py`, `core/identity.py`
- Test: `server/tests/test_turn.py`

**Interfaces:**
- Consumes: `brain.*` (Tasks 8 and 9); `comm.*` (Task 3); `MemoryEngine.retrieve_context` (existing); `policy.decide_tool_call` (Task 11, interface only: in this task a stub that refuses everything, replaced in Task 11)
- Produces:
  - `identity.memory_slug(engine: MemoryEngine, person_id: UUID) -> str` (the person's slug from `engine.people()`; raises `UnknownPerson`)
  - `sessions.open_or_join(conn, person_id: UUID, now: datetime) -> UUID`: the 30 min idle rule (it closes an idle session, then opens a new one), and at most one open session per person (P2-E09). Concurrency-safe through the partial unique index from Task 3 and `INSERT … ON CONFLICT DO NOTHING`, followed by a re-select.
  - `prompt.build(conn, engine, *, session_id: UUID, person_id: UUID, utterance: str, screen_summary: str) -> BrainRequest` (last 20 messages; memory with `viewer = memory_slug(person_id)`; the tool menu = the desktop modules + `memory.write` + `pc.delete_test_file`, never `pc.list_test_folder`; it returns the memory IDs it used, for the policy check)
  - Job handler `conv.turn(conn, job)`:
    - reads `conv.inbound`;
    - computes the reservation with `prices.cost_paise`; `UnknownModelPrice` → no call, event `brain.config_error`, a TODAY notification, `outcome = 'REFUSED'`, and the job completes;
    - calls `budget.reserve`, and on anything but RESERVED follows the §10.1 table (on NO_BUDGET it holds: re-enqueues every 5 min, and after 1 h it expires with `turn.cap_expired`);
    - if the brain is DOWN, holds before reserving;
    - streams SPEECH chunks with NOTIFY `jarvis_stream`, each written with the turn's `session_id` (spec R9-1);
    - a CONNECT failure → `release`, then re-queue;
    - a SEND failure → `lapse` now, then the indeterminate path;
    - on success → `settle`, write the final chunk and the JARVIS message, and set `outcome = 'ANSWERED'`.
  - All writes for a turn use `Actor(PERSON, slug)` for things the person did, and `Actor(SYSTEM, "core")` for things Jarvis did.

- [ ] **Step 1: Write the failing tests**

```python
def test_turn_streams_and_stores_reply(app, engine):           # FakeBrain [Speech a, Speech b] → chunks seq 1,2,final; conv.message JARVIS;
                                                               #   every chunk carries the turn's session_id
def test_same_session_across_nodes(app):                       # node A then node B within 30 min → same session (P2-E09)
def test_new_session_after_30_min_idle(app): ...
def test_concurrent_first_messages_one_session(dsns):          # two threads call open_or_join for the same person at once
                                                               #   with no open session → both get the same session_id;
                                                               #   exactly one open conv.session row (index + ON CONFLICT)
def test_unknown_model_price_refuses_turn(app):                # config model not in prices.toml → FakeBrain never called;
                                                               #   no reservation; event 'brain.config_error'; one TODAY
                                                               #   outbox item; conv.inbound.outcome = 'REFUSED'  [Review Focus 2]
def test_memory_viewer_is_authenticated_person(app, engine):   # payload claims 'daughter'; retrieve_context called with viewer 'manj' (P2-S2)
def test_claude_down_holds_and_runs_once_when_back(app):       # P2-B1: CONNECT failures → DOWN; turn held; reminders job still runs;
                                                               #   probe ok → turn answered exactly once
def test_send_failure_is_indeterminate_no_second_call(app):    # P2-B4: FakeBrain [BrainCallFailed(SEND, 529)]; rerun job →
                                                               #   brain called once total; outcome INDETERMINATE; one notification
def test_crash_after_call_no_second_call(dsns):                # P2-B4: core killed after think() before settle (simulated by
                                                               #   raising after the call); lease expiry; retried job → no call
def test_two_jobs_same_turn_one_call(dsns):                    # P2-B5: two workers claim duplicate jobs concurrently → one call
def test_connect_failure_released_and_answered_once(app): ...  # P2-B4 positive branch
def test_cap_hold_expires_after_one_hour(app):                 # P2-B2: NO_BUDGET; clock +61 min → cap_expired notification; never calls
def test_cap_raised_within_hour_runs_once(app): ...
def test_server_runs_with_every_node_offline(app):             # P2-E12/E02: no gateway; turn + system events processed; outbox PENDING
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_turn.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `turn.py`, `sessions.py`, `prompt.py` and `identity.py`, and register `conv.turn`**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: conversation turn with sessions, memory retrieval, holds and indeterminate handling"
```

---

### Task 11: Policy, approvals, Atlas, live actions and receipts

**Files:**
- Create: `server/src/jarvis_server/policy.py`, `server/src/jarvis_server/atlas.py`, `core/approvals.py`, `core/actions.py`, `core/keys.py`
- Modify: `core/turn.py` (replace the policy stub)
- Test: `server/tests/test_policy.py`, `server/tests/test_approval_flow.py`

**Interfaces:**
- Consumes: `ledger.create_task`, `ledger.transition`, `ledger.consume_approval` (Task 4); `signing.make_authz` (Task 1); `MemoryEngine.assert_actionable`
- Produces:
  - `class Tier(StrEnum)`: READ_ONLY, REVERSIBLE, IRREVERSIBLE
  - `policy.TIERS: Mapping[str, Tier]`, exactly the §9.1 table: `module.news`, `module.live_tv` and `screen.*` are READ_ONLY; `memory.write` is REVERSIBLE; `pc.list_test_folder` is READ_ONLY; `pc.delete_test_file` is IRREVERSIBLE. Anything else is refused.
  - `policy.decide_tool_call(conn, *, person_id: UUID, session_id: UUID, node_id: UUID, node_kind: str, call: ToolCall, memory_ids: Sequence[str]) -> Decision`, with `Decision = Allow(tier) | NeedsApproval | Refuse(code: str)`. It runs the five checks in §9.2, in order.
  - `atlas.Atlas.accept_plan(conn, plan: ApprovedPlan, *, by: Actor) -> list[UUID]` and `ApprovedPlan(title: str, definition_of_done: str, source_ref: str, owner_person_id: UUID)`. One plan becomes one task (ADR-014).
  - `actions.issue_live(conn, *, reply_id: UUID, session_id: UUID, target_node_id: UUID, person_id: UUID, action_kind: str, args: dict, now: datetime) -> int`: writes a signed ACTION chunk with that `session_id` (spec R9-1) with `valid_until = now + 2 min`, NOTIFYs, and returns its `seq`.
  - `approvals.propose_delete(conn, *, session_id, person_id, node_id, name: str, now) -> UUID | None`: issues `pc.list_test_folder` and waits for its receipt (10 s); then creates the task (through Atlas) and the approval (expiry 10 min, `card_authz` signed); then the URGENT outbox card. It returns `None` if the file isn't listed.
  - Job handlers:
    - `approval.decision`: the §9.3 step 6 checks, then the decision, `consume_approval`, and the signed `node.action` outbox item, all in one transaction. A refusal from `consume_approval` (or from the §9.3 checks) is **committed with its audit row**, and the handler returns. It never raises inside that transaction (spec R10-1);
    - `action.receipt`: one receipt per key; the file ID and hash check; a fresh listing; then the outcome and close (§9.4);
    - `approval.expire` and `approval.expiring` (scheduled).
  - **`memory.write` from conversation (REVERSIBLE, §12):** `actions.memory_write(conn, engine, *, person_id, content: str, category: str, topics: Sequence[str]) -> str` writes a STATED entry with `by = memory_slug(person_id)` and `source = <inbound id>`, and creates a SILENT outbox item for history. Claude's arguments can never set `by`, `viewer` or the subject.
  - **Tickets linked to sessions:** `propose_delete` inserts `conv.session_ticket(session_id, task_id)` in the same transaction as the task (v2 §8.4).
  - **Creates `core/keys.py`** with only the temporary `active_core_key() -> tuple[Ed25519PrivateKey, str]`. It reads the single secret `core_signing_key`, with `key_id = fingerprint(public)`. Task 12 modifies this file and replaces the body, keeping the signature.

- [ ] **Step 1: Write the failing tests**

```python
def test_tier_table_exact(): assert policy.TIERS == EXPECTED_FROM_SPEC_9_1
def test_unknown_action_refused(...): ...
def test_inferred_memory_blocks_action(...):          # memory_ids containing an INFERRED entry → Refuse('memory_not_actionable')
# P2-A1 end to end with FakeNode acting as the desktop over the real gateway app:
def test_delete_flow_end_to_end(...):
    # utterance → FakeBrain ToolCall(pc.delete_test_file, name="a.txt") → node gets live pc.list_test_folder;
    # node replies with listing {a.txt, size, sha, file_id} → node gets URGENT approval card (authz valid, core key);
    # node sends approval.decision(APPROVED, card_sig) → node gets node.action with execution_key, valid_until ≤ +2 min;
    # node sends receipt RECYCLED with matching file_id+hash → core sends fresh listing; node lists without a.txt →
    # task COMPLETED, ledger.outcome verified, spoken result chunk; audit trail has every transition
def test_no_approval_refused(...):                    # P2-A2
def test_expired_approval_refused(...):               # P2-A3 (clock past expiry; decision → refused, approval EXPIRED)
def test_revoked_node_cannot_execute(...):            # P2-A4
def test_concurrent_decisions_one_action(dsns):       # P2-A5: two decision jobs → one node.action, one execution_key
def test_receipt_mismatch_fails_task(...):            # P2-A6: receipt with different file_id → task FAILED; nothing COMPLETED
def test_decision_with_wrong_card_sig_refused(...):   # P2-A9 (also: other person's node; already decided)
def test_action_signed_by_gateway_key_only_rejected_by_fake_node(...):  # P2-S4 server side of the node rule
def test_live_action_receipt_after_valid_until_refused(...):           # C3, core side
def test_atlas_one_plan_one_task(...): ...
def test_memory_write_uses_authenticated_person(...):  # ToolCall(memory.write, args incl. by='daughter') → entry by 'manj', STATED
def test_delete_proposal_links_ticket_to_session(...): # conv.session_ticket row exists
def test_fake_node_rejects_action_for_another_node(...):   # P2-S4: core-signed action with target_node_id = node B, sent to A
                                                           #   by a test-only path → A rejects, reason 'other-target-node'
def test_fake_node_rejects_expired_action(...):           # P2-S4: valid_until 31 s in the past → rejected 'expired'
def test_gateway_refuses_to_send_expired_action(...):     # C3 gateway side: chunk past valid_until is never written to a socket
```

The fake node implements the node-side rules in §7.1 (target, `valid_until`, core key) so that P2-S4 is exercised. Desktop-only cases (P2-A7, A8, A10) are in Plan B.

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_policy.py tests/test_approval_flow.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `policy.py`, `atlas.py`, `approvals.py`, `actions.py` and `keys.py`, and register the handlers**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: policy engine, approvals with signed cards, Atlas interface, live actions and verified receipts"
```

---

### Task 12: Key trust and rotation (core and gateway)

**Files:**
- Create: `server/migrations/versions/0011_trusted_keys.py`
- Modify: `server/src/jarvis_server/core/keys.py` (created in Task 11 with the temporary loader)
- Modify: `gateway/keys.py`, `gateway/app.py` (load the trusted key set from the database; sign with the ACTIVE gateway key)
- Modify: `server/src/jarvis_server/core_main.py` (`keys` subcommands)
- Test: `server/tests/test_key_rotation.py`

**Interfaces:**
- Produces (SQL):
  - `gw.trusted_key(key_id text PRIMARY KEY, role text CHECK (role IN ('CORE','GATEWAY')), public_key bytea UNIQUE, state text CHECK (state IN ('NEXT','ACTIVE','RETIRED')), announced_outbox_ids uuid[], created_at, activated_at, retired_at)`;
  - a partial unique index: one ACTIVE key per role;
  - the app role may write it, and the gateway may only SELECT it.
- Produces (Python), in `core.keys`:
  - `active_core_key() -> tuple[Ed25519PrivateKey, str]`: the private key comes from the secret file named for the ACTIVE CORE `key_id`; `key_id` = `fingerprint(public)`;
  - `announce(conn, *, role: Literal['CORE','GATEWAY'], public_key: bytes) -> str`: inserts a NEXT key and writes one `keyset` outbox item per active node. The item's `authz` (kind `keyset`, with `role`, `key_id`, `public_key`, `not_before`) is **always signed by the ACTIVE core key**, for both roles;
  - `activate_ready(conn, now: datetime) -> list[str]`: a NEXT key becomes ACTIVE, and the previous ACTIVE of that role becomes RETIRED-pending. That happens when every `keyset` item for it is ACKNOWLEDGED, or after 24 h, whichever is first. In the **same transaction**, every active node whose `keyset` item was **not** acknowledged is marked `gw.node.key_state = 'STALE'` (a new column, default `'CURRENT'`; app-writable, gateway SELECT only). Runs from the scheduler every minute;
  - `retire(conn, key_id: str) -> None`.
- **Offline-node recovery rule: a node that missed the overlap must re-enrol.**
  - **The 24-hour activation policy stays.** A node that is offline when the key activates can't be brought up to date over the gateway, because nothing the gateway sends is trustworthy to it: the gateway may be signing with a key the node never received, and an ordinary `welcome` (after `hello`) carries no keys by design. The one `welcome` that carries a key, the enrolment `welcome`, is pinned only against the fingerprint in a new out-of-band enrolment code (spec R6-6). Recovery therefore never asks the node to accept a key from the gateway alone: it goes through re-enrolment and a new code.
  - **The gateway refuses STALE nodes (spec R6-6).** Every connection first gets the pre-authentication `challenge`, which authorises nothing and carries no Jarvis content. After a STALE node's `hello` passes path A, the gateway closes with **4409** (`reenrol_required`) **instead of** `welcome`. It sends no welcome, summary, outbox item, chunk or action. The close code authorises nothing, so it is safe unsigned. If the gateway key itself rotated, the stale node can't verify the challenge and closes before `hello`.
    - The node treats 4409 as "stop and ask for re-enrolment". It runs nothing, shows "Jarvis's keys have changed. Please re-enrol this device", and keeps its local journal.
    - A forged 4409 can only deny service, which an attacker on the path could do anyway.
  - **A node with fresh keys but an unknown key_id** fails closed too. If a node that isn't STALE receives a `gw-transport` or `core-authz` signature with a `key_id` it has never pinned, it rejects the message, closes the connection and shows the same re-enrol prompt. This covers any gap between the database flag and the node's state.
  - **During the overlap**, a node that reconnects before activation gets its pending `keyset` items **first**, before the welcome summary, other outbox items or any action. So it learns the new key while the old one is still active.
  - **Operator procedure (runbook):**
    1. `jarvis-core node reenrol <node_id>`: in one transaction, revokes the old node and creates a fresh enrolment for the same person, kind and name. It prints a new enrolment code with the **current** core and gateway fingerprints.
    2. On the device, choose "Re-enrol" and enter the code. The device generates a new key pair and enrols as a new node. The pinned keys come from the code's fingerprints (out of band, from Manj), never from the gateway alone.
    3. The old node's unacknowledged outbox items are re-addressed to the person, so they appear on the new node through the normal "all nodes of the person" rule.
  - **`jarvis-core keys list`** shows the STALE nodes after each activation, so Manj knows which devices need step 1.
- CLI:
  - `jarvis-core keys announce --role CORE|GATEWAY --public-key-file <path>`: the new private key is placed by `harden.sh` as secret `core_signing_key_<key_id>` or `gateway_signing_key_<key_id>`;
  - `jarvis-core keys retire <key_id>`;
  - `jarvis-core keys list`.
- **Gateway behaviour:**
  - signs `gw-transport` with its ACTIVE GATEWAY key;
  - re-reads `gw.trusted_key` every 60 s and on startup;
  - refuses to start if its ACTIVE key's private secret file is missing;
  - **Where nodes get keys.** An ordinary `welcome` (after `hello`) carries no keys. A node learns keys only in two ways:
    - at **initial enrolment or re-enrolment**: the gateway key from the `challenge` and the core key from the enrolment `welcome` block, each pinned only if it matches the fingerprint in the enrolment code;
    - during **rotation**: from a core-signed, unexpired `keyset`.

    No other message changes a node's pinned keys.
- **Node behaviour (FakeNode, and Plan B):**
  - accepts a `keyset` only if its `authz` verifies with a pinned CORE key;
  - trusts both the old and the new key until a later `keyset` or 24 h after `not_before`;
  - accepts `gw-transport` from either GATEWAY key during that overlap;
  - on close code 4409, or on any signature with an unknown `key_id`: runs nothing, keeps its journal and prompts for re-enrolment (offline-node recovery rule, below).

- [ ] **Step 1: Write the failing tests**

```python
def test_core_rotation_end_to_end(...):            # announce CORE → FakeNode receives keyset, accepts it; after ack,
                                                    #   activate_ready → new key ACTIVE; next node.action signed with new
                                                    #   key_id → accepted; old key still accepted inside 24 h, rejected after
def test_gateway_rotation_end_to_end(...):         # announce GATEWAY → keyset signed by CORE key; node accepts; gateway
                                                    #   restarts and signs with new key → node accepts transport; old
                                                    #   gateway key rejected after overlap
def test_keyset_signed_by_gateway_key_rejected(...):   # forged keyset authz signed with the gateway key → node rejects
def test_keyset_from_untrusted_core_key_rejected(...): # signed by a key never pinned → rejected
def test_activation_waits_for_all_acks_or_24h(app): ...
def test_one_active_key_per_role(app):              # second ACTIVE CORE row → UniqueViolation
def test_gateway_cannot_write_trusted_keys(gateway):# INSERT/UPDATE gw.trusted_key → denied
def test_gateway_refuses_start_without_active_secret(...): ...
def test_core_rotation_node_offline_beyond_24h(...):   # node A online acks, node B offline; 24 h pass (injected clock) →
                                                       #   key ACTIVE, B marked STALE; B reconnects → receives exactly one
                                                       #   message (challenge), then close 4409 after its hello; no welcome,
                                                       #   outbox item, chunk or action; B runs nothing; B.prompt == 'reenrol'
def test_gateway_rotation_node_offline_beyond_24h(...):# same for a GATEWAY key; also: if B were not flagged, a
                                                       #   gw-transport signature with an unknown key_id makes B close and
                                                       #   prompt re-enrol (B never accepts the new gateway key from transport);
                                                       #   with a rotated gateway key, B closes on the challenge itself and
                                                       #   never sends hello
def test_reconnect_within_overlap_gets_keyset_first(...):  # B offline 2 h, others acked → not activated yet (waits for all
                                                       #   or 24 h); B reconnects → first message after welcome handshake
                                                       #   is the keyset; then activation proceeds
def test_reenrol_recovers_stale_node(...):             # node reenrol → old node revoked, new code with current
                                                       #   fingerprints; FakeNode re-enrols with a new key; receives the
                                                       #   old node's pending items; accepts actions signed by the new key
def test_reenrol_code_pins_from_fingerprints_not_gateway(...):  # gateway presents a key not matching the code's fingerprint
                                                       #   during re-enrolment → node refuses
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_key_rotation.py -v`
Expected: FAIL

- [ ] **Step 3: Implement migration 0011 (including `gw.node.key_state`); the rotation functions in `core/keys.py` (modify); the gateway key loading; the CLI; and the FakeNode keyset handling**

Replace Task 11's temporary loader with `active_core_key()`. Add `keys.activate` to the scheduler's job list (Task 13).

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: trusted key table, core and gateway key rotation via core-signed keyset"
```

---

### Task 13: Memory integration and scheduler jobs

**Files:**
- Modify: `server/src/jarvis_server/memory/engine.py` (add `set_visibility`)
- Modify: `server/migrations/versions/` (new `0012_memory_visibility_audit.py` only if `audit.action` needs `SET_VISIBILITY`)
- Modify: `server/src/jarvis_server/scheduler_main.py`
- Create: `server/src/jarvis_server/retention.py`
- Test: `server/tests/test_memory_visibility.py`, `server/tests/test_scheduler_jobs.py`, `server/tests/test_retention.py`

**Interfaces:**
- Produces:
  - `MemoryEngine.set_visibility(memory_id: str, visibility: Visibility, shared_with: Sequence[str] = (), *, by: str) -> None`: only the subject or the recorder; a new git version; audited (§12).
  - Scheduler enqueues these jobs (scheduler logic only enqueues):

    | Job | When |
    |---|---|
    | `approval.expiring` | 2 min before each approval's expiry |
    | `approval.expire` | at expiry |
    | `outbox.expire` | as items expire |
    | `brain.probe` | every 2 min while the brain is DOWN |
    | `brain.reservation_sweep` | every 1 min |
    | `outbox.resend_sweep` | every 60 s |
    | `retention.sweep` | daily at 03:30 IST |
    | `keys.activate` | every 1 min (Task 12) |

    Each is enqueued at most once per due item, deduplicated by a deterministic job key in the payload.
  - `retention.sweep(conn, now: datetime) -> dict[str, int]`: the §5.7 table; one audit row per table with the count.

- [ ] **Step 1: Write the failing tests**

```python
def test_set_visibility_by_subject_or_recorder_only(...): ...
def test_set_visibility_new_git_version_and_audit(...): ...
def test_scheduler_enqueues_each_due_item_once(app):   # two scheduler ticks → one approval.expire job per approval
def test_retention_deletes_only_past_window(app):      # chunk 25 h after final → deleted; 23 h → kept; messages never
def test_retention_writes_counts_to_audit(app): ...
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_memory_visibility.py tests/test_scheduler_jobs.py tests/test_retention.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `set_visibility`, the scheduler jobs and retention**

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 2: set_visibility, scheduled expiries and sweeps, retention"
```

---

### Task 14: Deployment, smoke and runbook

**Files:**
- Modify: `server/deploy/compose.yaml`
  - Add `gateway-ts` (`tailscale/tailscale@sha256:<digest>`, kernel mode, `/dev/net/tun`, `NET_ADMIN`, networks `jarvis_internal` and `jarvis_ts_egress`, volumes `gateway_ts_state` and `ts_socket`, secret `tailscale_gateway_authkey`, the entrypoint wrapper below).
  - Add `egress-proxy` (pinned by digest, networks `jarvis_internal` and `jarvis_egress`).
  - `gateway`: real command `jarvis-gateway`, `network_mode: service:gateway-ts`, secrets `pg_gateway_password` and `gateway_signing_key`, volume `ts_socket`, plus `JARVIS_TS_SOCKET`.
  - `core`: secrets `anthropic_api_key` and `core_signing_key`, `HTTPS_PROXY=http://egress-proxy:3128`.
  - `init`: secret `pg_gateway_password`.
- Create: `server/deploy/gateway-ts/entrypoint.sh` (installs the §4.2 OUTPUT rules for UID 10001, then `exec containerboot`)
- Create: `server/deploy/egress-proxy/` (config allowing only `CONNECT api.anthropic.com:443`)
- Modify: `server/deploy/harden/harden.sh` (create the new secret files with the same ownership and mode as the existing ones; generate the two Ed25519 keys; never print private keys)
- Modify: `server/src/jarvis_server/smoke.py`: **database-only** SMOKE lines that need no node, no gateway process and no brain. They run as `init`, using a fictional smoke node row registered through `gw.enrol_node` with a smoke token:

  | Row | Checks |
  |---|---|
  | `P2-E01` | A `system.event` creates an outbox row |
  | `P2-E04` | `record_sent` twice on one smoke connection: one delivery row, attempts = 2 |
  | `P2-E06` | Superseded and expired items readable |
  | `P2-E11` | Approval decision after expiry refused |
  | `P2-S1` | As the gateway role: `ledger.transition`, `audit.event` insert, `audit.record_memory` all denied |
  | `P2-S6-db` | Direct update of `gw.node` key or identity denied; SECURITY DEFINER `search_path` check |
  | `P2-B-idx` | Second ACTIVE reservation for one turn refused |
  | `P2-fence` | A superseded smoke connection is fenced, including `gw.touch_connection` |

  Rows that need the gateway, a node or a brain are **not** in init smoke: E02, E03, E05, E07, E09, E10, E12, E13, A*, B1 to B5, S2, S3, S4 and S6-enrolment. They run in CI with the fake node (Tasks 6 to 12). S5, S7 and P0-8 are live checks (Step 6).
- Modify: `server/deploy/README.md` (Tailscale auth key and ACL for `tag:jarvis-gateway:8443`; node enrol and revoke; core and gateway key rotation, `keys list` after activation, and the re-enrolment procedure for STALE nodes (Task 12); the gateway checks; the live checks below)
- Modify: `.github/workflows/server.yml` (a step that fails if `docker compose -f deploy/compose.yaml config` contains `ports:`)
- Test: `server/tests/test_compose.py`

**Interfaces:**
- Consumes: everything above. Produces the deployable stack.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_service_publishes_ports():          # parse compose.yaml → no service has 'ports'                     (P2-S5 static)
def test_gateway_shares_sidecar_namespace():    # gateway.network_mode == 'service:gateway-ts'
def test_core_has_no_egress_network():          # core networks == ['jarvis_internal']; HTTPS_PROXY set           (P2-S7 static)
def test_only_core_mounts_memory_and_api_key(): # memory volume and anthropic_api_key only on core
def test_all_third_party_images_pinned():       # gateway-ts and egress-proxy images contain '@sha256:'
def test_smoke_lists_phase2_rows():             # smoke.run() prints exactly the database-only rows in the table above
def test_smoke_imports_no_gateway_or_brain():   # jarvis_server.smoke imports neither jarvis_server.gateway nor jarvis_server.brain
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_compose.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the compose changes, the sidecar entrypoint, the proxy config, `harden.sh`, the smoke rows, the runbook and the CI step**

- [ ] **Step 4: Run the tests and the CI-equivalent checks**

Run: `cd server && uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy && JARVIS_REF=test docker compose -f deploy/compose.yaml config --quiet`
Expected: all PASS; compose valid

- [ ] **Step 5: Commit, then open the PR**

```bash
git commit -am "Phase 2: deploy gateway with Tailscale sidecar, egress proxy, smoke rows, runbook"
```

Open a PR to `main`. Merging and deploying wait for Manj's go-ahead, as in Phase 1.

- [ ] **Step 6: Live checks on the shared server (after Manj approves the deploy)**

Deploy through the Hostinger connector as in the runbook. Then check:
- the `init` logs show every SMOKE PASS line (the Phase 0 and Phase 1 rows plus the eight database-only P2 rows);
- **integration on the server:** from Manj's PC, a one-off run of the Python fake node over the tailnet (enrol with a fresh code, then hello, utterance, reply, ack) to check the real gateway path. Fictional text only;
- the `gateway` logs show it bound to its tailnet address only;
- **P2-S5 live:** Manj runs from the server over Tailscale SSH `docker compose exec gateway python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"`, and it must fail;
- **P2-S7 live:** the same from `core` against any host other than `api.anthropic.com`, which must fail, while a Claude probe through the proxy succeeds;
- **P0-8:** the external scan still shows only 22/80/443.

P2-A1 on the real desktop waits for Plan B.

---

### Task 15: Independent QA and security review

**Files:**
- Create: `.10x/reviews/2026-11-XX-phase-2-qa.md`, `.10x/reviews/2026-11-XX-phase-2-security.md`
- Modify: `.10x/status.md`, `.10x/handoff.md`, `.10x/decisions/*/phase-2-core-and-nodes.md`

- [ ] **Step 1: Run the full test map**

Check that every spec test ID is covered by a named test in this plan or is marked "Plan B" or "live":

| ID | Covered by (named tests) |
|---|---|
| P2-E01 to E12 | Tasks 3, 5, 6, 7 and 10 |
| P2-E13 | Task 3 (database half) and Task 7 (display-once half); the Rust half is Plan B, replaying `traces/e13.jsonl` |
| P2-A1 to A6, A9 | Task 11 |
| P2-A7, A8, A10 | Plan B |
| P2-B1 to B5 | Tasks 9 and 10 |
| P2-S1 | Task 3: `test_gateway_cannot_call_ledger_transition`, `test_gateway_cannot_write_audit`, `test_gateway_has_no_memory_function_or_table`; Task 14: `test_only_core_mounts_memory_and_api_key` |
| P2-S2 | Tasks 3 and 10 |
| P2-S3 | Task 6 |
| P2-S4 | Task 1: `test_node_rules_match_authz_cases`; Task 11: other-node and expired tests; Task 12: forged `keyset` tests |
| P2-S6 | Task 2: the five direct-update and enrolment tests |
| P2-S5, S7 | Task 14: static compose tests and live checks |

Every row must name real test functions that exist in the branch. A row that names only a task is a gap.

Fix any gap before review.

- [ ] **Step 2: Dispatch fresh QA and security reviewers** on the branch diff against `main`, with the spec. Fix the blocking and medium findings, and record the rest.

- [ ] **Step 3: Update status and handoff; commit**

```bash
git commit -am "Phase 2 server: reviews, status and handoff"
```
