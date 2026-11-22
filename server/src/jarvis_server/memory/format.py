"""The markdown format of the memory repo: layout, strict parsing and canonical writing.

Phase 1 spec §4. Every entry is two lines:

    - [m-0042] [CONFIRMED] [preference] Prefers morning domestic flights
      (about: household; domain: travel; source: msg-7f3a, 2026-11-21; by: manj; ...)

Parsing is strict: any line that does not fit raises MemoryFormatError naming the file and line.
Writing is canonical, so parse(render(x)) == x and render(parse(text)) == text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from jarvis_server.contracts import MemoryCategory, MemoryEntry, MemoryTag, Visibility
from jarvis_server.memory.errors import MemoryFormatError

HOUSEHOLD = "household"
INDEX = "index.md"

PREFERENCE_DOMAINS = ("travel", "restaurants", "shopping", "communication", "lifestyle")
HOUSEHOLD_DOMAINS = ("services", "vendors", "vehicles", "recurring_activities")
OPERATIONAL_DOMAINS = ("routines", "responsibilities", "workflows", "standing_instructions")
LESSONS = "operational/lessons_learned.md"

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_ENTRY_RE = re.compile(r"^- \[(m-[0-9]{4,})\] \[([A-Z]+)\] \[([a-z]+)\] (\S(?:.*\S)?)$")
_META_RE = re.compile(r"^  \((.+)\)$")
_PERSON_ID_RE = re.compile(r"^person_id: ([0-9a-f-]{36})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS = (
    "about",
    "domain",
    "happened on",
    "source",
    "by",
    "confirmed by",
    "visibility",
    "shared with",
    "topics",
)


@dataclass(frozen=True)
class Person:
    """A memory subject that is a person, linked to the ledger (spec §4.2)."""

    slug: str
    person_id: UUID
    display_name: str


@dataclass(frozen=True)
class ParsedFile:
    path: str
    person: Person | None
    entries: tuple[MemoryEntry, ...]


# --- layout --------------------------------------------------------------------------------


def domains_for(category: MemoryCategory) -> tuple[str, ...]:
    if category is MemoryCategory.PREFERENCE:
        return PREFERENCE_DOMAINS
    if category is MemoryCategory.OPERATIONAL:
        return HOUSEHOLD_DOMAINS + OPERATIONAL_DOMAINS
    return ()


def path_for(entry: MemoryEntry) -> str:
    """The one file an entry belongs in (spec §4.1)."""
    c = entry.category
    if c is MemoryCategory.IDENTITY:
        return f"people/{entry.subject}.md"
    if c is MemoryCategory.PREFERENCE:
        return f"preferences/{entry.domain}.md"
    if c is MemoryCategory.EPISODIC:
        assert entry.happened_on is not None
        return f"episodic/{entry.happened_on.year}.md"
    if c is MemoryCategory.OPERATIONAL:
        folder = "household" if entry.domain in HOUSEHOLD_DOMAINS else "operational"
        return f"{folder}/{entry.domain}.md"
    return LESSONS


def title_for(path: str) -> str:
    """Heading of a non-people file."""
    folder, _, name = path.removesuffix(".md").partition("/")
    if path == LESSONS:
        return "Lessons learned"
    if folder == "episodic":
        return f"Episodes {name}"
    return f"{folder.capitalize()}: {name.replace('_', ' ')}"


def is_layout_path(path: str) -> bool:
    if path in (INDEX, LESSONS):
        return True
    folder, sep, name = path.partition("/")
    if not sep or not name.endswith(".md") or "/" in name:
        return False
    stem = name.removesuffix(".md")
    return (
        (folder == "people" and bool(SLUG_RE.match(stem)) and stem != HOUSEHOLD)
        or (folder == "preferences" and stem in PREFERENCE_DOMAINS)
        or (folder == "household" and stem in HOUSEHOLD_DOMAINS)
        or (folder == "operational" and stem in OPERATIONAL_DOMAINS)
        or (folder == "episodic" and bool(re.fullmatch(r"[0-9]{4}", stem)))
    )


def sort_key(entry: MemoryEntry) -> tuple[date, int]:
    when = entry.happened_on if entry.category is MemoryCategory.EPISODIC else None
    return (when or date.min, int(entry.memory_id[2:]))


# --- writing -------------------------------------------------------------------------------


def render_entry(e: MemoryEntry) -> str:
    parts = [f"about: {e.subject}"]
    if e.domain is not None:
        parts.append(f"domain: {e.domain}")
    if e.happened_on is not None:
        parts.append(f"happened on: {e.happened_on.isoformat()}")
    parts.append(f"source: {e.source}, {e.recorded_on.isoformat()}")
    parts.append(f"by: {e.by}")
    if e.confirmed_by is not None:
        assert e.confirmed_on is not None
        parts.append(f"confirmed by: {e.confirmed_by}, {e.confirmed_on.isoformat()}")
    parts.append(f"visibility: {e.visibility.value}")
    if e.shared_with:
        parts.append("shared with: " + ", ".join(e.shared_with))
    if e.topics:
        parts.append("topics: " + ", ".join(e.topics))
    head = f"- [{e.memory_id}] [{e.tag.value}] [{e.category.value}] {e.content}"
    return head + "\n  (" + "; ".join(parts) + ")\n"


def render_file(path: str, entries: Iterable[MemoryEntry], person: Person | None = None) -> str:
    if path.startswith("people/"):
        assert person is not None
        out = f"# {person.display_name}\nperson_id: {person.person_id}\n"
    else:
        out = f"# {title_for(path)}\n"
    body = "".join(render_entry(e) for e in sorted(entries, key=sort_key))
    return out + ("\n" + body if body else "")


def render_index(counts: dict[str, tuple[int, int]]) -> str:
    """counts: path -> (entries, of which expired)."""
    lines = [
        "# Memory index",
        "",
        "Generated by the JARVIS memory engine on every change. Do not edit.",
        "",
        "| File | Entries | Expired |",
        "|---|---|---|",
    ]
    lines += [f"| {p} | {n} | {x} |" for p, (n, x) in sorted(counts.items())]
    return "\n".join(lines) + "\n"


# --- parsing -------------------------------------------------------------------------------


def _date(value: str, path: str, line: int) -> date:
    if not _DATE_RE.match(value):
        raise MemoryFormatError(path, line, f"bad date {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise MemoryFormatError(path, line, f"bad date {value!r}") from e


def _meta(text: str, path: str, line: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split("; "):
        key, sep, value = part.partition(": ")
        if not sep or key not in _KEYS:
            raise MemoryFormatError(path, line, f"unknown or malformed field {part!r}")
        if key in out:
            raise MemoryFormatError(path, line, f"field {key!r} given twice")
        out[key] = value
    return out


def _entry(
    head: re.Match[str], meta: dict[str, str], path: str, line: int, meta_line: int
) -> MemoryEntry:
    """Shape and rule problems are reported on `line`; metadata problems on `meta_line`."""
    memory_id, tag, category, content = head.groups()
    for required in ("about", "source", "by", "visibility"):
        if required not in meta:
            raise MemoryFormatError(path, meta_line, f"missing field {required!r}")
    source, sep, recorded = meta["source"].rpartition(", ")
    if not sep:
        raise MemoryFormatError(path, meta_line, "source must be '<source>, <date>'")
    fields: dict[str, Any] = {
        "memory_id": memory_id,
        "tag": tag,
        "category": category,
        "content": content,
        "subject": meta["about"],
        "domain": meta.get("domain"),
        "source": source,
        "recorded_on": _date(recorded, path, meta_line),
        "by": meta["by"],
        "visibility": meta["visibility"],
        "shared_with": tuple(meta["shared with"].split(", ")) if "shared with" in meta else (),
        "topics": tuple(meta["topics"].split(", ")) if "topics" in meta else (),
    }
    if "happened on" in meta:
        fields["happened_on"] = _date(meta["happened on"], path, meta_line)
    if "confirmed by" in meta:
        who, sep, when = meta["confirmed by"].rpartition(", ")
        if not sep:
            raise MemoryFormatError(path, meta_line, "confirmed by must be '<person>, <date>'")
        fields["confirmed_by"] = who
        fields["confirmed_on"] = _date(when, path, meta_line)
    try:
        entry = MemoryEntry.model_validate(fields)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise MemoryFormatError(path, line, problems) from e
    check_entry(entry, path, line)
    if path_for(entry) != path:
        raise MemoryFormatError(path, line, f"entry belongs in {path_for(entry)}")
    return entry


def check_entry(entry: MemoryEntry, path: str = "<entry>", line: int = 0) -> None:
    """Rules beyond the contract shape that every stored entry must keep."""

    def bad(problem: str) -> MemoryFormatError:
        return MemoryFormatError(path, line, problem)

    allowed = domains_for(entry.category)
    if allowed and entry.domain not in allowed:
        raise bad(f"{entry.category.value} needs a domain from {', '.join(allowed)}")
    if not allowed and entry.domain is not None:
        raise bad(f"{entry.category.value} takes no domain")
    if (entry.category is MemoryCategory.EPISODIC) != (entry.happened_on is not None):
        raise bad("happened on is required for episodic entries and only for them")
    if entry.happened_on is not None and entry.happened_on.year < 1000:
        raise bad("happened on must be a four-digit year")
    if entry.category is MemoryCategory.IDENTITY and entry.subject == HOUSEHOLD:
        raise bad("identity entries are about a person, not the household")
    if entry.visibility not in (
        Visibility.PRIVATE,
        Visibility.SELECTED_PERSON,
        Visibility.FAMILY_SHARED,
    ):
        raise bad(f"visibility {entry.visibility.value} is not used in Phase 1")
    if (entry.visibility is Visibility.SELECTED_PERSON) != bool(entry.shared_with):
        raise bad("shared with is required for SELECTED_PERSON and only for it")
    if (entry.tag is MemoryTag.CONFIRMED) != (entry.confirmed_by is not None):
        raise bad("confirmed by is present exactly when the tag is CONFIRMED")
    if len(set(entry.topics)) != len(entry.topics) or len(set(entry.shared_with)) != len(
        entry.shared_with
    ):
        raise bad("duplicate topic or shared-with name")
    if entry.content != entry.content.strip() or any(ord(ch) < 32 for ch in entry.content):
        raise bad("content must be one clean line")


def parse_file(path: str, text: str) -> ParsedFile:
    """Parse one memory file strictly. `path` is relative to the repo root."""
    if not is_layout_path(path) or path == INDEX:
        raise MemoryFormatError(path, 0, "not a memory file in the layout")
    if not text.endswith("\n"):
        raise MemoryFormatError(path, 0, "file must end with a newline")
    lines = text.split("\n")[:-1]
    if not lines or not lines[0].startswith("# "):
        raise MemoryFormatError(path, 1, "first line must be '# <title>'")
    person: Person | None = None
    i = 1
    if path.startswith("people/"):
        display_name = lines[0][2:]
        if not display_name or len(display_name) > 100 or display_name != display_name.strip():
            raise MemoryFormatError(path, 1, "bad display name")
        m = _PERSON_ID_RE.match(lines[1]) if len(lines) > 1 else None
        if m is None:
            raise MemoryFormatError(path, 2, "second line must be 'person_id: <uuid>'")
        try:
            pid = UUID(m.group(1))
        except ValueError as e:
            raise MemoryFormatError(path, 2, "bad person_id") from e
        person = Person(path.removeprefix("people/").removesuffix(".md"), pid, display_name)
        i = 2
    elif lines[0] != f"# {title_for(path)}":
        raise MemoryFormatError(path, 1, f"title must be '# {title_for(path)}'")

    entries: list[MemoryEntry] = []
    while i < len(lines):
        line = lines[i]
        if line == "":
            i += 1
            continue
        head = _ENTRY_RE.match(line)
        if head is None:
            raise MemoryFormatError(path, i + 1, "expected an entry line '- [m-NNNN] ...'")
        meta = _META_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
        if meta is None:
            raise MemoryFormatError(path, i + 2, "expected the entry's '  (...)' line")
        entry = _entry(head, _meta(meta.group(1), path, i + 2), path, i + 1, i + 2)
        if person is not None and entry.subject != person.slug:
            raise MemoryFormatError(path, i + 1, f"entry is about {entry.subject!r}")
        entries.append(entry)
        i += 2
    return ParsedFile(path, person, tuple(entries))
