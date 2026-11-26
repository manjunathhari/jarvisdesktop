"""The price table (spec §10.1 step 1, R12-12): `config/prices.toml`, paise per million tokens.

Integer arithmetic only, rounded up to the next paisa, so a cost is never under-stated. A model
missing from the table raises `UnknownModelPrice`: the caller must not call a model whose cost
it cannot meter (fail closed).

The reservation ceiling of §10.1 step 1 is `cost_paise(model, count_input_tokens(req),
req.max_tokens)`: the same formula with the worst-case token counts, rounded up once. Because
the cost only grows with either count, a settled cost never exceeds the reservation.
"""

from __future__ import annotations

import functools
import tomllib
from dataclasses import dataclass
from pathlib import Path

# server/config/prices.toml (in the image: /app/config/prices.toml).
PRICES_FILE = Path(__file__).resolve().parents[3] / "config" / "prices.toml"

TOKENS_PER_PRICE_UNIT = 1_000_000


class UnknownModelPrice(LookupError):
    """The model has no entry in the price table."""


class UnknownToolPrice(LookupError):
    """A tool billed per use has no entry in the price table."""


@dataclass(frozen=True)
class Price:
    input: int  # paise per million input tokens
    output: int  # paise per million output tokens


@dataclass(frozen=True)
class PriceTable:
    default_model: str
    models: dict[str, Price]
    tools: dict[str, int]  # paise per use of a tool Anthropic runs (web search)


def _count(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer")
    return value


def load(path: Path = PRICES_FILE) -> PriceTable:
    """Parse and check a price table file."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    models_raw = raw.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise ValueError(f"{path}: [models] is missing or empty")
    models: dict[str, Price] = {}
    for name, entry in models_raw.items():
        if not isinstance(entry, dict) or set(entry) != {"input", "output"}:
            raise ValueError(f"{path}: model {name!r} needs exactly input and output")
        models[name] = Price(
            input=_count(entry["input"], f"{name} input price"),
            output=_count(entry["output"], f"{name} output price"),
        )
    default = raw.get("default_model")
    if not isinstance(default, str) or default not in models:
        raise ValueError(f"{path}: default_model must name a model in the table")
    tools: dict[str, int] = {}
    tools_raw = raw.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ValueError(f"{path}: [tools] must be a table")
    for name, entry in tools_raw.items():
        if not isinstance(entry, dict) or set(entry) != {"per_use"}:
            raise ValueError(f"{path}: tool {name!r} needs exactly per_use")
        tools[name] = _count(entry["per_use"], f"{name} price per use")
    return PriceTable(default_model=default, models=models, tools=tools)


@functools.cache
def table() -> PriceTable:
    """The deployed price table, read once per process."""
    return load()


def default_model() -> str:
    return table().default_model


def cost_paise(model: str, input_tokens: int, output_tokens: int) -> int:
    """The cost of `input_tokens` + `output_tokens` on `model`, rounded up to the paisa."""
    price = table().models.get(model)
    if price is None:
        raise UnknownModelPrice(f"no price for model {model!r}")
    numerator = (
        _count(input_tokens, "input_tokens") * price.input
        + _count(output_tokens, "output_tokens") * price.output
    )
    return -(-numerator // TOKENS_PER_PRICE_UNIT)


def tool_paise(tool: str, uses: int = 1) -> int:
    """The cost of `uses` uses of a tool Anthropic runs and bills per use. A tool without a
    price raises `UnknownToolPrice`: it must not be offered to the model (fail closed)."""
    price = table().tools.get(tool)
    if price is None:
        raise UnknownToolPrice(f"no price for tool {tool!r}")
    return _count(uses, "uses") * price
