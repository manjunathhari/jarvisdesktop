"""Lock 1, inside the agent: which tool calls a role may make.

The sandbox runner asks this before every tool call. It catches honest mistakes. It can't stop a
determined Bash command, and isn't meant to: the pre-push check in the orchestrator (gate.py) is
the lock that matters, and GitHub's rules are the last one.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis_server.improve import gate, team
from jarvis_server.improve.plan import Plan

_WRITES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_READS = frozenset({"Read", "Glob", "Grep"})


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


def role_for(stage: str, agent_type: str | None) -> team.Role | None:
    """The role a tool call speaks for: the stage's own, or one of its subagents. None for an
    agent that is not one of the team (a built-in one the lead tried to start, say)."""
    if agent_type is None:
        return team.ROLES[team.STAGES[stage]]
    if stage == "design" and agent_type in team.SUBAGENTS:
        return team.ROLES[agent_type]
    return None


def decide(
    role: team.Role, tool: str, tool_input: Mapping[str, Any], *, cwd: Path, plan: Plan | None
) -> Decision:
    if tool == "Skill":
        skill = tool_input.get("skill")
        ok = isinstance(skill, str) and skill in role.skills
        return Decision(ok, "" if ok else f"{skill} is not one of the {role.name}'s skills")
    if tool not in role.tools:
        return Decision(False, f"{tool} is not one of the {role.name}'s tools")
    if tool == "Agent":
        ok = tool_input.get("subagent_type") in team.SUBAGENTS
        return Decision(ok, "" if ok else "the lead may start only the analyst or the researcher")
    if tool in _WRITES:
        return _write(role, _path(tool_input), cwd, plan)
    if tool in _READS:
        return _read(_path(tool_input), cwd)
    return Decision(True)


def _path(tool_input: Mapping[str, Any]) -> str | None:
    value = tool_input.get("file_path", tool_input.get("notebook_path", tool_input.get("path")))
    return value if isinstance(value, str) else None


def _relative(path: str, root: Path) -> str | None:
    """`path` as a plain path inside `root`, or None if it leaves it."""
    full = posixpath.normpath(posixpath.join(str(root), path))
    prefix = str(root).rstrip("/") + "/"
    return full[len(prefix) :] if full.startswith(prefix) else None


def _read(path: str | None, cwd: Path) -> Decision:
    if path is None:
        return Decision(True)  # Glob and Grep default to the working folder
    if _relative(path, cwd) is not None or _relative(path, team.PLUGINS_DIR) is not None:
        return Decision(True)
    return Decision(False, f"{path} is outside the copy")


def _write(role: team.Role, path: str | None, cwd: Path, plan: Plan | None) -> Decision:
    rel = None if path is None else _relative(path, cwd)
    if rel is None:
        return Decision(False, f"{path} is outside the copy")
    if gate.is_protected(rel):
        return Decision(False, f"{rel} is protected")
    if role.name == "design_lead":
        ok = rel == team.DESIGN_FILE
        return Decision(ok, "" if ok else f"the lead writes only {team.DESIGN_FILE}")
    if plan is None:
        return Decision(False, "no approved plan")
    if role.name == "test_writer":
        ok = rel in plan.tests
        return Decision(ok, "" if ok else f"{rel} is not one of the plan's tests")
    if rel in plan.tests:
        return Decision(False, f"{rel} belongs to the Test writer and is fingerprinted")
    if rel in plan.files:
        return Decision(True)
    return Decision(False, f"{rel} is not in the approved plan")
