"""Word matching for retrieval and search (Phase 1 spec §6, step 5). Deterministic, no AI.

Words are lower-cased runs of letters and digits; stop-words are dropped; simple plurals are
folded ("trips" -> "trip", "activities" -> "activity"). A small fixed alias table maps common
words to a canonical topic word, so "school trip" also matches entries tagged "travel".
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")

STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
    ]
)

# word -> canonical topic word. Both sides are already folded. Keep small; every entry is tested.
ALIASES: dict[str, str] = {
    "trip": "travel",
    "flight": "travel",
    "holiday": "travel",
    "vacation": "travel",
    "journey": "travel",
    "airport": "travel",
    "exam": "school",
    "homework": "school",
    "teacher": "school",
    "class": "school",
    "dinner": "restaurant",
    "lunch": "restaurant",
    "breakfast": "restaurant",
    "shopping": "shop",
    "buy": "shop",
    "doctor": "health",
    "appointment": "health",
}


def fold(word: str) -> str:
    """Fold a simple English plural to its singular."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def words(text: str) -> set[str]:
    """The folded, stop-word-free words of a text."""
    return {fold(w) for w in _WORD.findall(text.lower()) if w not in STOP_WORDS}


def terms(text: str) -> set[str]:
    """Words plus their canonical alias, ready to compare."""
    found = words(text)
    return found | {ALIASES[w] for w in found if w in ALIASES}


def matches(task_terms: set[str], content: str, topics: tuple[str, ...]) -> bool:
    entry_terms = terms(content) | terms(" ".join(topics))
    return bool(task_terms & entry_terms)
