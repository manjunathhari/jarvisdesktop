"""The pre-push check.

This is plain code in the orchestrator, outside the sandbox where the agents run, so nothing an
agent writes can change it. It refuses a patch unless every changed file is named in the approved
design, none is protected, the Test writer's files still match their fingerprints, lock files
change only for packages the design names, and nothing in the diff or the commit messages quotes
the ticket's private evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# What JARVIS can never change about himself. The team may propose a change here in a design;
# the patch is still refused, and the change is built by hand after Manj's separate yes.
PROTECTED: tuple[str, ...] = (
    # Safety and approvals
    "server/src/jarvis_server/policy.py",
    "server/src/jarvis_server/signing.py",
    "server/src/jarvis_server/approvals_db.py",
    "server/src/jarvis_server/bootstrap.py",
    "server/src/jarvis_server/core/approvals.py",
    "server/src/jarvis_server/core/keys.py",
    "server/src/jarvis_server/core/node_signature.py",
    "server/src/jarvis_server/core/turn.py",
    "server/src/jarvis_server/core/dispatch.py",
    "server/src/jarvis_server/core/prompt.py",
    "server/src/jarvis_server/core/identity.py",
    "server/src/jarvis_server/gateway/**",
    "server/src/jarvis_server/brain/budget.py",
    # Database and build
    "server/migrations/**",
    "server/pyproject.toml",
    # The team's own rules
    "server/src/jarvis_server/improve/**",
    "server/tests/improve/**",
    "server/tests/test_compose.py",
    ".claude/**",
    "CLAUDE.md",
    "**/CLAUDE.md",
    # Secrets, deploy and CI
    "server/deploy/**",
    "server/Dockerfile",
    ".github/**",
    "contracts/signing-vectors/**",
    "**/secrets/**",
    # Desktop seal and keys
    "app/src-tauri/capabilities/**",
    "app/src-tauri/tauri.conf.json",
    "app/src-tauri/src/board.rs",
    "app/src-tauri/src/key.rs",
    "crates/jarvis-node/src/canvas.rs",
    "**/canvas_seal.js",
    "**/keystore.rs",
    "**/sign.rs",
    "**/trust.rs",
    "**/approval.rs",
)

LOCK_FILES = frozenset({"uv.lock", "Cargo.lock", "package-lock.json"})

# Evidence shorter than this is too common to mean anything ("the", "ok").
_MIN_EVIDENCE = 12


@dataclass(frozen=True)
class ApprovedDesign:
    """What Manj approved: the files the change may touch and the packages it may add."""

    files: frozenset[str]
    packages: frozenset[str]


@dataclass(frozen=True)
class Patch:
    """A patch as the orchestrator sees it after applying it to its own clone."""

    changed: tuple[str, ...]
    diff: str
    commit_messages: tuple[str, ...]
    # For each changed lock file: (text on main, text with the patch applied).
    lock_texts: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    # Paths the patch makes into a symlink or a submodule.
    special: tuple[str, ...] = ()


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_protected(path: str) -> bool:
    return any(fnmatchcase(path, pattern) or _under(path, pattern) for pattern in PROTECTED)


def _under(path: str, pattern: str) -> bool:
    # fnmatch's ** is just *, which already crosses "/"; also let "dir/**" match "dir" itself.
    return pattern.endswith("/**") and path == pattern[:-3]


def is_plain(path: str) -> bool:
    if not path or "\\" in path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def lock_packages(path: str, text: str) -> set[tuple[str, str]]:
    """The (name, version) pairs a lock file pins."""
    name = PurePosixPath(path).name
    if name == "package-lock.json":
        packages = json.loads(text).get("packages", {})
        return {
            (key.rsplit("node_modules/", 1)[-1], str(meta.get("version", "")))
            for key, meta in packages.items()
            if key
        }
    data = tomllib.loads(text)
    return {(str(p["name"]), str(p.get("version", ""))) for p in data.get("package", [])}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def check(
    design: ApprovedDesign,
    patch: Patch,
    *,
    fingerprints: Mapping[str, str],
    current_hashes: Mapping[str, str],
    evidence: Sequence[str],
) -> list[str]:
    """Every reason to refuse the patch, in a stable order. An empty list means it may go."""
    problems: list[str] = []
    if not patch.changed and not patch.diff.strip():
        return ["the patch changes nothing"]

    for path in patch.changed:
        if not is_plain(path):
            problems.append(f"{path!r} is not a plain repo path")
        elif is_protected(path):
            problems.append(f"{path} is protected and can only be changed by hand")
        elif PurePosixPath(path).name in LOCK_FILES:
            problems.extend(_lock_problems(design, patch, path))
        elif path not in design.files:
            problems.append(f"{path} is not named in the approved design")

    problems.extend(
        f"{path} is a symlink or submodule, which is never allowed" for path in patch.special
    )
    for path, expected in sorted(fingerprints.items()):
        if current_hashes.get(path) != expected:
            problems.append(f"{path} changed after the Test writer fingerprinted it")

    if quotes(_diff_text(patch.diff), evidence):
        problems.append("the diff quotes the ticket's private evidence")
    if any(quotes(m, evidence) for m in patch.commit_messages):
        problems.append("a commit message quotes the ticket's private evidence")
    return problems


def quotes(text: str, evidence: Sequence[str]) -> bool:
    """Whether `text` contains any piece of the ticket's private evidence, ignoring spacing."""
    squashed = _squash(text)
    return any(
        len(needle) >= _MIN_EVIDENCE and needle in squashed for needle in map(_squash, evidence)
    )


def _diff_text(diff: str) -> str:
    """The diff's content without the +, - and space markers that start each line."""
    return "\n".join(line[1:] if line[:1] in "+- " else line for line in diff.splitlines())


def _lock_problems(design: ApprovedDesign, patch: Patch, path: str) -> list[str]:
    texts = patch.lock_texts.get(path)
    if texts is None:
        return [f"{path} changed but its before and after texts were not given"]
    old, new = (lock_packages(path, t) for t in texts)
    touched = sorted({name for name, _ in new - old} - design.packages)
    if touched:
        return [f"{path} adds or changes {', '.join(touched)}, which the design does not name"]
    return []
