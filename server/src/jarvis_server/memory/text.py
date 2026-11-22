"""Word matching for retrieval and search (Phase 1 spec §6, step 5). Deterministic, no AI.

Words are runs of letters, marks and digits in any script (Kannada, Devanagari, Latin ...),
normalised with NFKC and case-folded; accents on Latin letters are dropped ("café" ->
"cafe"). Stop-words are dropped and simple English plurals folded ("trips" -> "trip").
A small fixed alias table maps common words to a canonical topic word, so "school trip"
also matches entries tagged "travel".
"""

from __future__ import annotations

import unicodedata

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


def _normalise(text_: str) -> str:
    """NFKC + casefold; drop combining accents that sit on Latin letters only."""
    decomposed = unicodedata.normalize("NFKD", text_.casefold())
    out: list[str] = []
    for ch in decomposed:
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def _split(text_: str) -> list[str]:
    """Runs of letters (L*), marks (M*) and digits (N*)."""
    found: list[str] = []
    current: list[str] = []
    for ch in text_:
        if unicodedata.category(ch)[0] in "LMN":
            current.append(ch)
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return found


def fold(word: str) -> str:
    """Fold a simple English plural to its singular (ASCII words only)."""
    if not word.isascii():
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def words(text: str) -> set[str]:
    """The folded, stop-word-free words of a text."""
    return {fold(w) for w in _split(_normalise(text)) if w not in STOP_WORDS}


def terms(text: str) -> set[str]:
    """Words plus their canonical alias, ready to compare."""
    found = words(text)
    return found | {ALIASES[w] for w in found if w in ALIASES}


def entry_terms(content: str, topics: tuple[str, ...]) -> frozenset[str]:
    """What an entry can be matched on; the engine computes this once per entry version."""
    return frozenset(terms(content) | terms(" ".join(topics)))


def matches(task_terms: set[str], content: str, topics: tuple[str, ...]) -> bool:
    return bool(task_terms & entry_terms(content, topics))
