"""The pre-push check: plain code the agent can't reach decides whether a patch may be pushed."""

from __future__ import annotations

import pytest

from jarvis_server.improve import gate
from jarvis_server.improve.gate import ApprovedDesign, Patch

DESIGN = ApprovedDesign(
    files=frozenset(
        {"server/src/jarvis_server/brain/research.py", "server/tests/test_research.py"}
    ),
    packages=frozenset(),
)


def _patch(**kw: object) -> Patch:
    base: dict[str, object] = {
        "changed": ("server/src/jarvis_server/brain/research.py",),
        "diff": "+    return better_answer\n",
        "commit_messages": ("Research answers cite three sources",),
        "lock_texts": {},
    }
    base.update(kw)
    return Patch(**base)  # type: ignore[arg-type]


def test_a_clean_patch_passes() -> None:
    assert gate.check(DESIGN, _patch(), fingerprints={}, current_hashes={}, evidence=()) == []


@pytest.mark.parametrize(
    "path",
    [
        "server/src/jarvis_server/policy.py",
        "server/src/jarvis_server/gateway/app.py",
        "server/src/jarvis_server/improve/gate.py",
        "server/migrations/versions/0018_x.py",
        "server/deploy/compose.yaml",
        "server/Dockerfile",
        "server/pyproject.toml",
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        "CLAUDE.md",
        "app/CLAUDE.md",
        "app/src-tauri/tauri.conf.json",
        "app/src-tauri/capabilities/default.json",
        "crates/jarvis-node/src/canvas.rs",
        "crates/jarvis-node/src/keystore.rs",
        "config/secrets/x.txt",
    ],
)
def test_a_protected_path_is_refused_even_when_the_design_names_it(path: str) -> None:
    design = ApprovedDesign(files=frozenset({path}), packages=frozenset())
    problems = gate.check(
        design, _patch(changed=(path,)), fingerprints={}, current_hashes={}, evidence=()
    )
    assert any("protected" in p and path in p for p in problems)


def test_a_file_the_design_does_not_name_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(changed=("server/src/jarvis_server/brain/other.py",)),
        fingerprints={},
        current_hashes={},
        evidence=(),
    )
    assert problems == [
        "server/src/jarvis_server/brain/other.py is not named in the approved design"
    ]


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "server\\x.py", "a/./b.py", ""])
def test_a_strange_path_is_refused(path: str) -> None:
    design = ApprovedDesign(files=frozenset({path}), packages=frozenset())
    problems = gate.check(
        design, _patch(changed=(path,)), fingerprints={}, current_hashes={}, evidence=()
    )
    assert any("not a plain repo path" in p for p in problems)


def test_a_changed_fingerprinted_test_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(changed=("server/tests/test_research.py",)),
        fingerprints={"server/tests/test_research.py": "aaa"},
        current_hashes={"server/tests/test_research.py": "bbb"},
        evidence=(),
    )
    assert problems == [
        "server/tests/test_research.py changed after the Test writer fingerprinted it"
    ]


def test_a_deleted_fingerprinted_test_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(changed=()),
        fingerprints={"server/tests/test_research.py": "aaa"},
        current_hashes={},
        evidence=(),
    )
    assert problems == [
        "server/tests/test_research.py changed after the Test writer fingerprinted it"
    ]


def test_fingerprint_is_sha256_of_the_bytes() -> None:
    assert gate.fingerprint(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


UV_OLD = '[[package]]\nname = "httpx"\nversion = "0.27.0"\n'
UV_NEW = UV_OLD + '\n[[package]]\nname = "leftpad"\nversion = "1.0.0"\n'


def test_an_unnamed_new_package_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(changed=("server/uv.lock",), lock_texts={"server/uv.lock": (UV_OLD, UV_NEW)}),
        fingerprints={},
        current_hashes={},
        evidence=(),
    )
    assert problems == ["server/uv.lock adds or changes leftpad, which the design does not name"]


def test_a_named_new_package_passes_without_naming_the_lock_file() -> None:
    design = ApprovedDesign(files=DESIGN.files, packages=frozenset({"leftpad"}))
    patch = _patch(changed=("server/uv.lock",), lock_texts={"server/uv.lock": (UV_OLD, UV_NEW)})
    assert gate.check(design, patch, fingerprints={}, current_hashes={}, evidence=()) == []


def test_an_upgraded_package_counts_as_a_change() -> None:
    new = UV_OLD.replace("0.27.0", "0.28.1")
    problems = gate.check(
        DESIGN,
        _patch(changed=("server/uv.lock",), lock_texts={"server/uv.lock": (UV_OLD, new)}),
        fingerprints={},
        current_hashes={},
        evidence=(),
    )
    assert problems == ["server/uv.lock adds or changes httpx, which the design does not name"]


def test_cargo_lock_is_read_the_same_way() -> None:
    old = '[[package]]\nname = "serde"\nversion = "1.0.0"\n'
    new = old + '\n[[package]]\nname = "evil"\nversion = "0.1.0"\n'
    problems = gate.check(
        DESIGN,
        _patch(changed=("Cargo.lock",), lock_texts={"Cargo.lock": (old, new)}),
        fingerprints={},
        current_hashes={},
        evidence=(),
    )
    assert problems == ["Cargo.lock adds or changes evil, which the design does not name"]


def test_package_lock_json_is_read_too() -> None:
    old = '{"packages": {"": {}, "node_modules/a": {"version": "1.0.0"}}}'
    new = old.replace("}}}", '}, "node_modules/b": {"version": "2.0.0"}}}')
    problems = gate.check(
        DESIGN,
        _patch(
            changed=("app/package-lock.json",), lock_texts={"app/package-lock.json": (old, new)}
        ),
        fingerprints={},
        current_hashes={},
        evidence=(),
    )
    assert problems == ["app/package-lock.json adds or changes b, which the design does not name"]


def test_a_changed_lock_file_without_its_texts_is_refused() -> None:
    problems = gate.check(
        DESIGN, _patch(changed=("Cargo.lock",)), fingerprints={}, current_hashes={}, evidence=()
    )
    assert problems == ["Cargo.lock changed but its before and after texts were not given"]


def test_private_evidence_in_the_diff_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(diff="+ # user said: Call my sister about the loan\n"),
        fingerprints={},
        current_hashes={},
        evidence=("call my sister about the loan",),
    )
    assert problems == ["the diff quotes the ticket's private evidence"]


def test_private_evidence_in_a_commit_message_is_refused() -> None:
    problems = gate.check(
        DESIGN,
        _patch(commit_messages=("Fix: Call my sister about the loan",)),
        fingerprints={},
        current_hashes={},
        evidence=("call my sister about the loan",),
    )
    assert problems == ["a commit message quotes the ticket's private evidence"]


def test_short_evidence_fragments_do_not_trip_the_check() -> None:
    problems = gate.check(
        DESIGN, _patch(), fingerprints={}, current_hashes={}, evidence=("the", "")
    )
    assert problems == []


def test_evidence_is_matched_ignoring_spacing() -> None:
    problems = gate.check(
        DESIGN,
        _patch(diff="+ call   my\nsister about the loan\n"),
        fingerprints={},
        current_hashes={},
        evidence=("call my sister about the loan",),
    )
    assert problems == ["the diff quotes the ticket's private evidence"]


def test_an_empty_patch_is_refused() -> None:
    problems = gate.check(
        DESIGN, _patch(changed=(), diff=""), fingerprints={}, current_hashes={}, evidence=()
    )
    assert problems == ["the patch changes nothing"]


def test_evidence_split_across_diff_lines_is_still_found() -> None:
    problems = gate.check(
        DESIGN,
        _patch(diff="+ call my\n+sister about the loan\n"),
        fingerprints={},
        current_hashes={},
        evidence=("call my sister about the loan",),
    )
    assert problems == ["the diff quotes the ticket's private evidence"]
