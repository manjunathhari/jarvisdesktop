"""On-server checks against the live database: P0-1 … P0-5 as the app role (`run`), and the
database-only Phase 2 rows as the app and gateway roles (`run_phase2`).

Run with `jarvis-core smoke` (init runs it on every deploy). P0 leaves a clearly labelled test
ticket and commitment behind (source_ref 'smoke-check'), which is fine: the ledger is
append-mostly and P0-7 reuses them.

The Phase 2 rows need no node, no gateway process and no brain (this module imports neither;
the rows that do need them run in CI with the fake node). They work on a fictional person,
"Smoke Fictional" (source_ref 'smoke-check'), and a fictional node enrolled through
`gw.enrol_node` with a fresh smoke token, whose private key is thrown away. They leave nothing
live: open smoke items are superseded and the node is revoked (closing its connections). The
expired approval and the reservations are written in transactions that are rolled back, and
the one job is claimed by the smoke itself, never by a running core.

| Row | Checks |
|---|---|
| P2-E01 | a `system.event` job creates an outbox item |
| P2-E04 | `comm.record_sent` twice on one connection: one delivery row, attempts = 2 |
| P2-E06 | superseded and expired items stay readable (by the gateway) |
| P2-E11 | an approval decision after expiry is refused |
| P2-S1 | the gateway may not call `ledger.transition` or `audit.record_memory`, or insert audit |
| P2-S6-db | no role may change a node's key or identity; SECURITY DEFINER functions are
  owned by jarvis_owner with the fixed search_path |
| P2-B-idx | a second ACTIVE reservation for one turn is refused |
| P2-fence | a superseded connection is fenced (JF001), `gw.touch_connection` included |
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.types.json import Jsonb

from jarvis_server import comm, config, jobs, ledger, nodes, signing
from jarvis_server.contracts import ActorKind, AuditOutcome, CommitmentState, EntityType, TaskState
from jarvis_server.core import dispatch
from jarvis_server.dbcall import FENCED_SQLSTATE
from jarvis_server.ledger import Actor, IllegalTransition

Conn = psycopg.Connection[Any]
Result = tuple[str, bool, str]

ACTOR = Actor(ActorKind.SYSTEM, "smoke-check")
MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
TASK_PATH = [
    TaskState.INTERPRETED,
    TaskState.PROPOSED,
    TaskState.APPROVAL_PENDING,
    TaskState.READY,
    TaskState.IN_PROGRESS,
    TaskState.COMPLETED,
]


def run(conn: psycopg.Connection[Any]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    corr = uuid.uuid4()

    task = ledger.create_task(
        conn,
        ACTOR,
        title="P0-1 test ticket",
        definition_of_done="Walks every main state",
        source_ref="smoke-check",
        correlation_id=corr,
    )
    for s in TASK_PATH:
        ledger.transition(conn, EntityType.TASK, task.id, s, ACTOR, f"smoke walk to {s}", corr)
    trail = ledger.audit_trail(conn, EntityType.TASK, task.id)
    ok = len(trail) == 7 and all(e.outcome is AuditOutcome.APPLIED for e in trail)
    results.append(("P0-1 ticket walk", ok, f"task {task.id}, {len(trail)} audit rows"))

    other = ledger.create_task(
        conn,
        ACTOR,
        title="P0-2 illegal move",
        definition_of_done="Stays DETECTED",
        source_ref="smoke-check",
    )
    try:
        ledger.transition(conn, EntityType.TASK, other.id, TaskState.COMPLETED, ACTOR, "skip")
        results.append(("P0-2 illegal transition", False, "was allowed"))
    except IllegalTransition:
        last = ledger.audit_trail(conn, EntityType.TASK, other.id)[-1]
        state = ledger.state_of(conn, EntityType.TASK, other.id)
        ok = state == "DETECTED" and last.outcome is AuditOutcome.REJECTED
        results.append(("P0-2 illegal transition", ok, f"state {state}, refusal audited"))

    try:
        with conn.transaction():
            conn.execute("DELETE FROM audit.event WHERE entity_id = %s", (task.id,))
        results.append(("P0-3 audit append-only", False, "delete succeeded"))
    except psycopg.errors.InsufficientPrivilege:
        results.append(("P0-3 audit append-only", True, "app role refused"))

    c = ledger.create_commitment(
        conn, ACTOR, person_id=MANJ, description="P0-4 test commitment", source_ref="smoke-check"
    )
    for cs in (CommitmentState.AT_RISK, CommitmentState.SECURED, CommitmentState.FULFILLED):
        ledger.transition(conn, EntityType.COMMITMENT, c.id, cs, ACTOR, f"smoke to {cs}")
    n = len(ledger.audit_trail(conn, EntityType.COMMITMENT, c.id))
    results.append(("P0-4 commitment lifecycle", n == 4, f"{n} audit rows"))

    now = datetime.now(UTC)
    tag = f"smoke-check-{corr.hex[:8]}"
    for summary, start, end in (
        ("expired", now - timedelta(days=2), now - timedelta(days=1)),
        ("live", now - timedelta(hours=1), now + timedelta(hours=1)),
    ):
        conn.execute(
            "INSERT INTO ledger.context (subject_person_id, kind, summary, valid_from, expires_at,"
            " source_ref) VALUES (%s, 'test', %s, %s, %s, %s)",
            (MANJ, summary, start, end, tag),
        )
    rows = conn.execute(
        "SELECT summary FROM ledger.current_context WHERE source_ref = %s", (tag,)
    ).fetchall()
    results.append(("P0-5 context expiry", [r[0] for r in rows] == ["live"], f"{rows}"))
    return results


# --- Phase 2, database only ------------------------------------------------------------------

SMOKE_PERSON = "Smoke Fictional"
SMOKE_REF = "smoke-check"
TERMINAL = ("ACKNOWLEDGED", "SUPERSEDED", "EXPIRED")
SCHEMAS = ["gw", "conv", "comm", "ledger", "ops", "audit", "board"]


class _World:
    def __init__(self, app: Conn, gateway: Conn) -> None:
        self.app = app
        self.gateway = gateway
        self.items: list[uuid.UUID] = []
        self.person = _smoke_person(app)
        key = signing.public_bytes(Ed25519PrivateKey.generate())  # private key discarded
        fp = signing.fingerprint(key)
        code = nodes.create_enrolment(
            app, person_id=self.person, kind="DESKTOP", name=SMOKE_REF, core_fp=fp, gw_fp=fp
        )
        token, _, _ = nodes.parse_enrolment_code(code)
        self.node = nodes.enrol(
            gateway, token=token, public_key=key, tailscale_identity=f"ts:smoke-{fp}"
        )
        self.conn_id = self.open()

    def open(self) -> int:
        row = self.gateway.execute(
            "SELECT conn_id, refusal FROM gw.open_connection(%s)", (self.node,)
        ).fetchone()
        if row is None or row[1] is not None:
            raise RuntimeError(f"smoke connection refused: {None if row is None else row[1]}")
        return int(row[0])

    def item(self, supersedes_id: uuid.UUID | None = None) -> uuid.UUID:
        event = comm.record_event(
            self.app, "smoke.notice", subject_type="smoke", subject_id=f"smoke-{uuid.uuid4().hex}",
            person_id=self.person, details={},
        )  # fmt: skip
        item = comm.create_outbox(
            self.app, person_id=self.person, event_id=event, priority=comm.Priority.SILENT,
            kind="smoke.notice", body={"text": "A fictional smoke notice"}, group_key="smoke",
            expires_at=None, supersedes_id=supersedes_id,
        )  # fmt: skip
        self.items.append(item)
        return item

    def tidy(self) -> None:
        """Supersede the smoke items still open, then revoke the node (closing its
        connections). Terminal items are left alone, so no refusal is audited."""
        for (item,) in self.app.execute(
            "SELECT id FROM comm.outbox WHERE id = ANY(%s) AND state <> ALL(%s) ORDER BY id",
            (self.items, list(TERMINAL)),
        ).fetchall():
            self.app.execute("SELECT comm.supersede(%s)", (item,))
        nodes.revoke(self.app, self.node)


def _smoke_person(app: Conn) -> uuid.UUID:
    row = app.execute(
        "SELECT id FROM ledger.person WHERE display_name = %s AND source_ref = %s"
        " ORDER BY id LIMIT 1",
        (SMOKE_PERSON, SMOKE_REF),
    ).fetchone()
    if row is None:
        row = app.execute(
            "INSERT INTO ledger.person (display_name, organization_id, source_ref)"
            " VALUES (%s, %s, %s) RETURNING id",
            (SMOKE_PERSON, config.HOUSEHOLD_ID, SMOKE_REF),
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _denied(conn: Conn, sql: Any, params: tuple[Any, ...] = ()) -> bool:
    """True if `sql` is refused for lack of privilege. Run in a transaction that is always
    rolled back, so a statement that is wrongly allowed still changes nothing."""
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(sql, params)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def _fenced(conn: Conn, sql: Any, params: tuple[Any, ...]) -> bool:
    try:
        conn.execute(sql, params)
    except psycopg.Error as e:
        return e.sqlstate == FENCED_SQLSTATE
    return False


def _e01(w: _World) -> tuple[bool, str]:
    event = comm.record_event(
        w.app, "memory.written", subject_type="smoke", subject_id=f"smoke-{uuid.uuid4().hex}",
        person_id=w.person, details={},
    )  # fmt: skip
    payload = {"event_id": str(event), "event_kind": "memory.written",
               "person_id": str(w.person), "context": {}}  # fmt: skip
    # Queued for tomorrow, then claimed here at once: a running core never takes it.
    job_id = jobs.enqueue(w.app, "system.event", payload, delay=timedelta(days=1))
    row = w.app.execute(
        "UPDATE ops.job SET status = 'RUNNING', attempts = attempts + 1, locked_by = %s,"
        " locked_until = now() + interval '5 minutes' WHERE id = %s AND status = 'QUEUED'"
        " RETURNING id, kind, payload, attempts, max_attempts",
        (SMOKE_REF, job_id),
    ).fetchone()
    assert row is not None
    dispatch.handle(w.app, jobs.Job(*row))
    items = w.app.execute(
        "SELECT o.id, o.state, j.status FROM comm.outbox o, ops.job j"
        " WHERE o.event_id = %s AND j.id = %s",
        (event, job_id),
    ).fetchall()
    w.items += [r[0] for r in items]
    ok = [(r[1], r[2]) for r in items] == [("PENDING", "DONE")]
    return ok, f"job {job_id}, {len(items)} outbox item"


def _e04(w: _World) -> tuple[bool, str]:
    item = w.item()
    sent = [w.gateway.execute("SELECT comm.record_sent(%s, %s)", (w.conn_id, item)).fetchone()
            for _ in range(2)]  # fmt: skip
    rows = w.app.execute(
        "SELECT node_id, attempts FROM comm.delivery WHERE outbox_id = %s", (item,)
    ).fetchall()
    ok = sent == [(True,), (True,)] and [tuple(r) for r in rows] == [(w.node, 2)]
    return ok, f"{len(rows)} delivery row, attempts {rows[0][1] if rows else None}"


def _e06(w: _World) -> tuple[bool, str]:
    old = w.item()
    new = w.item(supersedes_id=old)
    gone = w.item()
    w.app.execute("SELECT comm.expire(%s)", (gone,))
    rows = w.gateway.execute(
        "SELECT id, state, body->>'text' FROM comm.outbox WHERE id = ANY(%s)", ([old, new, gone],)
    ).fetchall()
    states = {r[0]: r[1] for r in rows if r[2]}
    ok = states == {old: "SUPERSEDED", new: "PENDING", gone: "EXPIRED"}
    return ok, "superseded and expired items read back by the gateway"


def _e11(w: _World) -> tuple[bool, str]:
    with w.app.transaction(force_rollback=True):  # nothing here survives
        task = ledger.create_task(
            w.app, ACTOR, title="P2-E11 smoke approval", definition_of_done="Refused after expiry",
            source_ref=SMOKE_REF,
        )  # fmt: skip
        row = w.app.execute(
            "INSERT INTO ledger.approval (task_id, requested_from_person_id, requesting_node_id,"
            " tier, action_kind, action_args, summary, card_authz, expires_at)"
            " VALUES (%s, %s, %s, 'IRREVERSIBLE', 'file.delete', %s, 'Smoke: decide after expiry',"
            " %s, clock_timestamp() - interval '1 second') RETURNING id",
            (task.id, w.person, w.node, Jsonb({"name": "fictional-smoke.txt"}),
             Jsonb({"object": {}, "key_id": SMOKE_REF, "sig": SMOKE_REF})),
        ).fetchone()  # fmt: skip
        assert row is not None
        refused = []
        for decision in ("APPROVED", "DENIED"):
            try:
                with w.app.transaction():
                    w.app.execute(
                        "UPDATE ledger.approval SET decision = %s, decided_at = now(),"
                        " decided_by_node_id = %s WHERE id = %s",
                        (decision, w.node, row[0]),
                    )
            except psycopg.errors.CheckViolation:
                refused.append(decision)
        state = w.app.execute(
            "SELECT decision FROM ledger.approval WHERE id = %s", (row[0],)
        ).fetchone()
    ok = refused == ["APPROVED", "DENIED"] and state == ("PENDING",)
    return ok, f"refused {', '.join(refused) or 'nothing'}; still {state[0] if state else None}"


def _s1(w: _World) -> tuple[bool, str]:
    checks = {
        "ledger.transition": "SELECT ledger.transition('task', gen_random_uuid(), 'READY',"
        " 'AGENT', 'gateway', 'smoke')",
        "audit insert": "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id,"
        " action, outcome, reason) VALUES ('AGENT', 'gateway', 'node', gen_random_uuid(),"
        " 'CREATE', 'APPLIED', 'smoke')",
        "audit.record_memory": "SELECT audit.record_memory(NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, NULL, NULL)",
    }
    denied = [name for name, sql in checks.items() if _denied(w.gateway, sql.encode())]
    return len(denied) == len(checks), f"gateway denied: {', '.join(denied)}"


def _s6(w: _World) -> tuple[bool, str]:
    denied = 0
    for conn in (w.gateway, w.app):
        denied += _denied(conn, "UPDATE gw.node SET public_key = %s WHERE id = %s",
                          (secrets.token_bytes(32), w.node))  # fmt: skip
        denied += _denied(conn, "UPDATE gw.node SET tailscale_identity = 'ts:smoke-other'"
                          " WHERE id = %s", (w.node,))  # fmt: skip
    row = w.app.execute(
        "SELECT count(*), count(*) FILTER (WHERE NOT coalesce("
        " 'search_path=pg_catalog, pg_temp' = ANY(p.proconfig), false)"
        " OR pg_get_userbyid(p.proowner) <> 'jarvis_owner')"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE p.prosecdef AND n.nspname = ANY(%s)",
        (SCHEMAS,),
    ).fetchone()
    assert row is not None
    total, bad = int(row[0]), int(row[1])
    ok = denied == 4 and total > 0 and bad == 0
    return ok, f"key and identity updates denied {denied}/4; {total} definer functions, {bad} bad"


def _b_idx(w: _World) -> tuple[bool, str]:
    with w.app.transaction(force_rollback=True):  # nothing here survives
        w.app.execute(
            "INSERT INTO ops.brain_budget (month, cap_inr) VALUES"
            " (date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata')::date, 0)"
            " ON CONFLICT DO NOTHING"
        )
        job = w.app.execute(
            "INSERT INTO ops.job (kind, status, finished_at) VALUES ('smoke.check', 'DONE', now())"
            " RETURNING id"
        ).fetchone()
        assert job is not None
        turn = uuid.uuid4()
        insert = (
            "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day, reserved_inr, state,"
            " lease_until) VALUES (%s, %s,"
            " date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata')::date,"
            " (now() AT TIME ZONE 'Asia/Kolkata')::date, 0, 'ACTIVE', now() + interval '1 minute')"
        )
        w.app.execute(insert, (job[0], turn))
        try:
            with w.app.transaction():
                w.app.execute(insert, (job[0], turn))
            refused = False
        except psycopg.errors.UniqueViolation:
            refused = True
    return refused, "second ACTIVE reservation refused" if refused else "second one allowed"


def _fence(w: _World) -> tuple[bool, str]:
    item = w.item()
    old, w.conn_id = w.conn_id, w.open()  # opening a new connection supersedes the old one
    checks = {
        "touch_connection": ("SELECT gw.touch_connection(%s)", (old,)),
        "record_sent": ("SELECT comm.record_sent(%s, %s)", (old, item)),
        "current_node": ("SELECT gw.current_node(%s)", (old,)),
    }
    fenced = [name for name, (sql, params) in checks.items() if _fenced(w.gateway, sql, params)]
    w.gateway.execute("SELECT gw.touch_connection(%s)", (w.conn_id,))  # the new one works
    return len(fenced) == len(checks), f"old connection fenced: {', '.join(fenced)}"


P2_CHECKS: list[tuple[str, Callable[[_World], tuple[bool, str]]]] = [
    ("P2-E01 system event makes an outbox item", _e01),
    ("P2-E04 re-send keeps one delivery row", _e04),
    ("P2-E06 superseded and expired readable", _e06),
    ("P2-E11 decision after expiry refused", _e11),
    ("P2-S1 gateway denied ledger and audit", _s1),
    ("P2-S6-db node key and identity locked", _s6),
    ("P2-B-idx one live reservation per turn", _b_idx),
    ("P2-fence superseded connection fenced", _fence),
]


def _error(e: Exception) -> str:
    first = str(e).strip().splitlines()
    return f"{type(e).__name__}: {first[0]}" if first else type(e).__name__


def run_phase2(app: Conn, gateway: Conn) -> list[Result]:
    """The database-only Phase 2 rows, in order. `app` and `gateway` are autocommit
    connections as jarvis_app and jarvis_gateway. A row that raises is a FAIL with the error."""
    try:
        world = _World(app, gateway)
    except Exception as e:
        return [(name, False, f"setup: {_error(e)}") for name, _ in P2_CHECKS]
    results: list[Result] = []
    try:
        for name, check in P2_CHECKS:
            try:
                ok, detail = check(world)
            except Exception as e:
                ok, detail = False, _error(e)
            results.append((name, ok, detail))
    finally:
        try:
            world.tidy()
        except Exception as e:
            results.append(("P2-tidy", False, _error(e)))
    return results
