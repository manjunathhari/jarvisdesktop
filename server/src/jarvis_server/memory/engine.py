"""The memory engine (Architecture v2 §11; Phase 1 spec §5-§9).

The markdown files in a private git repo are the source of truth. The engine loads them into
memory, answers reads from there (reloading when HEAD moves), and makes every change as one git
commit followed by one audit row. Tags change only through these calls, under the rules of
spec §7. Visibility is checked against a viewer on every read (spec §5).

Phase 1 boundary: no embeddings, no AI, no consolidation, no external memory services.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import NoReturn, Protocol
from uuid import UUID

from pydantic import ValidationError

from jarvis_server.contracts import (
    ActorKind,
    AuditAction,
    AuditOutcome,
    MemoryCategory,
    MemoryEntry,
    MemoryTag,
    Visibility,
)
from jarvis_server.memory import format as fmt
from jarvis_server.memory import text
from jarvis_server.memory.errors import (
    MemoryEngineError,
    MemoryFormatError,
    MemoryNotFound,
    MemoryRefused,
    NotActionable,
    UnknownViewer,
)
from jarvis_server.memory.format import HOUSEHOLD, INDEX, Person
from jarvis_server.memory.store import Commit, GitStore

log = logging.getLogger("jarvis.memory")

SYSTEM = "system"
_AGENT_RE = re.compile(r"^agent:[a-z][a-z0-9-]{0,39}$")
UNASSIGNED = "m-unassigned"
_MEMORY_ID_RE = re.compile(r"^m-[0-9]{4,}$")
# Fixed reasons for archive (spec §7). Free text never reaches the append-only audit trail.
ARCHIVE_REASONS = ("no-longer-true", "superseded", "recorded-in-error", "requested-by-person")
MAX_CONTEXT = 50
_TAG_RANK = {
    MemoryTag.CONFIRMED: 0,
    MemoryTag.STATED: 1,
    MemoryTag.INFERRED: 2,
    MemoryTag.HISTORICAL: 3,
    MemoryTag.EXPIRED: 4,
}


# --- collaborators -------------------------------------------------------------------------


class Directory(Protocol):
    """Who exists in the ledger, and in which household."""

    def organization_of(self, person_id: UUID) -> UUID | None:
        """The person's organization, or None. Raises LookupError if the person is unknown."""
        ...


@dataclass(frozen=True)
class AuditRecord:
    actor_kind: ActorKind
    actor_id: str
    ref: str  # m-NNNN, person:<slug> or m-unassigned
    action: AuditAction
    from_tag: str | None
    to_tag: str | None
    outcome: AuditOutcome
    reason: str
    commit: str | None


class AuditSink(Protocol):
    """Where memory changes are recorded (Postgres in production; ADR-010)."""

    def record(self, rec: AuditRecord) -> None: ...

    def recorded_commits(self) -> set[str]: ...


@dataclass(frozen=True)
class MemoryVersion:
    commit: str
    committed_at: datetime
    entry: MemoryEntry


class _Refusal(Exception):
    """A rule refused the change. `code` is a fixed reason code (the only text audited);
    `detail` names fields, never values, and goes only to the caller."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


# --- the engine ----------------------------------------------------------------------------


class MemoryEngine:
    def __init__(
        self,
        root: Path,
        directory: Directory,
        audit: AuditSink,
        household_id: UUID,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.store = GitStore(root)
        self.directory = directory
        self.audit = audit
        self.household_id = household_id
        self.today = today
        self._lock = threading.RLock()
        self._head = ""
        self._people: dict[str, Person] = {}
        self._entries: dict[str, MemoryEntry] = {}
        self._household: dict[str, bool] = {}

    # --- opening -------------------------------------------------------------------------

    def open(self) -> MemoryEngine:
        """Create or open the repo, check it, reconcile the audit trail and load everything."""
        with self._lock:
            if not self.store.exists():
                self.store.create(
                    {INDEX: fmt.render_index({})},
                    _message("create repository", "INIT", None, None, None, SYSTEM),
                )
            self.store.check_no_remote()
            self.store.check_safe()
            with self.store.locked():
                if self.store.discard_uncommitted():
                    log.warning("memory: discarded uncommitted changes left by a crash")
                self._load()
                self._reconcile()
        log.info("memory ready: %d entries, head %s", len(self._entries), self._head[:12])
        return self

    def _load(self) -> None:
        people: dict[str, Person] = {}
        entries: dict[str, MemoryEntry] = {}
        for path, content in self.store.read_files().items():
            if path == INDEX:
                continue
            parsed = fmt.parse_file(path, content)
            if parsed.person is not None:
                people[parsed.person.slug] = parsed.person
            for e in parsed.entries:
                if e.memory_id in entries:
                    raise MemoryFormatError(path, 0, f"{e.memory_id} appears twice")
                entries[e.memory_id] = e
        ids = {p.person_id for p in people.values()}
        if len(ids) != len(people):
            raise MemoryFormatError("people/", 0, "two people files share a person_id")
        for e in entries.values():
            if e.subject != HOUSEHOLD and e.subject not in people:
                raise MemoryFormatError(fmt.path_for(e), 0, f"{e.memory_id}: unknown subject")
        self._people, self._entries = people, entries
        self._household = {}
        self._head = self.store.head()

    def _fresh(self) -> None:
        """Reload if another writer moved HEAD (reads)."""
        if self.store.head() != self._head:
            with self.store.locked():
                self._load()

    def _fresh_locked(self) -> None:
        """Reload if HEAD moved; the caller already holds the repo lock (changes)."""
        if self.store.head() != self._head:
            self._load()

    def _reconcile(self) -> None:
        """Add audit rows for commits that have none (process died between commit and audit)."""
        done = self.audit.recorded_commits()
        for c in self.store.log():
            trailers = _trailers(c)
            if c.sha in done or trailers.get("Action") in (None, "INIT"):
                continue
            self.audit.record(
                AuditRecord(
                    ActorKind.SYSTEM,
                    SYSTEM,
                    trailers.get("Memory-Ref", UNASSIGNED),
                    AuditAction(trailers["Action"]),
                    trailers.get("From") or None,
                    trailers.get("To") or None,
                    AuditOutcome.APPLIED,
                    "reconciled",
                    c.sha,
                )
            )
            log.warning("memory: reconciled audit row for commit %s", c.sha[:12])

    # --- reads ---------------------------------------------------------------------------

    def get(self, memory_id: str, *, viewer: str) -> MemoryEntry:
        with self._lock:
            self._fresh()
            self._check_viewer(viewer)
            e = self._entries.get(memory_id)
            if e is None or not self.can_see(e, viewer):
                raise MemoryNotFound(memory_id)
            return e

    def read(
        self,
        subject: str,
        category: MemoryCategory,
        *,
        viewer: str,
        include_expired: bool = False,
    ) -> list[MemoryEntry]:
        with self._lock:
            self._fresh()
            self._check_viewer(viewer)
            found = [
                e
                for e in self._entries.values()
                if e.subject == subject
                and e.category is category
                and (include_expired or e.tag is not MemoryTag.EXPIRED)
                and self.can_see(e, viewer)
            ]
            return sorted(found, key=fmt.sort_key)

    def search(
        self,
        query: str,
        *,
        viewer: str,
        subject: str | None = None,
        category: MemoryCategory | None = None,
        include_expired: bool = False,
    ) -> list[MemoryEntry]:
        with self._lock:
            self._fresh()
            self._check_viewer(viewer)
            wanted = text.terms(query)
            found = [
                e
                for e in self._entries.values()
                if (subject is None or e.subject == subject)
                and (category is None or e.category is category)
                and (include_expired or e.tag is not MemoryTag.EXPIRED)
                and self.can_see(e, viewer)
                and text.matches(wanted, e.content, e.topics)
            ]
            return _ranked(found)

    def retrieve_context(
        self, person: str, task: str, *, viewer: str, limit: int = MAX_CONTEXT
    ) -> list[MemoryEntry]:
        """Relevant, permitted entries for a task about a person (spec §6)."""
        with self._lock:
            self._fresh()
            self._check_viewer(viewer)
            if person != HOUSEHOLD and person not in self._people:
                raise MemoryNotFound(f"unknown person {person!r}")
            wanted = text.terms(task)
            found = [
                e
                for e in self._entries.values()
                if e.subject in (person, HOUSEHOLD)
                and e.tag is not MemoryTag.EXPIRED
                and self.can_see(e, viewer)
                and (
                    (e.category is MemoryCategory.IDENTITY and e.subject == person)
                    or text.matches(wanted, e.content, e.topics)
                )
            ]
            return _ranked(found)[: max(1, min(limit, MAX_CONTEXT))]

    def history(self, memory_id: str, *, viewer: str) -> list[MemoryVersion]:
        """Every committed version of one entry, oldest first (M2)."""
        current = self.get(memory_id, viewer=viewer)
        path = fmt.path_for(current)
        versions: list[MemoryVersion] = []
        for c in self.store.log(path):
            content = self.store.show(c.sha, path)
            if content is None:
                continue
            try:
                parsed = fmt.parse_file(path, content)
            except MemoryFormatError:
                continue
            for e in parsed.entries:
                if e.memory_id == memory_id and (not versions or versions[-1].entry != e):
                    versions.append(MemoryVersion(c.sha, c.committed_at, e))
        return versions

    def assert_actionable(self, memory_ids: Iterable[str]) -> None:
        """Raise NotActionable unless every entry is STATED or CONFIRMED (M3)."""
        if isinstance(memory_ids, str):
            raise TypeError("pass a list of memory IDs, not a single string")
        with self._lock:
            self._fresh()
            blocked = [
                m
                for m in memory_ids
                if m not in self._entries or not self._entries[m].drives_action
            ]
        if blocked:
            raise NotActionable(blocked)

    def people(self) -> list[Person]:
        with self._lock:
            self._fresh()
            return sorted(self._people.values(), key=lambda p: p.slug)

    # --- visibility (spec §5) ------------------------------------------------------------

    def _check_viewer(self, viewer: str) -> None:
        if viewer != SYSTEM and viewer not in self._people:
            raise UnknownViewer(viewer)

    def _in_household(self, slug: str) -> bool:
        if slug not in self._household:
            p = self._people.get(slug)
            try:
                found = p is not None and (
                    self.directory.organization_of(p.person_id) == self.household_id
                )
            except LookupError:
                found = False
            self._household[slug] = found
        return self._household[slug]

    def _may_edit(self, e: MemoryEntry, person: str) -> bool:
        return person in (e.subject, e.by) or (
            e.subject == HOUSEHOLD and self._in_household(person)
        )

    def can_see(self, e: MemoryEntry, viewer: str) -> bool:
        if viewer == SYSTEM or viewer in (e.subject, e.by):
            return True
        if e.visibility is Visibility.SELECTED_PERSON and viewer in e.shared_with:
            return True
        return e.visibility is Visibility.FAMILY_SHARED and self._in_household(viewer)

    # --- changes -------------------------------------------------------------------------

    def write(
        self,
        subject: str,
        category: MemoryCategory,
        content: str,
        tag: MemoryTag,
        source: str,
        *,
        by: str,
        visibility: Visibility = Visibility.PRIVATE,
        shared_with: Iterable[str] = (),
        topics: Iterable[str] = (),
        domain: str | None = None,
        happened_on: date | None = None,
    ) -> str:
        """Record a new entry; returns its memory ID."""

        def change() -> tuple[str, MemoryEntry, str | None]:
            self._check_by(by)
            if tag not in (MemoryTag.STATED, MemoryTag.INFERRED, MemoryTag.HISTORICAL):
                raise _Refusal("tag-not-writable")
            if (by == SYSTEM or by.startswith("agent:")) and tag is MemoryTag.STATED:
                raise _Refusal("agent-tag-not-allowed")
            if subject != HOUSEHOLD and subject not in self._people:
                raise _Refusal("unknown-subject")
            names = tuple(shared_with)
            for name in names:
                if name not in self._people:
                    raise _Refusal("unknown-shared-with")
            memory_id = f"m-{self._next_number():04d}"
            entry = self._build(
                memory_id=memory_id,
                subject=subject,
                category=category,
                domain=domain,
                tag=tag,
                content=content,
                source=source,
                recorded_on=self.today(),
                by=by,
                visibility=visibility,
                shared_with=names,
                topics=tuple(t.lower() for t in topics),
                happened_on=happened_on,
            )
            return memory_id, entry, None

        return self._apply(AuditAction.CREATE, None, by, change)

    def update(self, memory_id: str, content: str, source: str, *, by: str) -> None:
        """New wording for an entry. A CONFIRMED entry goes back to STATED (spec §7)."""

        def change() -> tuple[str, MemoryEntry, str | None]:
            old = self._changeable(memory_id, by)
            if by == SYSTEM or by.startswith("agent:"):
                if old.tag not in (MemoryTag.INFERRED, MemoryTag.HISTORICAL):
                    raise _Refusal("agent-update-not-allowed")
            elif not self._may_edit(old, by):
                raise _Refusal("not-subject-or-recorder")
            if content == old.content:
                raise _Refusal("content-unchanged")
            tag = MemoryTag.STATED if old.tag is MemoryTag.CONFIRMED else old.tag
            entry = self._build(
                **{
                    **old.model_dump(),
                    "content": content,
                    "source": source,
                    "recorded_on": self.today(),
                    "tag": tag,
                    "confirmed_by": None,
                    "confirmed_on": None,
                }
            )
            return memory_id, entry, old.tag.value

        self._apply(AuditAction.UPDATE, memory_id, by, change)

    def confirm(self, memory_id: str, *, by: str) -> None:
        """A person confirms an entry. Jarvis can never confirm its own inference."""

        def change() -> tuple[str, MemoryEntry, str | None]:
            if by not in self._people:
                raise _Refusal("person-required")
            old = self._changeable(memory_id, by)
            if not self._may_edit(old, by):
                raise _Refusal("not-subject-or-recorder")
            if old.tag not in (MemoryTag.STATED, MemoryTag.INFERRED):
                raise _Refusal("tag-not-confirmable")
            entry = old.model_copy(
                update={
                    "tag": MemoryTag.CONFIRMED,
                    "confirmed_by": by,
                    "confirmed_on": self.today(),
                }
            )
            return memory_id, entry, old.tag.value

        self._apply(AuditAction.CONFIRM, memory_id, by, change)

    def archive(self, memory_id: str, *, by: str, reason: str) -> None:
        """Mark an entry EXPIRED: no longer true, kept for history. `reason` is one of
        ARCHIVE_REASONS."""

        def change() -> tuple[str, MemoryEntry, str | None]:
            if by.startswith("agent:"):
                raise _Refusal("agent-archive-not-allowed")
            if reason not in ARCHIVE_REASONS:
                raise _Refusal("bad-archive-reason", "use one of " + ", ".join(ARCHIVE_REASONS))
            old = self._changeable(memory_id, by)
            if by != SYSTEM and not self._may_edit(old, by):
                raise _Refusal("not-subject-or-recorder")
            entry = old.model_copy(
                update={"tag": MemoryTag.EXPIRED, "confirmed_by": None, "confirmed_on": None}
            )
            return memory_id, entry, old.tag.value

        self._apply(AuditAction.ARCHIVE, memory_id, by, change, reason=reason)

    def add_person(self, slug: str, person_id: UUID, display_name: str, *, by: str) -> None:
        """Make a ledger person a memory subject (spec §4.2)."""
        ref = f"person:{slug}"
        with self._lock, self.store.locked():
            self._fresh_locked()
            try:
                self._check_by(by)
                if by.startswith("agent:"):
                    raise _Refusal("agent-add-person-not-allowed")
                if not fmt.SLUG_RE.match(slug) or slug in (HOUSEHOLD, SYSTEM):
                    raise _Refusal("bad-subject-name")
                if slug in self._people:
                    raise _Refusal("subject-exists")
                if any(p.person_id == person_id for p in self._people.values()):
                    raise _Refusal("person-has-subject")
                name = display_name.strip()
                if not name or len(name) > 100 or any(ord(ch) < 32 for ch in name):
                    raise _Refusal("bad-display-name")
                try:
                    self.directory.organization_of(person_id)
                except LookupError:
                    raise _Refusal("person-not-in-ledger") from None
            except _Refusal as r:
                valid = bool(fmt.SLUG_RE.match(slug))
                self._refused(by, ref if valid else UNASSIGNED, AuditAction.CREATE, r)
            person = Person(slug, person_id, name)
            path = f"people/{slug}.md"
            files = {path: fmt.render_file(path, [], person)}
            people = {**self._people, slug: person}
            files[INDEX] = self._index_text(self._entries, extra=[path])
            msg = _message(f"add person {slug}", "CREATE", ref, None, None, by)
            self._people = people
            try:
                self._commit_and_audit(files, msg, by, ref, AuditAction.CREATE, None, None, "added")
            except Exception:
                self._people = {k: v for k, v in people.items() if k != slug}
                raise
            self._household.pop(slug, None)

    # --- internals -----------------------------------------------------------------------

    def _apply(
        self,
        action: AuditAction,
        memory_id: str | None,
        by: str,
        change: Callable[[], tuple[str, MemoryEntry, str | None]],
        *,
        reason: str | None = None,
    ) -> str:
        with self._lock, self.store.locked():
            self._fresh_locked()
            try:
                ref, entry, from_tag = change()
            except _Refusal as r:
                bad = not (memory_id and _MEMORY_ID_RE.match(memory_id))
                self._refused(by, UNASSIGNED if bad else str(memory_id), action, r)
            entries = {**self._entries, ref: entry}
            path = fmt.path_for(entry)
            files = {
                path: self._file_text(path, entries),
                INDEX: self._index_text(entries),
            }
            verb = action.value.lower()
            msg = _message(
                f"{verb} {ref} ({entry.category.value})",
                action.value,
                ref,
                from_tag,
                entry.tag.value,
                by,
            )
            self._commit_and_audit(
                files, msg, by, ref, action, from_tag, entry.tag.value, reason or verb
            )
            self._entries = entries
            return ref

    def _commit_and_audit(
        self,
        files: dict[str, str],
        message: str,
        by: str,
        ref: str,
        action: AuditAction,
        from_tag: str | None,
        to_tag: str | None,
        reason: str,
    ) -> None:
        before = self._head
        sha = self.store.commit(files, message)
        kind, actor_id = self._actor(by)
        try:
            self.audit.record(
                AuditRecord(
                    kind, actor_id, ref, action, from_tag, to_tag, AuditOutcome.APPLIED, reason, sha
                )
            )
        except Exception:
            self.store.reset_to(before)
            raise
        self._head = sha

    def _refused(self, by: str, ref: str, action: AuditAction, r: _Refusal) -> NoReturn:
        kind, actor_id = self._actor(by)
        self.audit.record(
            AuditRecord(
                kind, actor_id, ref, action, None, None, AuditOutcome.REJECTED, r.code, None
            )
        )
        raise MemoryRefused(r.code, r.detail)

    def _actor(self, by: str) -> tuple[ActorKind, str]:
        if by == SYSTEM:
            return ActorKind.SYSTEM, SYSTEM
        if by.startswith("agent:"):
            return ActorKind.AGENT, by.removeprefix("agent:")[:200] or "unknown"
        person = self._people.get(by)
        return ActorKind.PERSON, str(person.person_id) if person else "unknown"

    def _check_by(self, by: str) -> None:
        if by == SYSTEM:
            return
        if by.startswith("agent:"):
            if not _AGENT_RE.match(by):
                raise _Refusal("bad-agent-name")
            return
        if by not in self._people:
            raise _Refusal("unknown-recorder")

    def _changeable(self, memory_id: str, by: str) -> MemoryEntry:
        self._check_by(by)
        old = self._entries.get(memory_id)
        if old is None:
            raise _Refusal("no-such-entry")
        if by in self._people and not self.can_see(old, by):
            raise _Refusal("no-such-entry")  # do not reveal entries the person cannot see
        if old.tag is MemoryTag.EXPIRED:
            raise _Refusal("entry-expired")
        return old

    def _build(self, **fields: object) -> MemoryEntry:
        try:
            entry = MemoryEntry.model_validate(fields)
            fmt.check_entry(entry)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            raise _Refusal("invalid-entry", problems) from None
        except MemoryFormatError as e:
            raise _Refusal("invalid-entry", e.problem) from None
        return entry

    def _next_number(self) -> int:
        return 1 + max((int(m[2:]) for m in self._entries), default=0)

    def _file_text(self, path: str, entries: dict[str, MemoryEntry]) -> str:
        person = None
        if path.startswith("people/"):
            person = self._people[path.removeprefix("people/").removesuffix(".md")]
        return fmt.render_file(
            path, (e for e in entries.values() if fmt.path_for(e) == path), person
        )

    def _index_text(self, entries: dict[str, MemoryEntry], extra: Iterable[str] = ()) -> str:
        counts: dict[str, tuple[int, int]] = {f"people/{s}.md": (0, 0) for s in self._people}
        counts.update({p: (0, 0) for p in extra})
        for e in entries.values():
            n, x = counts.get(fmt.path_for(e), (0, 0))
            counts[fmt.path_for(e)] = (n + 1, x + (e.tag is MemoryTag.EXPIRED))
        return fmt.render_index(counts)


# --- helpers -------------------------------------------------------------------------------


def _ranked(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    def key(e: MemoryEntry) -> tuple[int, int, int]:
        when = e.happened_on or e.recorded_on
        return (_TAG_RANK[e.tag], -when.toordinal(), -int(e.memory_id[2:]))

    return sorted(entries, key=key)


def _message(
    title: str, action: str, ref: str | None, from_tag: str | None, to_tag: str | None, by: str
) -> str:
    lines = [f"memory: {title}", "", f"Action: {action}"]
    if ref:
        lines.append(f"Memory-Ref: {ref}")
    lines += [f"From: {from_tag or ''}", f"To: {to_tag or ''}", f"Actor: {by}"]
    return "\n".join(lines) + "\n"


def _trailers(c: Commit) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in c.message.splitlines()[1:]:
        key, sep, value = line.partition(": ")
        if sep:
            out[key] = value.strip()
        elif line.endswith(":"):
            out[line[:-1]] = ""
    return out


__all__ = [
    "SYSTEM",
    "AuditRecord",
    "AuditSink",
    "Directory",
    "MemoryEngine",
    "MemoryEngineError",
    "MemoryVersion",
]
