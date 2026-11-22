"""Memory file format (spec §4) and word matching (spec §6): no database, no git."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from jarvis_server.contracts import MemoryCategory, MemoryEntry, MemoryTag, Visibility
from jarvis_server.memory import format as fmt
from jarvis_server.memory import text
from jarvis_server.memory.errors import MemoryFormatError

PERSON = fmt.Person("daughter", UUID("00000000-0000-4000-8000-00000000d001"), "Daughter")


def entry(**kw: object) -> MemoryEntry:
    base: dict[str, object] = {
        "memory_id": "m-0042",
        "subject": "household",
        "category": MemoryCategory.PREFERENCE,
        "domain": "travel",
        "tag": MemoryTag.CONFIRMED,
        "content": "Prefers morning domestic flights",
        "source": "msg-7f3a",
        "recorded_on": date(2026, 11, 21),
        "by": "manj",
        "confirmed_by": "manj",
        "confirmed_on": date(2026, 11, 22),
        "visibility": Visibility.FAMILY_SHARED,
        "topics": ("travel", "flights"),
    }
    base.update(kw)
    return MemoryEntry.model_validate(base)


CANONICAL = (
    "# Preferences: travel\n"
    "\n"
    "- [m-0042] [CONFIRMED] [preference] Prefers morning domestic flights\n"
    "  (about: household; domain: travel; source: msg-7f3a, 2026-11-21; by: manj;"
    " confirmed by: manj, 2026-11-22; visibility: FAMILY_SHARED; topics: travel, flights)\n"
)


def test_spec_example_renders_exactly() -> None:
    assert fmt.render_file("preferences/travel.md", [entry()]) == CANONICAL


def test_round_trip_both_ways() -> None:
    parsed = fmt.parse_file("preferences/travel.md", CANONICAL)
    assert parsed.entries == (entry(),)
    assert fmt.render_file("preferences/travel.md", parsed.entries) == CANONICAL


def test_people_file_round_trip_with_every_field() -> None:
    entries = [
        entry(
            memory_id="m-0007",
            subject="daughter",
            category=MemoryCategory.IDENTITY,
            domain=None,
            tag=MemoryTag.STATED,
            confirmed_by=None,
            confirmed_on=None,
            visibility=Visibility.SELECTED_PERSON,
            shared_with=("manj",),
            topics=(),
            content="Plays the veena; [brackets] and: colons; are fine",
        ),
    ]
    text_ = fmt.render_file("people/daughter.md", entries, PERSON)
    parsed = fmt.parse_file("people/daughter.md", text_)
    assert parsed.person == PERSON and list(parsed.entries) == entries
    assert fmt.render_file("people/daughter.md", parsed.entries, PERSON) == text_


def test_episodic_entries_sorted_by_date() -> None:
    a = entry(
        memory_id="m-0002",
        category=MemoryCategory.EPISODIC,
        domain=None,
        happened_on=date(2026, 12, 20),
        tag=MemoryTag.HISTORICAL,
        confirmed_by=None,
        confirmed_on=None,
    )
    b = a.model_copy(update={"memory_id": "m-0003", "happened_on": date(2026, 1, 5)})
    out = fmt.render_file("episodic/2026.md", [a, b])
    assert out.index("m-0003") < out.index("m-0002")
    assert fmt.parse_file("episodic/2026.md", out).entries == (b, a)


@pytest.mark.parametrize(
    ("damage", "line", "fragment"),
    [
        (lambda t: t.replace("# Preferences: travel", "# Travel"), 1, "title"),
        (lambda t: t.replace("[CONFIRMED]", "[MAYBE]"), 3, "tag"),
        (lambda t: t.replace("  (about", "(about"), 4, "'  (...)'"),
        (lambda t: t.replace("by: manj; ", "by: manj; colour: red; "), 4, "colour"),
        (lambda t: t.replace("2026-11-21", "21-11-2026"), 4, "bad date"),
        (lambda t: t.replace("; by: manj", ""), 4, "missing field 'by'"),
        (lambda t: t + "stray text\n", 5, "expected an entry line"),
        (lambda t: t.replace("domain: travel", "domain: lifestyle"), 3, "belongs in"),
        (lambda t: t.replace("FAMILY_SHARED", "ORGANIZATION_SHARED"), 3, "Phase 1"),
        (lambda t: t.replace("confirmed by: manj, 2026-11-22; ", ""), 3, "confirmed by"),
        (lambda t: t.rstrip("\n"), 0, "newline"),
    ],
)
def test_damaged_file_names_file_and_line(damage, line: int, fragment: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(MemoryFormatError) as err:
        fmt.parse_file("preferences/travel.md", damage(CANONICAL))
    assert err.value.path == "preferences/travel.md"
    assert err.value.line == line
    assert fragment in err.value.problem


@pytest.mark.parametrize(
    "path",
    ["notes.md", "people/household.md", "preferences/food.md", "episodic/26.md", "a/b/c.md"],
)
def test_files_outside_the_layout_are_refused(path: str) -> None:
    with pytest.raises(MemoryFormatError):
        fmt.parse_file(path, "# x\n")


def test_entry_in_wrong_persons_file() -> None:
    e = entry(
        memory_id="m-0001",
        subject="manj",
        category=MemoryCategory.IDENTITY,
        domain=None,
        tag=MemoryTag.STATED,
        confirmed_by=None,
        confirmed_on=None,
    )
    bad = fmt.render_file("people/daughter.md", [], PERSON) + "\n" + fmt.render_entry(e)
    with pytest.raises(MemoryFormatError, match=r"belongs in people/manj\.md"):
        fmt.parse_file("people/daughter.md", bad)


@pytest.mark.parametrize(
    ("category", "domain", "expected"),
    [
        (MemoryCategory.PREFERENCE, "travel", "preferences/travel.md"),
        (MemoryCategory.OPERATIONAL, "vendors", "household/vendors.md"),
        (
            MemoryCategory.OPERATIONAL,
            "standing_instructions",
            "operational/standing_instructions.md",
        ),
        (MemoryCategory.LEARNED, None, "operational/lessons_learned.md"),
    ],
)
def test_layout_table(category: MemoryCategory, domain: str | None, expected: str) -> None:
    e = entry(
        category=category, domain=domain, tag=MemoryTag.STATED, confirmed_by=None, confirmed_on=None
    )
    assert fmt.path_for(e) == expected
    fmt.check_entry(e)


# --- word matching (spec §6 step 5) -------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "content", "topics", "hit"),
    [
        ("school trip", "Gets travel-sick on long bus rides", ("travel",), True),
        ("school trip", "Loves dosa at the corner cafe", ("food",), False),
        ("book flights", "Prefers aisle seats", ("travel",), True),
        ("Exams next week", "Studies best in the morning", ("school",), True),
        ("dinner plans", "Vegetarian", ("restaurant",), True),
        ("the and for", "the and for", (), False),
        ("activities", "recurring activity", (), True),
    ],
)
def test_matching(task: str, content: str, topics: tuple[str, ...], hit: bool) -> None:
    assert text.matches(text.terms(task), content, topics) is hit


@pytest.mark.parametrize("word", sorted(text.ALIASES))
def test_every_alias_is_folded_and_reaches_its_topic(word: str) -> None:
    assert text.fold(word) == word
    assert text.ALIASES[word] in text.terms(word)


def test_plural_folding() -> None:
    assert [text.fold(w) for w in ("trips", "activities", "class", "bus", "rides")] == [
        "trip",
        "activity",
        "class",
        "bus",
        "ride",
    ]
