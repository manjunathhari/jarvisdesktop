"""Load the JSON Schemas in /contracts, resolving cross-file `$ref`s from disk (never the web)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


@cache
def registry() -> Registry[Any]:
    resources = []
    for path in sorted(CONTRACTS.glob("**/*.schema.json")):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schema, registry=registry(), format_checker=jsonschema.FormatChecker()
    )
