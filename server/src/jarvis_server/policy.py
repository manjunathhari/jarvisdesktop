"""The policy engine (spec §9.1, §9.2): a deterministic tier table and five checks.

`decide_tool_call` decides one tool call of a conversation turn. The call carries the
**internal action kind** (`prompt.TOOL_ACTIONS` has already mapped the brain's wire name;
`core.turn` refuses an unmapped wire name as `unknown_action` before it gets here). The checks
run in the §9.2 order, and the first that fails decides:

1. Identity: the person comes from authentication. The node that spoke must be a registered,
   non-revoked node of that person (`identity_mismatch`).
2. Scope: the action kind is in the table (`unknown_action`) and the node kind can run it
   (`node_kind_not_allowed`).
3. Purpose: the session is the person's open session (`wrong_session`).
4. Approval: an IRREVERSIBLE action is never allowed from a turn: it needs an approval
   (`NeedsApproval`, returned once check 5 has passed as well); the approval flow
   (`core.approvals`) checks and consumes the approval before anything is issued.
5. Memory: the memory entries the decision used must be actionable, STATED or CONFIRMED
   (`memory_not_actionable`, Phase 1 M3).

The caller records and explains a refusal (`core.turn`). Nothing in a call's arguments ever
sets the person, the viewer or the subject.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

import psycopg

from jarvis_server.brain.types import ToolCall
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.memory.errors import NotActionable


class Tier(StrEnum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE = "REVERSIBLE"
    IRREVERSIBLE = "IRREVERSIBLE"


# §9.1, exactly, and the Board's five kinds (R12-39). `screen.*` is every `screen.<name>` kind.
# Anything else is refused.
TIERS: Mapping[str, Tier] = MappingProxyType(
    {
        "module.news": Tier.READ_ONLY,
        "module.live_tv": Tier.READ_ONLY,
        "screen.*": Tier.READ_ONLY,
        "memory.write": Tier.REVERSIBLE,
        "pc.list_test_folder": Tier.READ_ONLY,
        "pc.delete_test_file": Tier.IRREVERSIBLE,
        # The Board (slice 1): each kind is named; there is no `board.*` row. They only change
        # what is displayed, and a closed panel is kept, so all are READ_ONLY.
        "board.make": Tier.READ_ONLY,
        "board.show": Tier.READ_ONLY,
        "board.update": Tier.READ_ONLY,
        "board.set_state": Tier.READ_ONLY,
        "board.focus": Tier.READ_ONLY,
    }
)

# Which node kinds can run a kind (§9.2 step 2). The desktop runs the modules, the screen and
# the test folder; `memory.write` runs on the server for whichever node the person spoke on.
_NODE_KINDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "module.news": frozenset({"DESKTOP"}),
        "module.live_tv": frozenset({"DESKTOP"}),
        "screen.*": frozenset({"DESKTOP"}),
        "memory.write": frozenset({"DESKTOP", "PHONE"}),
        "pc.list_test_folder": frozenset({"DESKTOP"}),
        "pc.delete_test_file": frozenset({"DESKTOP"}),
        "board.make": frozenset({"DESKTOP"}),
        "board.show": frozenset({"DESKTOP"}),
        "board.update": frozenset({"DESKTOP"}),
        "board.set_state": frozenset({"DESKTOP"}),
        "board.focus": frozenset({"DESKTOP"}),
    }
)

REFUSAL_CODES = (
    "identity_mismatch",
    "unknown_action",
    "node_kind_not_allowed",
    "wrong_session",
    "memory_not_actionable",
)


@dataclass(frozen=True)
class Allow:
    tier: Tier


@dataclass(frozen=True)
class NeedsApproval:
    tier: Tier = Tier.IRREVERSIBLE


@dataclass(frozen=True)
class Refuse:
    code: str


Decision = Allow | NeedsApproval | Refuse

_memory: MemoryEngine | None = None


def set_memory(engine: MemoryEngine | None) -> None:
    """The memory engine for check 5 (set with the turn's engine)."""
    global _memory
    _memory = engine


def table_key(action_kind: str) -> str | None:
    """The §9.1 row of an action kind, or None (default deny)."""
    if action_kind in TIERS and action_kind != "screen.*":
        return action_kind
    head, dot, rest = action_kind.partition(".")
    if head == "screen" and dot and rest and rest.replace("_", "a").isalnum() and rest.islower():
        return "screen.*"
    return None


def tier_of(action_kind: str) -> Tier | None:
    key = table_key(action_kind)
    return None if key is None else TIERS[key]


def decide_tool_call(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    session_id: UUID,
    node_id: UUID,
    node_kind: str,
    call: ToolCall,
    memory_ids: Sequence[str],
) -> Decision:
    """The five checks of §9.2, in order; the first failure is the refusal."""
    # 1. Identity: the authenticated node's person, never anything from the call.
    row = conn.execute(
        "SELECT person_id, kind, revoked_at FROM gw.node WHERE id = %s", (node_id,)
    ).fetchone()
    if row is None or row[0] != person_id or row[2] is not None or row[1] != node_kind:
        return Refuse("identity_mismatch")
    # 2. Scope.
    key = table_key(call.name)
    if key is None:
        return Refuse("unknown_action")
    if node_kind not in _NODE_KINDS[key]:
        return Refuse("node_kind_not_allowed")
    # 3. Purpose: the person's own, open session.
    open_session = conn.execute(
        "SELECT 1 FROM conv.session WHERE id = %s AND person_id = %s AND closed_at IS NULL",
        (session_id, person_id),
    ).fetchone()
    if open_session is None:
        return Refuse("wrong_session")
    # 4. Approval: an IRREVERSIBLE action needs one; nothing in a turn can supply it, so the
    # answer is NeedsApproval, once check 5 has passed too.
    tier = TIERS[key]
    # 5. Memory.
    if memory_ids:
        if _memory is None:
            return Refuse("memory_not_actionable")  # fail closed: nothing to check against
        try:
            _memory.assert_actionable(list(memory_ids))
        except NotActionable:
            return Refuse("memory_not_actionable")
    if tier is Tier.IRREVERSIBLE:
        return NeedsApproval()
    return Allow(tier)
