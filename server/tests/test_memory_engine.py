"""Memory engine rules (spec §4.4, §5, §7, §9) with an in-memory audit log and directory.

The Postgres audit function and M1-M4 against the real database are in test_memory_acceptance.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from jarvis_server.contracts import (
    ActorKind,
    AuditAction,
    AuditOutcome,
    MemoryCategory,
    MemoryEntry,
    MemoryTag,
    Visibility,
)
from jarvis_server.memory import checks as checks_ids
from jarvis_server.memory import format as fmt
from jarvis_server.memory.checks import (
    HOUSEHOLD_ID,
    ListAudit,
    StaticDirectory,
    add_test_family,
)
from jarvis_server.memory.engine import SYSTEM, MemoryEngine
from jarvis_server.memory.errors import (
    MemoryFormatError,
    MemoryNotFound,
    MemoryRefused,
    NotActionable,
    RemoteConfigured,
    UnknownViewer,
)

TODAY = date(2026, 11, 22)
P, IDENT = MemoryCategory.PREFERENCE, MemoryCategory.IDENTITY
S, INF, H = MemoryTag.STATED, MemoryTag.INFERRED, MemoryTag.HISTORICAL


def make(root: Path, audit: ListAudit | None = None) -> MemoryEngine:
    return MemoryEngine(
        root, StaticDirectory(), audit or ListAudit(), HOUSEHOLD_ID, today=lambda: TODAY
    ).open()


@pytest.fixture
def audit() -> ListAudit:
    return ListAudit()


@pytest.fixture
def eng(tmp_path: Path, audit: ListAudit) -> MemoryEngine:
    e = make(tmp_path / "memory", audit)
    add_test_family(e)
    return e


def pref(eng: MemoryEngine, **kw: Any) -> str:
    args: dict[str, Any] = {
        "subject": "manj",
        "category": P,
        "content": "Likes tea",
        "tag": S,
        "source": "test",
        "by": "manj",
        "domain": "lifestyle",
    }
    args.update(kw)
    return eng.write(**args)


def git(eng: MemoryEngine, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=eng.store.root, capture_output=True, text=True, check=True
    ).stdout


# --- tags (spec §7), against an independent copy of the table ------------------------------

# (start tag, call, by) -> resulting tag, or None when refused
TAG_RULES: list[tuple[MemoryTag, str, str, MemoryTag | None]] = [
    (S, "confirm", "manj", MemoryTag.CONFIRMED),
    (INF, "confirm", "manj", MemoryTag.CONFIRMED),
    (H, "confirm", "manj", None),
    (MemoryTag.CONFIRMED, "confirm", "manj", None),
    (S, "confirm", "agent:atlas", None),
    (INF, "confirm", "agent:atlas", None),
    (INF, "confirm", SYSTEM, None),
    (MemoryTag.CONFIRMED, "update", "manj", S),
    (S, "update", "manj", S),
    (INF, "update", "manj", INF),
    (H, "update", "manj", H),
    (INF, "update", "agent:atlas", INF),
    (S, "update", "agent:atlas", None),
    (MemoryTag.CONFIRMED, "update", SYSTEM, None),
    (S, "archive", "manj", MemoryTag.EXPIRED),
    (INF, "archive", SYSTEM, MemoryTag.EXPIRED),
    (MemoryTag.CONFIRMED, "archive", "manj", MemoryTag.EXPIRED),
    (H, "archive", "agent:atlas", None),
    (MemoryTag.EXPIRED, "update", "manj", None),
    (MemoryTag.EXPIRED, "confirm", "manj", None),
    (MemoryTag.EXPIRED, "archive", "manj", None),
]


def _entry_with_tag(eng: MemoryEngine, tag: MemoryTag) -> str:
    if tag is MemoryTag.CONFIRMED:
        mid = pref(eng)
        eng.confirm(mid, by="manj")
    elif tag is MemoryTag.EXPIRED:
        mid = pref(eng)
        eng.archive(mid, by="manj", reason="no-longer-true")
    else:
        mid = pref(eng, tag=tag, by="manj" if tag is S else "agent:atlas")
    return mid


@pytest.mark.parametrize(("start", "call", "by", "result"), TAG_RULES)
def test_tag_rules(
    eng: MemoryEngine, start: MemoryTag, call: str, by: str, result: MemoryTag | None
) -> None:
    mid = _entry_with_tag(eng, start)
    head = eng.store.head()

    def do() -> None:
        if call == "confirm":
            eng.confirm(mid, by=by)
        elif call == "update":
            eng.update(mid, "Likes green tea", "test", by=by)
        else:
            eng.archive(mid, by=by, reason="no-longer-true")

    if result is None:
        with pytest.raises(MemoryRefused):
            do()
        assert eng.store.head() == head
        assert eng.get(mid, viewer=SYSTEM).tag is start
    else:
        do()
        assert eng.get(mid, viewer=SYSTEM).tag is result


@pytest.mark.parametrize(
    ("kw", "fragment"),
    [
        ({"tag": MemoryTag.CONFIRMED}, "tag-not-writable"),
        ({"tag": MemoryTag.EXPIRED}, "tag-not-writable"),
        ({"by": "agent:atlas"}, "agent-tag-not-allowed"),
        ({"by": SYSTEM}, "agent-tag-not-allowed"),
        ({"subject": "stranger"}, "unknown-subject"),
        ({"by": "stranger"}, "unknown-recorder"),
        ({"by": "agent:BAD NAME", "tag": INF}, "bad-agent-name"),
        ({"visibility": Visibility.SELECTED_GROUP}, "Phase 1"),
        ({"visibility": Visibility.ORGANIZATION_SHARED}, "Phase 1"),
        ({"shared_with": ["daughter"]}, "shared with"),
        ({"visibility": Visibility.SELECTED_PERSON}, "shared with"),
        (
            {"visibility": Visibility.SELECTED_PERSON, "shared_with": ["ghost"]},
            "unknown-shared-with",
        ),
        ({"content": "two\nlines"}, "content"),
        ({"content": ""}, "content"),
        ({"content": "x" * 501}, "content"),
        ({"source": "has space"}, "source"),
        ({"domain": None}, "domain"),
        ({"domain": "food"}, "domain"),
        ({"topics": [f"t{i}" for i in range(9)]}, "topics"),
        ({"topics": ["Two Words"]}, "topics"),
        ({"subject": "household", "category": IDENT, "domain": None}, "household"),
        ({"category": MemoryCategory.EPISODIC, "domain": None}, "happened on"),
    ],
)
def test_write_refusals_are_audited_and_change_nothing(
    eng: MemoryEngine, audit: ListAudit, kw: dict[str, Any], fragment: str
) -> None:
    head, before = eng.store.head(), len(audit.records)
    with pytest.raises(MemoryRefused, match=fragment):
        pref(eng, **kw)
    assert eng.store.head() == head
    assert len(audit.records) == before + 1
    rec = audit.records[-1]
    assert rec.outcome is AuditOutcome.REJECTED and rec.commit is None
    assert rec.ref == "m-unassigned" and rec.action is AuditAction.CREATE


# --- visibility (spec §5) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("visibility", "shared", "viewer", "sees"),
    [
        (Visibility.PRIVATE, (), "daughter", True),  # subject
        (Visibility.PRIVATE, (), "manj", True),  # recorder
        (Visibility.PRIVATE, (), "neighbour", False),
        (Visibility.SELECTED_PERSON, ("neighbour",), "neighbour", True),
        (Visibility.SELECTED_PERSON, ("neighbour",), "manj", True),  # recorder
        (Visibility.FAMILY_SHARED, (), "neighbour", False),  # not in the household
        (Visibility.FAMILY_SHARED, (), "daughter", True),
        (Visibility.PRIVATE, (), SYSTEM, True),
    ],
)
def test_visibility_matrix(
    eng: MemoryEngine, visibility: Visibility, shared: tuple[str, ...], viewer: str, sees: bool
) -> None:
    mid = pref(eng, subject="daughter", visibility=visibility, shared_with=shared)
    assert any(e.memory_id == mid for e in eng.read("daughter", P, viewer=viewer)) is sees
    if not sees:
        with pytest.raises(MemoryNotFound):
            eng.get(mid, viewer=viewer)


def test_no_automatic_parental_access(eng: MemoryEngine) -> None:
    """Manj decision 2026-11-22: a parent does not see a child's PRIVATE entries."""
    mid = pref(eng, subject="daughter", by="daughter", content="Keeps a diary")
    assert eng.read("daughter", P, viewer="manj") == []
    assert eng.search("diary", viewer="manj") == []
    assert eng.retrieve_context("daughter", "diary", viewer="manj") == []
    for change in (
        lambda: eng.confirm(mid, by="manj"),
        lambda: eng.update(mid, "x", "test", by="manj"),
        lambda: eng.archive(mid, by="manj", reason="no-longer-true"),
    ):
        with pytest.raises(MemoryRefused, match="no-such-entry"):
            change()
    with pytest.raises(MemoryNotFound):
        eng.history(mid, viewer="manj")
    assert eng.get(mid, viewer="daughter").content == "Keeps a diary"


def test_unknown_viewer_is_refused(eng: MemoryEngine) -> None:
    with pytest.raises(UnknownViewer):
        eng.read("manj", P, viewer="stranger")


# --- IDs, git, concurrency (spec §4.3, §4.4) ----------------------------------------------


def test_ids_are_never_reused(eng: MemoryEngine) -> None:
    a = pref(eng)
    eng.archive(a, by="manj", reason="no-longer-true")
    b = pref(eng, content="Likes coffee")
    assert (a, b) == ("m-0001", "m-0002")
    reopened = make(eng.store.root)
    assert pref(reopened, content="Likes juice") == "m-0003"


def test_one_commit_per_change_and_no_content_in_commits_or_audit(
    eng: MemoryEngine, audit: ListAudit
) -> None:
    commits = len(eng.store.log())
    mid = pref(eng, content="Secret sauce recipe")
    eng.update(mid, "Secret sauce recipe v2", "test", by="manj")
    eng.confirm(mid, by="manj")
    eng.archive(mid, by="manj", reason="no-longer-true")
    assert len(eng.store.log()) == commits + 4
    messages = git(eng, "log", "--format=%B")
    assert "Secret" not in messages
    assert all("Secret" not in repr(r) for r in audit.records)
    applied = [r for r in audit.records if r.ref == mid]
    assert [(r.action, r.from_tag, r.to_tag) for r in applied] == [
        (AuditAction.CREATE, None, "STATED"),
        (AuditAction.UPDATE, "STATED", "STATED"),
        (AuditAction.CONFIRM, "STATED", "CONFIRMED"),
        (AuditAction.ARCHIVE, "CONFIRMED", "EXPIRED"),
    ]
    assert all(r.commit and r.actor_kind is ActorKind.PERSON for r in applied)


def test_concurrent_writers_get_distinct_ids(eng: MemoryEngine) -> None:
    other = make(eng.store.root)  # a second engine on the same repo
    ids: list[str] = []
    lock = threading.Lock()

    def worker(engine: MemoryEngine, n: int) -> None:
        for i in range(20):
            mid = pref(engine, content=f"Entry {n}-{i}")
            with lock:
                ids.append(mid)

    threads = [threading.Thread(target=worker, args=(e, n)) for n, e in enumerate([eng, other] * 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 80 and len(set(ids)) == 80
    assert len(make(eng.store.root).read("manj", P, viewer="manj")) == 80


def test_engine_reloads_when_another_writer_moves_head(eng: MemoryEngine) -> None:
    other = make(eng.store.root)
    mid = pref(other)
    assert eng.get(mid, viewer="manj").content == "Likes tea"


def test_refuses_a_repo_with_a_remote(eng: MemoryEngine) -> None:
    git(eng, "remote", "add", "origin", "https://example.invalid/memory.git")
    with pytest.raises(RemoteConfigured):
        make(eng.store.root)


def test_crashed_write_is_discarded_on_open(eng: MemoryEngine) -> None:
    pref(eng)
    path = eng.store.root / "preferences" / "lifestyle.md"
    path.write_text(path.read_text() + "half-written junk\n")
    (eng.store.root / "preferences" / "travel.md").write_text("# untracked\n")
    reopened = make(eng.store.root)
    assert len(reopened.read("manj", P, viewer="manj")) == 1
    assert git(eng, "status", "--porcelain") == ""


def test_damaged_committed_file_stops_the_load(eng: MemoryEngine) -> None:
    pref(eng)
    path = eng.store.root / "preferences" / "lifestyle.md"
    path.write_text(path.read_text().replace("[STATED]", "[MAYBE]"))
    git(eng, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "hand edit")
    with pytest.raises(MemoryFormatError) as err:
        make(eng.store.root)
    assert err.value.path == "preferences/lifestyle.md" and err.value.line == 3


# --- audit failure and recovery (spec §9) -------------------------------------------------


def test_audit_failure_undoes_the_commit(eng: MemoryEngine, audit: ListAudit) -> None:
    head = eng.store.head()
    audit.fail_next = True
    with pytest.raises(RuntimeError, match="simulated"):
        pref(eng)
    assert eng.store.head() == head
    assert eng.read("manj", P, viewer="manj") == []
    assert pref(eng) == "m-0001"  # the undone ID is free again: it was never recorded


def test_startup_reconciles_commits_without_audit_rows(eng: MemoryEngine, audit: ListAudit) -> None:
    mid = pref(eng)
    lost = audit.records.pop()  # the process "died" after the commit
    make(eng.store.root, audit)
    rec = audit.records[-1]
    assert rec.commit == lost.commit and rec.ref == mid
    assert rec.actor_kind is ActorKind.SYSTEM and rec.reason.startswith("reconciled")
    assert (rec.action, rec.to_tag) == (AuditAction.CREATE, "STATED")
    before = len(audit.records)
    make(eng.store.root, audit)
    assert len(audit.records) == before  # nothing more to reconcile


# --- reads ---------------------------------------------------------------------------------


def test_assert_actionable(eng: MemoryEngine) -> None:
    stated = pref(eng)
    hist = pref(eng, tag=H, by="agent:atlas")
    eng.assert_actionable([stated])
    with pytest.raises(NotActionable) as err:
        eng.assert_actionable([stated, hist, "m-9999"])
    assert err.value.memory_ids == [hist, "m-9999"]


def test_history_returns_every_version(eng: MemoryEngine) -> None:
    mid = pref(eng)
    eng.update(mid, "Likes green tea", "msg-2", by="manj")
    eng.confirm(mid, by="manj")
    versions = eng.history(mid, viewer="manj")
    assert [(v.entry.content, v.entry.tag) for v in versions] == [
        ("Likes tea", S),
        ("Likes green tea", S),
        ("Likes green tea", MemoryTag.CONFIRMED),
    ]


def test_retrieve_context_orders_by_tag_then_recency(eng: MemoryEngine) -> None:
    inferred = pref(eng, tag=INF, by="agent:atlas", topics=["travel"])
    stated = pref(eng, topics=["travel"])
    confirmed = pref(eng, topics=["travel"])
    eng.confirm(confirmed, by="manj")
    got = eng.retrieve_context("manj", "trip", viewer="manj")
    assert [e.memory_id for e in got] == [confirmed, stated, inferred]
    assert [e.drives_action for e in got] == [True, True, False]


def test_retrieve_context_speed_with_5000_entries(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    eng = make(root)
    add_test_family(eng)
    entries = [
        MemoryEntry(
            memory_id=f"m-{i:04d}",
            subject="manj" if i % 2 else "daughter",
            category=P,
            domain="travel",
            tag=S,
            content=f"Travel note number {i}",
            source="test",
            recorded_on=TODAY,
            by="manj",
            visibility=Visibility.FAMILY_SHARED,
            topics=("travel",) if i % 5 == 0 else (),
        )
        for i in range(1, 5001)
    ]
    eng.store.commit(
        {"preferences/travel.md": fmt.render_file("preferences/travel.md", entries)}, "bulk test"
    )
    big = make(root)
    start = time.perf_counter()
    got = big.retrieve_context("daughter", "school trip", viewer="manj")
    elapsed = time.perf_counter() - start
    assert len(got) == 50
    assert elapsed < 0.05, f"retrieve_context took {elapsed * 1000:.1f} ms"


# --- findings from the independent review (2026-11-22) -------------------------------------

CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def test_q1_episodic_year_must_have_four_digits(eng: MemoryEngine) -> None:
    with pytest.raises(MemoryRefused, match="invalid-entry"):
        pref(
            eng,
            category=MemoryCategory.EPISODIC,
            domain=None,
            tag=H,
            by="agent:atlas",
            happened_on=date(999, 1, 1),
        )
    make(eng.store.root)  # still loads


def test_q2_failed_commit_leaves_nothing_behind(
    eng: MemoryEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = eng.store._git
    calls = {"failed": False}

    def flaky(*args: str, check: bool = True) -> str:
        if args and args[0] == "commit" and not calls["failed"]:
            calls["failed"] = True
            raise RuntimeError("disk full")
        return real(*args, check=check)

    monkeypatch.setattr(eng.store, "_git", flaky)
    with pytest.raises(RuntimeError):
        pref(
            eng,
            category=MemoryCategory.EPISODIC,
            domain=None,
            tag=H,
            by="agent:atlas",
            happened_on=date(2026, 5, 1),
        )
    mid = pref(eng)
    files = git(eng, "show", "--name-only", "--format=", "HEAD").split()
    assert "episodic/2026.md" not in files
    assert git(eng, "status", "--porcelain") == ""
    assert make(eng.store.root).get(mid, viewer="manj").content == "Likes tea"


def test_q3_matching_works_in_any_script(eng: MemoryEngine) -> None:
    kannada = pref(eng, content="ದೋಸೆ ಇಷ್ಟ", domain="restaurants")
    cafe = pref(eng, content="Loves the Café near school", domain="restaurants")
    assert [e.memory_id for e in eng.search("ದೋಸೆ", viewer="manj")] == [kannada]
    assert [e.memory_id for e in eng.search("cafe", viewer="manj")] == [cafe]
    assert eng.search("ಇಷ್ಟ", viewer="manj")[0].memory_id == kannada


def test_q5_retrieve_context_most_recent_first(tmp_path: Path) -> None:
    day = {"d": date(2026, 1, 1)}
    e = MemoryEngine(
        tmp_path / "m", StaticDirectory(), ListAudit(), HOUSEHOLD_ID, today=lambda: day["d"]
    ).open()
    add_test_family(e)
    older = pref(e, topics=["travel"])
    day["d"] = date(2026, 6, 1)
    newer = pref(e, topics=["travel"])
    day["d"] = date(2026, 3, 1)
    middle = pref(e, topics=["travel"])
    got = [x.memory_id for x in e.retrieve_context("manj", "trip", viewer="manj")]
    assert got == [newer, middle, older]


def test_s1_no_free_text_reaches_the_audit(eng: MemoryEngine, audit: ListAudit) -> None:
    mid = pref(eng)
    with pytest.raises(MemoryRefused, match="bad-archive-reason"):
        eng.archive(mid, by="manj", reason="Her diary was about Rahul")
    for bad in (
        lambda: pref(eng, subject="her-diary-secret"),
        lambda: pref(eng, by="rahul-told-me"),
        lambda: eng.confirm("diary about rahul", by="manj"),
        lambda: eng.add_person("Rahul Diary", checks_ids.NEIGHBOUR_ID, "x", by=SYSTEM),
    ):
        with pytest.raises(MemoryRefused):
            bad()
    eng.archive(mid, by="manj", reason="superseded")
    for r in audit.records:
        assert CODE.match(r.reason), r.reason
        assert r.ref in ("m-unassigned",) or r.ref.startswith(("m-0", "person:")), r.ref
        assert "diary" not in repr(r).lower() and "rahul" not in repr(r).lower()


def test_s2_repo_config_that_could_run_code_is_refused(eng: MemoryEngine) -> None:
    from jarvis_server.memory.errors import UnsafeRepo

    for key, value in (("core.fsmonitor", "touch pwned"), ("include.path", "../evil.cfg")):
        git(eng, "config", key, value)
        with pytest.raises(UnsafeRepo, match=key.lower()):
            make(eng.store.root)
        git(eng, "config", "--unset", key)
    attrs = eng.store.root / ".git" / "info" / "attributes"
    attrs.parent.mkdir(exist_ok=True)
    attrs.write_text("*.md filter=evil\n")
    with pytest.raises(UnsafeRepo):
        make(eng.store.root)
    attrs.unlink()
    make(eng.store.root)


def test_s5_agents_learn_nothing_about_existence_from_confirm(eng: MemoryEngine) -> None:
    mid = pref(eng)
    codes = []
    for target in (mid, "m-9999"):
        with pytest.raises(MemoryRefused) as err:
            eng.confirm(target, by="agent:atlas")
        codes.append(err.value.code)
    assert codes == ["person-required", "person-required"]


def test_d6_limit_and_argument_checks(eng: MemoryEngine) -> None:
    for i in range(3):
        pref(eng, content=f"Travel note {i}", topics=["travel"])
    assert len(eng.retrieve_context("manj", "trip", viewer="manj", limit=-1)) == 1
    assert len(eng.retrieve_context("manj", "trip", viewer="manj", limit=500)) == 3
    with pytest.raises(TypeError):
        eng.assert_actionable("m-0001")
