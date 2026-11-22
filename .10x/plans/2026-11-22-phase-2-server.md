# Phase 2 server (Jarvis core and gateway) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the server side of Phase 2: the shared contracts and signing, the gateway, the event-driven core with conversations, the Claude router with budget reservations, policy and approvals, the Atlas interface, memory integration, and the deployment changes. All of it is proven against a Python fake node.

**Architecture:** A new `gateway` process (FastAPI WebSocket) and the existing `core` and `scheduler` processes communicate only through Postgres rows, with NOTIFY used only to wake them (ADR-011). All work enters as `ops.job` rows (ADR-013). The gateway holds no Claude key and no memory, and changes state only through SECURITY DEFINER functions.

**Tech Stack:** Python 3.12, uv, psycopg 3 (sync in core, async in the gateway), Alembic, FastAPI and uvicorn, `rfc8785`, `cryptography` (Ed25519), `httpx` (Claude), pytest, ruff, mypy; Postgres 17; Docker Compose; `tailscale/tailscale` sidecar.

**Spec:** `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md` (Revision 4, approved 2026-11-22). Section references (§) below point there. Read it alongside this plan.

**Out of this plan (separate plans):**
- **Plan B:** the Rust desktop node (spec §13).
- **Track D11:** the dedicated-server move (spec §19). It needs its own short design first.

This plan's tests use a Python fake node (Task 6) in place of the desktop.

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
- Modify: `contracts/node-envelope.schema.json` (add required `seq` (integer ≥ 1) and `sig` (string); remove the STUB note; `kind` becomes an enum of the 16 kinds below)
- Modify: `contracts/approval.schema.json` (add the §5.5 columns; remove the STUB note)
- Create: `contracts/messages/<kind>.schema.json`, one per kind: `enrol`, `hello`, `welcome`, `utterance`, `reply.chunk`, `outbox.item`, `outbox.cleared`, `received`, `ack`, `approval.decision`, `node.action`, `action.receipt`, `keyset`, `ping`, `pong`, `error` (payload shapes from the §7.1 table; `error` = `{code, detail}`)
- Create: `contracts/messages/authz.schema.json` (`{object, key_id, sig}`; `object` fields exactly as listed in §7.1)
- Create: `contracts/examples/messages/<kind>.valid.json` and `.invalid.json`
- Create: `contracts/signing-vectors/vectors.json`
- Create: `server/src/jarvis_server/signing.py`
- Modify: `server/src/jarvis_server/contracts.py` (pydantic models for each message and `Authz`; register them in a new `MESSAGE_MODELS` dict)
- Modify: `server/pyproject.toml` (add `rfc8785`, `cryptography`)
- Test: `server/tests/test_signing.py`, `server/tests/test_message_contracts.py`

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
  - `check_authz(authz: Mapping[str, Any], trusted: Mapping[str, bytes]) -> dict[str, Any]` (returns the object; raises `AuthzInvalid` if `key_id` isn't trusted or the signature fails)
  - `fingerprint(public_key: bytes) -> str` (the first 10 bytes of SHA-256, as lower-case base32 without padding: 16 characters)
  - `load_private_key(pem_path: Path) -> Ed25519PrivateKey`, `public_bytes(key: Ed25519PrivateKey) -> bytes` (raw 32 bytes)
- Produces, in `contracts.py`: `MESSAGE_MODELS: dict[str, type[BaseModel]]` and `Authz`

- [ ] **Step 1: Write the signing vectors file**

`contracts/signing-vectors/vectors.json` holds a list of cases: `{name, purpose, key_id, seed_hex, object, expected_canonical_utf8, expected_sig}`. It needs at least these cases:
- `ascii-simple`;
- `nested-keys-unsorted` (keys out of order at two levels);
- `kannada-and-emoji` (`"text": "ನಮಸ್ಕಾರ 🙏"`);
- `hindi`;
- `integers-only` (paise and sequence numbers);
- `same-object-two-purposes` (one object signed as `node-msg` and as `core-authz`, giving different signatures).

Generate `expected_*` once with the implementation in Step 3, then check one case by hand against `rfc8785` alone.

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
```

`test_message_contracts.py` follows the pattern in `test_contracts.py`:
- every `contracts/messages/*.schema.json` has a model in `MESSAGE_MODELS`;
- the schemas are valid Draft 2020-12;
- valid examples pass both the schema and the model, and invalid ones fail both.

- [ ] **Step 3: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_signing.py tests/test_message_contracts.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis_server.signing`)

- [ ] **Step 4: Implement `signing.py`, the schemas, the examples and the models**

`canonical` is `rfc8785.dumps`. `signed_bytes` concatenates exactly as in Global Constraints.

- [ ] **Step 5: Run the tests to check they pass**

Run: `cd server && uv run pytest tests/test_signing.py tests/test_message_contracts.py tests/test_contracts.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add contracts server/src/jarvis_server/signing.py server/src/jarvis_server/contracts.py server/pyproject.toml server/uv.lock server/tests/test_signing.py server/tests/test_message_contracts.py
git commit -m "Phase 2: final node envelope, message contracts, JCS signing and shared vectors"
```

---

### Task 2: Gateway role and node registry (migration 0008)

**Files:**
- Modify: `server/src/jarvis_server/bootstrap.py` (`GATEWAY_ROLE = "jarvis_gateway"`; `bootstrap(…, gateway_password: str)` creates it LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE, with CONNECT on the database)
- Modify: `server/tests/conftest.py` (a third password; `Dsns.gateway`; a `gateway` fixture connecting as `jarvis_gateway`)
- Modify: `server/src/jarvis_server/core_main.py` `_bootstrap()` and `config.py` (`gateway_dsn()`, which reads secret `pg_gateway_password`)
- Create: `server/migrations/versions/0008_gateway_nodes.py`
- Create: `server/src/jarvis_server/nodes.py` (core-side node admin)
- Modify: `server/src/jarvis_server/core_main.py` (subcommands `node enrol --person <slug> --kind DESKTOP|PHONE --name <text>` and `node revoke <node_id>`)
- Test: `server/tests/test_nodes.py`

**Interfaces:**
- Produces (SQL):
  - schema `gw`, with `gw.node`, `gw.node_connection` and `gw.enrolment` (columns as in §5.1 and §7.2);
  - unique on `gw.node.public_key`, and a partial unique on `tailscale_identity WHERE revoked_at IS NULL`;
  - `gw.enrol_node(p_token text, p_public_key bytea, p_tailscale_identity text) RETURNS uuid`;
  - `gw.open_connection(p_node_id uuid) RETURNS bigint`;
  - `gw.close_connection(p_conn_id bigint, p_reason text) RETURNS void`;
  - `gw.touch_node(p_node_id uuid) RETURNS void`;
  - `gw.revoke_node(p_node_id uuid) RETURNS void` (EXECUTE for `jarvis_app` only);
  - `audit.event` accepts `entity_type = 'node'` and actions `ENROL`, `REVOKE`, `CONNECT_REFUSED`.
- Produces (Python), in `jarvis_server.nodes`:
  - `create_enrolment(conn, *, person_id: UUID, kind: str, name: str, core_fp: str, gw_fp: str) -> str` (returns the enrolment code `"<token_b32>.<core_fp>.<gw_fp>"`; stores only the SHA-256 of the token)
  - `revoke(conn, node_id: UUID) -> None`
  - `parse_enrolment_code(code: str) -> tuple[str, str, str]`

- [ ] **Step 1: Write the failing tests**

```python
def test_enrol_node_once(app, gateway):
    code = nodes.create_enrolment(app, person_id=MANJ, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP)
    token, _, _ = nodes.parse_enrolment_code(code)
    node_id = gateway.execute("SELECT gw.enrol_node(%s,%s,%s)", (token, PUB1, "ts:pc")).fetchone()[0]
    row = app.execute("SELECT person_id, kind FROM gw.node WHERE id=%s", (node_id,)).fetchone()
    assert row == (MANJ, "DESKTOP")

def test_token_reuse_refused_and_audited(...):           # second call raises; audit REJECTED row exists
def test_expired_token_refused(...):                     # expires_at moved to the past by owner → raises
def test_concurrent_use_of_one_token_one_wins(...):      # two threads, two connections; exactly one node row
def test_enrol_never_updates_existing_node(...):         # reuse PUB1 with a new token → unique violation; node 1 unchanged
def test_gateway_cannot_insert_node_directly(gateway):   # INSERT INTO gw.node → InsufficientPrivilege
def test_gateway_cannot_read_enrolment(gateway):         # SELECT FROM gw.enrolment → InsufficientPrivilege
def test_gateway_cannot_revoke(gateway):                 # SELECT gw.revoke_node(...) → InsufficientPrivilege
def test_security_definer_functions_have_fixed_search_path(owner):
    rows = owner.execute("""SELECT p.oid::regprocedure, p.proconfig FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef AND n.nspname IN ('gw','conv','comm','ledger','ops','audit')""").fetchall()
    assert rows and all(cfg and "search_path=pg_catalog, pg_temp" in cfg for _, cfg in rows)
def test_no_function_executable_by_public(owner):       # no SECURITY DEFINER function grants EXECUTE to PUBLIC
```

The two catalogue tests are named `P2-S6-*`. They cover every later migration too, so Tasks 3 and 4 inherit them automatically.

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd server && uv run pytest tests/test_nodes.py -v`
Expected: FAIL (`gw` schema missing; `bootstrap()` signature)

- [ ] **Step 3: Implement the bootstrap role, migration 0008, `nodes.py` and the CLI**

`gw.enrol_node` follows §7.2 step 3 exactly:
- a single `UPDATE … SET used_at = now() WHERE token_hash = sha256(p_token) AND used_at IS NULL AND expires_at > now() RETURNING …`;
- on zero rows, audit REJECTED and `RAISE`;
- otherwise insert the node, using `person_id` and `kind` from the enrolment row.

- [ ] **Step 4: Run the tests to check they pass**

Run: `cd server && uv run pytest -v`
Expected: all PASS, including the existing 236

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
  - schema `conv`, with `conv.session`, `conv.inbound` (including `outcome text NULL`, which may be `ANSWERED` or `INDETERMINATE`), `conv.message`, `conv.stream_chunk` and `conv.session_ticket`, as in §5.2;
  - schema `comm`, with `comm.event` (append-only trigger), `comm.outbox` and `comm.delivery`, as in §5.3.
- Produces (SQL functions):
  - `conv.record_inbound(p_message_id uuid, p_node_id uuid, p_kind text, p_payload jsonb) RETURNS boolean` (gateway EXECUTE): returns false on a duplicate `message_id`; fills `person_id` from `gw.node`; enqueues the fixed job kind by `p_kind`: `utterance` → `conv.turn`, `approval.decision` → `approval.decision`, `action.receipt` → `action.receipt`; any other kind is refused; NOTIFYs `jarvis_jobs`;
  - `comm.record_sent(p_outbox_id uuid, p_node_id uuid) RETURNS boolean`, which is false if the item is terminal (gateway);
  - `comm.record_received(p_outbox_id uuid, p_node_id uuid) RETURNS text` (gateway);
  - `comm.acknowledge(p_outbox_id uuid, p_node_id uuid) RETURNS text` (gateway; NOTIFY `jarvis_outbox` with the id);
  - `comm.supersede(p_outbox_id uuid) RETURNS void` and `comm.expire(p_outbox_id uuid) RETURNS void` (app; NOTIFY `jarvis_outbox`).

  Each function locks the row with `FOR UPDATE`, moves the state forward only, and writes an audit row.
- Produces (Python), in `jarvis_server.comm`:
  - `record_event(conn, kind: str, *, subject_type: str, subject_id: str, person_id: UUID | None, details: dict[str, str]) -> UUID`
  - `create_outbox(conn, *, person_id: UUID, event_id: UUID, priority: Priority, kind: str, body: dict[str, Any], group_key: str, expires_at: datetime | None, supersedes_id: UUID | None = None, authz: dict[str, Any] | None = None) -> UUID` (supersedes the referenced item in the same transaction; NOTIFYs `jarvis_outbox`)
  - `class Priority(StrEnum)`: URGENT, TODAY, DIGEST, SILENT
  - `class OutboxState(StrEnum)`: PENDING, DELIVERING, DELIVERED, ACKNOWLEDGED, SUPERSEDED, EXPIRED

- [ ] **Step 1: Write the failing tests**

```python
def test_inbound_duplicate_is_noop(gateway, app):            # same message_id twice → True then False; one ops.job
def test_inbound_person_comes_from_node_row(gateway, app):   # payload claims another person; row has node's person (P2-S2)
def test_inbound_unknown_kind_refused(gateway):              # kind 'hello' → raises; no row
def test_event_is_append_only(app):                          # UPDATE/DELETE comm.event → raises
def test_outbox_states_only_move_forward(gateway, app):      # ack then record_received → state stays ACKNOWLEDGED
def test_record_sent_refuses_terminal(gateway, app):         # supersede then record_sent → False
def test_ack_on_one_node_ends_delivery(gateway, app):        # node A ack → ACKNOWLEDGED; record_sent for B → False (P2-E05)
def test_retry_keeps_id_and_counts_attempts(gateway, app):   # record_sent twice for (item,A) → one delivery row, attempts=2 (P2-E04)
def test_superseded_and_expired_remain_readable(app):        # both still SELECTable with state (P2-E06)
def test_delivery_race_is_consistent(gateway, app):          # P2-E13: 1000 runs, 2 nodes, random interleaving of
                                                             #   sent/received/ack/supersede across threads; final state ∈ legal
                                                             #   set; no forward-rank violation in audit order
def test_gateway_cannot_write_conv_message_or_ledger(gateway):  # P2-S1 part: INSERT conv.message, ledger.task → denied
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
  - `ledger.approval` (columns in §5.5), with a trigger enforcing the decision guards;
  - `ledger.consume_approval(p_approval_id uuid) RETURNS uuid`, which returns the execution key, or NULL if it can't be consumed (app);
  - `ops.brain_budget`, `ops.brain_reservation` (including `turn_id uuid NULL`, with the partial unique index `UNIQUE (turn_id) WHERE state IN ('ACTIVE','SETTLED','LAPSED')`) and `ops.brain_usage`, as in §5.6.

  All of these are app only, with no gateway grant.

- [ ] **Step 1: Write the failing tests**

```python
def test_decision_only_from_pending_before_expiry(app):    # APPROVED after expires_at → raises (P2-E11)
def test_consume_once(app):                                # first → uuid; second → None
def test_consume_refused_unless_approved_and_in_date(app):  # PENDING → None; DENIED → None; expired → None
def test_concurrent_consume_one_wins(dsns):                # two connections in threads → exactly one non-null key
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
- Create: `server/tests/fakes/__init__.py`, `server/tests/fakes/fake_node.py`
- Modify: `server/pyproject.toml` (add `fastapi` and `uvicorn[standard]`; script `jarvis-gateway = "jarvis_server.gateway.app:main"`)
- Test: `server/tests/test_gateway_handshake.py`

**Interfaces:**
- Consumes: `signing.*` (Task 1); `gw.*` functions (Task 2); `conv.record_inbound` (Task 3)
- Produces:
  - `class WhoIs(Protocol): async def identity(self, peer_ip: str) -> str | None`; `class LocalApiWhoIs(WhoIs)` (Tailscale local API over the shared socket at `JARVIS_TS_SOCKET`); `class FakeWhoIs(WhoIs)` (dict-backed, tests only)
  - `create_app(*, dsn: str, whois: WhoIs, gw_key: Ed25519PrivateKey, gw_key_id: str, core_public_keys: Mapping[str, bytes], bind_check: bool = True) -> FastAPI`. The WebSocket route is `/v1/node`.
  - `gateway.app.main() -> int`: asks the sidecar for its tailnet IPv4 address and binds uvicorn only to `<that address>:8443`. It exits non-zero if the address is unknown, and never binds `0.0.0.0` (§4.2).
  - `FakeNode(client: TestClient, *, key: Ed25519PrivateKey, node_id: UUID | None, peer_ip: str)` with these methods: `enrol(code: str) -> UUID`, `hello() -> dict`, `send(kind: str, payload: dict) -> UUID`, `recv(timeout: float = 2.0) -> dict`, `received(outbox_id)`, `ack(outbox_id)`, `close()`. It signs `node-msg` envelopes with an increasing `seq`, checks the gateway's `gw-transport` signature on everything it receives, and pins the keys from the enrolment code.

- [ ] **Step 1: Write the failing tests**

```python
def test_enrol_then_hello(gw_client, enrolment_code):       # FakeNode.enrol pins keys matching the fingerprints; hello → welcome
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
  - **Stream relay:** the gateway LISTENs on `jarvis_stream` (payload: `reply_id`) and sends chunks with `seq > last_sent[reply_id]` to the nodes in the session. ACTION chunks go only to `target_node_id`, and only while `now() <= valid_until`.
  - **Outbox relay:** the gateway LISTENs on `jarvis_outbox`, runs the 60 s re-send sweep, and sends `outbox.cleared` on terminal states.
  - **Welcome:** carries `{session_id, summary: reconnect_summary(...)}`.
  - **Duplicate connection rule:** a new connection for a `node_id` that already has one closes the old one with reason `superseded`.

- [ ] **Step 1: Write the failing tests**

```python
def test_stream_resumes_after_gateway_restart(...):     # chunks 1..3 sent; new app instance; node reconnects;
                                                        #   chunks 4..5 inserted → node gets 4,5 only (P2-E03)
def test_action_chunk_only_to_target_and_before_valid_until(...):  # other node gets nothing; expired chunk never sent
def test_outbox_delivered_to_all_person_nodes(...):     # two FakeNodes; both receive the same outbox_id
def test_ack_on_a_clears_on_b(...):                     # A acks → B receives outbox.cleared reason ACKNOWLEDGED (P2-E05)
def test_offline_node_sees_acked_item_only_in_history(...):  # B offline during ack; B reconnects; summary excludes it
def test_resend_after_two_minutes_same_id(...):         # clock advanced; sweep → same outbox_id re-sent (P2-E04)
def test_outbox_survives_restart(...):                  # item PENDING; restart gateway; node connects → receives it (P2-E03)
def test_second_connection_supersedes_first(...):       # same node connects twice → first closed 'superseded';
                                                        #   pending item delivered once in total to that node
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
  - `sessions.open_or_join(conn, person_id: UUID, now: datetime) -> UUID` (30 min idle rule, at most one open session per person; P2-E09)
  - `prompt.build(conn, engine, *, session_id: UUID, person_id: UUID, utterance: str, screen_summary: str) -> BrainRequest` (last 20 messages; memory with `viewer = memory_slug(person_id)`; the tool menu = the desktop modules + `memory.write` + `pc.delete_test_file`, never `pc.list_test_folder`; it returns the memory IDs it used, for the policy check)
  - Job handler `conv.turn(conn, job)`:
    - reads `conv.inbound`;
    - calls `budget.reserve`, and on anything but RESERVED follows the §10.1 table (on NO_BUDGET it holds: re-enqueues every 5 min, and after 1 h it expires with `turn.cap_expired`);
    - if the brain is DOWN, holds before reserving;
    - streams SPEECH chunks with NOTIFY `jarvis_stream`;
    - a CONNECT failure → `release`, then re-queue;
    - a SEND failure → `lapse` now, then the indeterminate path;
    - on success → `settle`, write the final chunk and the JARVIS message, and set `outcome = 'ANSWERED'`.
  - All writes for a turn use `Actor(PERSON, slug)` for things the person did, and `Actor(SYSTEM, "core")` for things Jarvis did.

- [ ] **Step 1: Write the failing tests**

```python
def test_turn_streams_and_stores_reply(app, engine):           # FakeBrain [Speech a, Speech b] → chunks seq 1,2,final; conv.message JARVIS
def test_same_session_across_nodes(app):                       # node A then node B within 30 min → same session (P2-E09)
def test_new_session_after_30_min_idle(app): ...
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
  - `keys.core_key() -> tuple[Ed25519PrivateKey, str]`, read from secret `core_signing_key` (`key_id` = `fingerprint`).
  - `actions.issue_live(conn, *, reply_id: UUID, target_node_id: UUID, person_id: UUID, action_kind: str, args: dict, now: datetime) -> int`: writes a signed ACTION chunk with `valid_until = now + 2 min`, NOTIFYs, and returns its `seq`.
  - `approvals.propose_delete(conn, *, session_id, person_id, node_id, name: str, now) -> UUID | None`: issues `pc.list_test_folder` and waits for its receipt (10 s); then creates the task (through Atlas) and the approval (expiry 10 min, `card_authz` signed); then the URGENT outbox card. It returns `None` if the file isn't listed.
  - Job handlers:
    - `approval.decision`: the §9.3 step 6 checks, then the decision, `consume_approval`, and the signed `node.action` outbox item, all in one transaction;
    - `action.receipt`: one receipt per key; the file ID and hash check; a fresh listing; then the outcome and close (§9.4);
    - `approval.expire` and `approval.expiring` (scheduled).
  - **`memory.write` from conversation (REVERSIBLE, §12):** `actions.memory_write(conn, engine, *, person_id, content: str, category: str, topics: Sequence[str]) -> str` writes a STATED entry with `by = memory_slug(person_id)` and `source = <inbound id>`, and creates a SILENT outbox item for history. Claude's arguments can never set `by`, `viewer` or the subject.
  - **Tickets linked to sessions:** `propose_delete` inserts `conv.session_ticket(session_id, task_id)` in the same transaction as the task (v2 §8.4).
  - **Core key rotation (§7.1):**
    - `jarvis-core keys rotate --new-key <secret name>` writes a `keyset` outbox item for every node. Its `authz` (kind `keyset`) is signed by the **current** core key and carries the new public key and `key_id`.
    - Core signs with the new key once the item is ACKNOWLEDGED on every active node, or after 24 h, whichever comes first.
    - `jarvis-core keys retire <key_id>` ends the overlap.
    - The gateway key rotates the same way, but its `keyset` is signed by core.

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
def test_keyset_signed_by_current_core_key(...):       # FakeNode accepts keyset; then accepts an action signed by the new key;
                                                       #   rejects a keyset signed by the gateway key
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

### Task 12: Memory integration and scheduler jobs

**Files:**
- Modify: `server/src/jarvis_server/memory/engine.py` (add `set_visibility`)
- Modify: `server/migrations/versions/` (new `0011_memory_visibility_audit.py` only if `audit.action` needs `SET_VISIBILITY`)
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

### Task 13: Deployment, smoke and runbook

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
- Modify: `server/src/jarvis_server/smoke.py` (SMOKE lines for P2-E01, E03, E04, E05, E11, S1, S2, S6; fictional people only)
- Modify: `server/deploy/README.md` (Tailscale auth key and ACL for `tag:jarvis-gateway:8443`; node enrol and revoke; core key rotation and re-enrolment; the gateway checks; the live checks below)
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
def test_smoke_lists_phase2_rows():             # smoke.run() output names each P2 row listed above
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
- the `init` logs show every SMOKE PASS line (the Phase 0 and Phase 1 rows plus the new P2 rows);
- the `gateway` logs show it bound to its tailnet address only;
- **P2-S5 live:** Manj runs from the server over Tailscale SSH `docker compose exec gateway python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"`, and it must fail;
- **P2-S7 live:** the same from `core` against any host other than `api.anthropic.com`, which must fail, while a Claude probe through the proxy succeeds;
- **P0-8:** the external scan still shows only 22/80/443.

P2-A1 on the real desktop waits for Plan B.

---

### Task 14: Independent QA and security review

**Files:**
- Create: `.10x/reviews/2026-11-XX-phase-2-qa.md`, `.10x/reviews/2026-11-XX-phase-2-security.md`
- Modify: `.10x/status.md`, `.10x/handoff.md`, `.10x/decisions/*/phase-2-core-and-nodes.md`

- [ ] **Step 1: Run the full test map**

Check that every spec test ID is covered by a named test in this plan or is marked "Plan B" or "live":

| ID | Covered by |
|---|---|
| P2-E01 to E13 | Tasks 3, 5, 6, 7 and 10 |
| P2-A1 to A6, A9 | Task 11 |
| P2-A7, A8, A10 | Plan B |
| P2-B1 to B5 | Tasks 9 and 10 |
| P2-S1 to S4, S6 | Tasks 2, 3, 4, 6 and 11 |
| P2-S5, S7 | Task 13: static checks and live checks |

Fix any gap before review.

- [ ] **Step 2: Dispatch fresh QA and security reviewers** on the branch diff against `main`, with the spec. Fix the blocking and medium findings, and record the rest.

- [ ] **Step 3: Update status and handoff; commit**

```bash
git commit -am "Phase 2 server: reviews, status and handoff"
```
