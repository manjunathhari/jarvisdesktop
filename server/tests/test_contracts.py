"""Contracts: each example passes (or fails) both the JSON Schema and the model."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from jarvis_server import contracts as c

from .schema_registry import CONTRACTS, validator

NAMES = sorted(c.MODELS)


def _schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / f"{name}.schema.json").read_text())


def _example(name: str, kind: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / "examples" / f"{name}.{kind}.json").read_text())


def test_every_schema_has_a_model() -> None:
    on_disk = {p.name.removesuffix(".schema.json") for p in CONTRACTS.glob("*.schema.json")}
    assert on_disk == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_schema(name))


@pytest.mark.parametrize("name", NAMES)
def test_valid_example_passes_schema_and_model(name: str) -> None:
    example = _example(name, "valid")
    validator(_schema(name)).validate(example)
    c.MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", NAMES)
def test_invalid_example_fails_schema_and_model(name: str) -> None:
    example = _example(name, "invalid")
    assert not validator(_schema(name)).is_valid(example)
    with pytest.raises(ValidationError):
        c.MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", NAMES)
def test_model_fields_match_schema_properties(name: str) -> None:
    schema = _schema(name)
    model = c.MODELS[name]
    assert set(model.model_fields) == set(schema["properties"])
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert required == set(schema["required"])


ENUMS: list[tuple[str, str, type[StrEnum]]] = [
    ("task", "state", c.TaskState),
    ("task", "visibility", c.Visibility),
    ("commitment", "state", c.CommitmentState),
    ("commitment", "visibility", c.Visibility),
    ("audit-event", "actor_kind", c.ActorKind),
    ("audit-event", "entity_type", c.EntityType),
    ("audit-event", "action", c.AuditAction),
    ("audit-event", "outcome", c.AuditOutcome),
    ("approval", "tier", c.ApprovalTier),
    ("approval", "decision", c.ApprovalDecision),
    ("memory-entry", "category", c.MemoryCategory),
    ("memory-entry", "tag", c.MemoryTag),
    ("memory-entry", "visibility", c.Visibility),
]


@pytest.mark.parametrize(("name", "field", "enum"), ENUMS)
def test_enum_values_match_schema(name: str, field: str, enum: type[StrEnum]) -> None:
    assert _schema(name)["properties"][field]["enum"] == [e.value for e in enum]
