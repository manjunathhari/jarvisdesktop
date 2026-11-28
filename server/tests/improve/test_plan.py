"""The design's plan block: the files a ticket may edit, its tests, packages and done-when."""

from __future__ import annotations

import pytest

from jarvis_server.improve import plan
from jarvis_server.improve.gate import ApprovedDesign

GOOD = """# Research answers are too thin

Problem, cause and change in prose.

```improve-plan
{
  "files": ["server/src/jarvis_server/brain/research.py"],
  "tests": ["server/tests/test_research_depth.py"],
  "packages": [{"name": "trafilatura", "licence": "Apache-2.0"}],
  "done_when": "A research answer cites at least three sources and the new tests pass."
}
```
"""


def _without_done_when() -> str:
    start = GOOD.index(',\n  "done_when"')
    end = GOOD.index("\n}\n```")
    return GOOD[:start] + GOOD[end:]


def test_a_good_plan_is_read() -> None:
    p = plan.parse(GOOD)
    assert p.files == ("server/src/jarvis_server/brain/research.py",)
    assert p.tests == ("server/tests/test_research_depth.py",)
    assert p.packages == (plan.Package("trafilatura", "Apache-2.0"),)
    assert p.done_when.startswith("A research answer cites")


def test_the_approved_design_allows_the_files_and_tests_and_names_the_packages() -> None:
    assert plan.parse(GOOD).approved() == ApprovedDesign(
        files=frozenset(
            {"server/src/jarvis_server/brain/research.py", "server/tests/test_research_depth.py"}
        ),
        packages=frozenset({"trafilatura"}),
    )


def test_the_fingerprint_covers_the_whole_design_text() -> None:
    assert plan.fingerprint(GOOD) != plan.fingerprint(GOOD.replace("prose", "words"))
    assert len(plan.fingerprint(GOOD)) == 64


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("# no block\n", "has no improve-plan block"),
        (GOOD + GOOD, "has more than one improve-plan block"),
        (GOOD.replace('"files": [', '"files": [1, '), "files must be a list of paths"),
        (GOOD.replace('"done_when": "A research', '"done_when": "  ", "x": "'), "unknown key x"),
        (_without_done_when(), "needs a done_when line"),
        (GOOD.replace("server/src/jarvis_server/brain/research.py", "../x.py"), "not a plain"),
        (
            GOOD.replace("server/src/jarvis_server/brain/research.py", "server/Dockerfile"),
            "protected",
        ),
        (GOOD.replace('"licence": "Apache-2.0"', '"licence": ""'), "needs a licence"),
        (
            GOOD.replace("server/tests/test_research_depth.py", "server/src/x.py"),
            "is not in a test folder",
        ),
        (GOOD.replace('{\n  "files"', '{\n  "extra": 1,\n  "files"'), "unknown key extra"),
        (GOOD.replace("```improve-plan\n{", "```improve-plan\n{{"), "is not valid JSON"),
    ],
)
def test_a_bad_plan_is_refused_with_a_reason(text: str, why: str) -> None:
    with pytest.raises(plan.BadPlan, match=why):
        plan.parse(text)


def test_a_plan_with_no_files_is_refused() -> None:
    text = GOOD.replace('["server/src/jarvis_server/brain/research.py"]', "[]")
    with pytest.raises(plan.BadPlan, match="names no files"):
        plan.parse(text)


@pytest.mark.parametrize(
    "path",
    [
        "server/tests/test_x.py",
        "server/tests/improve/test_x.py",
        "crates/jarvis-node/tests/canvas_flow.rs",
        "app/src-tauri/tests/smoke.rs",
        "evals/cases/research.yaml",
    ],
)
def test_test_folders(path: str) -> None:
    assert plan.is_test_path(path)
