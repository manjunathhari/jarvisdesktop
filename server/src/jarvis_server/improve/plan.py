"""The plan block every design ends with.

The design lead writes prose for Manj and one fenced ```improve-plan JSON block for the code: the
only files the ticket may edit, the tests the Test writer adds, any new package with its licence,
and one "done when" line. Manj's yes is bound to the fingerprint of the whole design text, and the
pre-push check and the agents' own guard both read their limits from this block.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from jarvis_server.improve import gate

_BLOCK = re.compile(r"^```improve-plan[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_KEYS = frozenset({"files", "tests", "packages", "done_when"})

TEST_FOLDERS: tuple[str, ...] = (
    "server/tests/**",
    "crates/*/tests/**",
    "app/src-tauri/tests/**",
    "evals/**",
)


class BadPlan(ValueError):
    """The design's plan block is missing or malformed; the design goes back to the lead."""


@dataclass(frozen=True)
class Package:
    name: str
    licence: str


@dataclass(frozen=True)
class Plan:
    files: tuple[str, ...]
    tests: tuple[str, ...]
    packages: tuple[Package, ...]
    done_when: str

    def approved(self) -> gate.ApprovedDesign:
        return gate.ApprovedDesign(
            files=frozenset(self.files) | frozenset(self.tests),
            packages=frozenset(p.name for p in self.packages),
        )


def fingerprint(design_text: str) -> str:
    return hashlib.sha256(design_text.encode()).hexdigest()


def is_test_path(path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in TEST_FOLDERS)


def parse(design_text: str) -> Plan:
    blocks = _BLOCK.findall(design_text)
    if not blocks:
        raise BadPlan("the design has no improve-plan block")
    if len(blocks) > 1:
        raise BadPlan("the design has more than one improve-plan block")
    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as e:
        raise BadPlan(f"the improve-plan block is not valid JSON: {e.msg}") from None
    if not isinstance(data, dict):
        raise BadPlan("the improve-plan block must be a JSON object")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise BadPlan(f"the improve-plan block has unknown key {', '.join(unknown)}")

    files = _paths(data, "files")
    if not files:
        raise BadPlan("the plan names no files")
    tests = _paths(data, "tests")
    for path in tests:
        if not is_test_path(path):
            raise BadPlan(f"{path} is not in a test folder")
    done_when = data.get("done_when")
    if not isinstance(done_when, str) or not done_when.strip():
        raise BadPlan("the plan needs a done_when line")
    return Plan(files, tests, _packages(data), done_when.strip())


def _paths(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise BadPlan(f"{key} must be a list of paths")
    for path in value:
        if not gate.is_plain(path):
            raise BadPlan(f"{path!r} is not a plain repo path")
        if gate.is_protected(path):
            raise BadPlan(f"{path} is protected and can only be changed by hand")
    return tuple(dict.fromkeys(value))


def _packages(data: dict[str, Any]) -> tuple[Package, ...]:
    value = data.get("packages", [])
    if not isinstance(value, list):
        raise BadPlan("packages must be a list")
    out: list[Package] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise BadPlan("each package needs a name")
        licence = item.get("licence")
        if not isinstance(licence, str) or not licence.strip():
            raise BadPlan(f"package {item['name']} needs a licence")
        out.append(Package(item["name"], licence.strip()))
    return tuple(out)


def strip_block(design_text: str) -> str:
    """The design's prose without its plan block."""
    return _BLOCK.sub("", design_text).strip()
