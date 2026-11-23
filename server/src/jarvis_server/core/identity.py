"""Who a turn is for (P2-S2, spec §12): the authenticated person, never a name from a payload.

The person comes from `conv.inbound.person_id`, which the gateway filled from the fenced node
row. Their memory slug is looked up in the memory engine's people, and it is the `viewer` of
every memory read of the turn (and `by` of any memory write, Task 11).

Actors: a turn's writes for what the person did are `Actor(PERSON, slug)`; those for what
Jarvis did are `CORE`.
"""

from __future__ import annotations

from uuid import UUID

from jarvis_server.contracts import ActorKind
from jarvis_server.ledger import Actor
from jarvis_server.memory.engine import MemoryEngine

CORE = Actor(ActorKind.SYSTEM, "core")


class UnknownPerson(LookupError):
    """The ledger person is not a memory subject."""


def memory_slug(engine: MemoryEngine, person_id: UUID) -> str:
    """The memory slug of ledger person `person_id`, from `engine.people()`."""
    for p in engine.people():
        if p.person_id == person_id:
            return p.slug
    raise UnknownPerson(f"person {person_id} is not in memory")


def display_name(engine: MemoryEngine, person_id: UUID) -> str:
    for p in engine.people():
        if p.person_id == person_id:
            return p.display_name
    raise UnknownPerson(f"person {person_id} is not in memory")


def person_actor(slug: str) -> Actor:
    return Actor(ActorKind.PERSON, slug)
