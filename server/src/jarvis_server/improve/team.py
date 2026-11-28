"""The team's roles and the Agent SDK options for each stage.

This module never imports the SDK: it builds plain keyword arguments, and the sandbox runner (the
only place the SDK is installed) turns them into ClaudeAgentOptions and AgentDefinition. Subagent
fields are the SDK's camelCase names.

Skills come from two vendored plugins under plugins/ (superpowers, MIT; EGM, Apache-2.0), loaded
as local plugins. setting_sources is empty, so the agents never load a CLAUDE.md or .claude
settings from the copy they work in: a file the Builder wrote can't steer the Reviewer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jarvis_server.improve.plan import Plan

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"

HERE = Path(__file__).parent
PLUGINS_DIR = HERE / "plugins"
ROLES_DIR = HERE / "roles"
DESIGN_FILE = "DESIGN.md"

_READ = ("Read", "Glob", "Grep")
_TDD = "superpowers:test-driven-development"


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    max_turns: int
    max_budget_usd: float | None = None
    effort: str | None = None


ROLES: Mapping[str, Role] = MappingProxyType(
    {
        r.name: r
        for r in (
            Role(
                "design_lead",
                "JARVIS as lead: writes the design",
                OPUS,
                (*_READ, "Agent", "Write"),
                ("egm:egm-brainstorm", "egm:egm-draft", "egm:egm-review"),
                40,
                3.0,
                "high",
            ),
            Role("analyst", "Looks inward: finds the cause in JARVIS's code", OPUS, _READ, (), 30),
            Role(
                "researcher",
                "Looks outward: libraries, releases and techniques; only suggests",
                SONNET,
                ("Read", "WebSearch", "WebFetch"),
                (),
                25,
            ),
            Role(
                "test_writer",
                "Writes tests that fail today",
                OPUS,
                (*_READ, "Edit", "Write", "Bash"),
                (_TDD,),
                40,
                1.5,
                "medium",
            ),
            Role(
                "builder",
                "Makes the tests pass, touching only the plan's files",
                SONNET,
                (*_READ, "Edit", "Write", "Bash"),
                (
                    _TDD,
                    "superpowers:systematic-debugging",
                    "superpowers:verification-before-completion",
                ),
                80,
                3.0,
                "high",
            ),
            Role(
                "reviewer",
                "Checks the patch on a clean copy; changes nothing",
                OPUS,
                (*_READ, "Bash"),
                ("superpowers:requesting-code-review",),
                30,
                1.5,
            ),
        )
    }
)

STAGES: Mapping[str, str] = MappingProxyType(
    {"design": "design_lead", "tests": "test_writer", "build": "builder", "review": "reviewer"}
)
SUBAGENTS = ("analyst", "researcher")


def prompt(role: str) -> str:
    return (ROLES_DIR / f"{role}.md").read_text()


def stage_options(stage: str, *, cwd: Path, plan: Plan | None) -> dict[str, Any]:
    """Keyword arguments for ClaudeAgentOptions for one stage."""
    role = ROLES[STAGES[stage]]
    if plan is None and stage != "design":
        raise ValueError(f"the {stage} stage needs the approved plan")
    opts: dict[str, Any] = {
        "cwd": str(cwd),
        "model": role.model,
        "system_prompt": prompt(role.name),
        "tools": list(role.tools),
        "skills": list(role.skills),
        "max_turns": role.max_turns,
        "max_budget_usd": role.max_budget_usd,
        "effort": role.effort,
        "setting_sources": [],
        "plugins": [
            {"type": "local", "path": str(PLUGINS_DIR / "superpowers")},
            {"type": "local", "path": str(PLUGINS_DIR / "egm")},
        ],
        "permission_mode": "default",
    }
    if stage == "design":
        opts["agents"] = {name: _agent(ROLES[name]) for name in SUBAGENTS}
    return opts


def _agent(role: Role) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "description": role.description,
        "prompt": prompt(role.name),
        "tools": list(role.tools),
        "model": role.model,
        "maxTurns": role.max_turns,
    }
    if role.skills:
        spec["skills"] = list(role.skills)
    return spec
