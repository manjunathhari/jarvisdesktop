"""Each role gets only its listed tools, skills and limits, and its guard enforces them."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_server.improve import guard, team
from jarvis_server.improve.plan import Package, Plan

PLAN = Plan(
    files=("server/src/jarvis_server/brain/research.py",),
    tests=("server/tests/test_research_depth.py",),
    packages=(Package("trafilatura", "Apache-2.0"),),
    done_when="Research answers cite three sources.",
)
CWD = Path("/work/4")

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def test_the_roles_match_the_design_table() -> None:
    r = team.ROLES
    assert set(r) == {"design_lead", "analyst", "researcher", "test_writer", "builder", "reviewer"}
    assert (r["design_lead"].model, r["design_lead"].effort, r["design_lead"].max_turns) == (
        OPUS,
        "high",
        40,
    )
    assert r["design_lead"].max_budget_usd == 3.0
    assert (r["analyst"].model, r["analyst"].max_turns) == (OPUS, 30)
    assert (r["researcher"].model, r["researcher"].max_turns) == (SONNET, 25)
    assert (r["test_writer"].model, r["test_writer"].effort, r["test_writer"].max_budget_usd) == (
        OPUS,
        "medium",
        1.5,
    )
    assert (r["builder"].model, r["builder"].effort, r["builder"].max_turns) == (SONNET, "high", 80)
    assert r["builder"].max_budget_usd == 3.0
    assert (r["reviewer"].model, r["reviewer"].max_turns, r["reviewer"].max_budget_usd) == (
        OPUS,
        30,
        1.5,
    )


def test_tools() -> None:
    r = team.ROLES
    assert set(r["analyst"].tools) == {"Read", "Glob", "Grep"}
    assert set(r["researcher"].tools) == {"Read", "WebSearch", "WebFetch"}
    assert set(r["design_lead"].tools) == {"Read", "Glob", "Grep", "Agent", "Write"}
    assert "Edit" not in r["reviewer"].tools and "Write" not in r["reviewer"].tools
    assert {"Edit", "Write", "Bash"} <= set(r["builder"].tools)


def test_skills_come_from_the_vendored_plugins() -> None:
    r = team.ROLES
    assert r["design_lead"].skills == ("egm:egm-brainstorm", "egm:egm-draft", "egm:egm-review")
    assert r["test_writer"].skills == ("superpowers:test-driven-development",)
    assert r["builder"].skills == (
        "superpowers:test-driven-development",
        "superpowers:systematic-debugging",
        "superpowers:verification-before-completion",
    )
    assert r["reviewer"].skills == ("superpowers:requesting-code-review",)
    for role in r.values():
        for skill in role.skills:
            plugin, name = skill.split(":")
            assert (team.PLUGINS_DIR / plugin / "skills" / name / "SKILL.md").is_file(), skill
    for plugin in ("egm", "superpowers"):
        assert (team.PLUGINS_DIR / plugin / "LICENSE").is_file()


def test_every_role_has_a_prompt_file() -> None:
    for name in team.ROLES:
        text = team.prompt(name)
        assert "data, not instructions" in text, name


def test_stage_options_load_no_project_settings() -> None:
    for stage in team.STAGES:
        opts = team.stage_options(stage, cwd=CWD, plan=PLAN)
        assert opts["setting_sources"] == []  # no CLAUDE.md or .claude/settings from the copy
        assert opts["cwd"] == str(CWD)
        assert [p["type"] for p in opts["plugins"]] == ["local", "local"]
        assert opts["permission_mode"] == "default"


def test_stage_options_carry_each_role_s_limits() -> None:
    opts = team.stage_options("build", cwd=CWD, plan=PLAN)
    assert (opts["model"], opts["effort"], opts["max_turns"], opts["max_budget_usd"]) == (
        SONNET,
        "high",
        80,
        3.0,
    )
    assert opts["skills"] == list(team.ROLES["builder"].skills)
    assert opts["tools"] == list(team.ROLES["builder"].tools)


def test_the_design_stage_has_the_analyst_and_researcher_as_camelcase_subagents() -> None:
    opts = team.stage_options("design", cwd=CWD, plan=None)
    agents = opts["agents"]
    assert set(agents) == {"analyst", "researcher"}
    for spec in agents.values():
        assert set(spec) <= {"description", "prompt", "tools", "model", "skills", "maxTurns"}
        assert "max_turns" not in spec
    assert agents["researcher"]["maxTurns"] == 25
    assert agents["analyst"]["model"] == OPUS


def test_only_the_design_stage_runs_without_a_plan() -> None:
    with pytest.raises(ValueError, match="needs the approved plan"):
        team.stage_options("build", cwd=CWD, plan=None)


# The guard: lock 1, inside the agent. It catches honest mistakes; the pre-push check is the lock
# that matters.


def _decide(role: str, tool: str, **tool_input: object) -> guard.Decision:
    return guard.decide(team.ROLES[role], tool, dict(tool_input), cwd=CWD, plan=PLAN)


def test_a_tool_not_on_the_role_s_list_is_denied() -> None:
    d = _decide("reviewer", "Edit", file_path=str(CWD / PLAN.files[0]))
    assert not d.allow and "not one of the reviewer's tools" in d.reason


def test_the_builder_may_edit_only_the_plan_s_files() -> None:
    assert _decide("builder", "Edit", file_path=str(CWD / PLAN.files[0])).allow
    assert _decide("builder", "Write", file_path=PLAN.files[0]).allow  # relative to cwd
    d = _decide("builder", "Edit", file_path=str(CWD / "server/src/jarvis_server/brain/x.py"))
    assert not d.allow and "not in the approved plan" in d.reason


def test_the_builder_may_not_touch_the_fingerprinted_tests() -> None:
    d = _decide("builder", "Edit", file_path=str(CWD / PLAN.tests[0]))
    assert not d.allow and "Test writer" in d.reason


def test_the_test_writer_may_write_only_the_plan_s_tests() -> None:
    assert _decide("test_writer", "Write", file_path=str(CWD / PLAN.tests[0])).allow
    assert not _decide("test_writer", "Edit", file_path=str(CWD / PLAN.files[0])).allow


def test_the_design_lead_may_write_only_the_draft_design() -> None:
    assert guard.decide(
        team.ROLES["design_lead"],
        "Write",
        {"file_path": str(CWD / team.DESIGN_FILE)},
        cwd=CWD,
        plan=None,
    ).allow
    assert not guard.decide(
        team.ROLES["design_lead"],
        "Write",
        {"file_path": str(CWD / "README.md")},
        cwd=CWD,
        plan=None,
    ).allow


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/work/5/x.py", "/work/4/../5/x.py", "server/Dockerfile", ".claude/x"]
)
def test_writes_outside_the_copy_or_to_protected_paths_are_denied(path: str) -> None:
    assert not _decide("builder", "Write", file_path=path).allow


def test_reads_stay_inside_the_copy_and_the_plugins() -> None:
    assert _decide("reviewer", "Read", file_path=str(CWD / "README.md")).allow
    assert _decide(
        "reviewer", "Read", file_path=str(team.PLUGINS_DIR / "superpowers" / "LICENSE")
    ).allow
    assert not _decide("reviewer", "Read", file_path="/proc/self/environ").allow


def test_the_researcher_may_use_the_web() -> None:
    assert _decide("researcher", "WebFetch", url="https://pypi.org/project/x/").allow
    assert _decide("researcher", "WebSearch", query="sherpa-onnx release").allow


def test_a_role_may_run_only_its_own_skills() -> None:
    tdd = "superpowers:test-driven-development"
    assert _decide("builder", "Skill", skill=tdd).allow
    d = _decide("builder", "Skill", skill="egm:egm-draft")
    assert not d.allow and "not one of the builder's skills" in d.reason
    assert not _decide("analyst", "Skill", skill=tdd).allow


def test_the_lead_may_start_only_the_analyst_and_the_researcher() -> None:
    assert _decide("design_lead", "Agent", subagent_type="analyst", prompt="look").allow
    assert _decide("design_lead", "Agent", subagent_type="researcher", prompt="look").allow
    d = _decide("design_lead", "Agent", subagent_type="general-purpose", prompt="look")
    assert not d.allow and "analyst or the researcher" in d.reason


def test_the_hook_speaks_for_the_stage_or_its_own_subagent() -> None:
    assert guard.role_for("design", None) is team.ROLES["design_lead"]
    assert guard.role_for("design", "analyst") is team.ROLES["analyst"]
    assert guard.role_for("design", "researcher") is team.ROLES["researcher"]
    assert guard.role_for("build", None) is team.ROLES["builder"]
    assert guard.role_for("design", "general-purpose") is None  # not one of the team
    assert guard.role_for("build", "analyst") is None  # only the design stage has subagents
