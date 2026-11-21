# ADR-002: Swappable Brain with a registered action menu (tool use)

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** news-wall
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
Start with Claude over the API; later move fully or partly (hybrid) to local open-source models. The brain must turn loose speech into precise actions, and must not be able to do anything outside what modules allow.

## Decision
- Define a Rust `Brain` trait: input = utterance + current `ScreenState` summary + conversation turns of the current session; output = stream of `BrainEvent` (`SpeechChunk(text)`, `Action{name, args}`, `Done`).
- First implementation `ClaudeBrain`: Anthropic Messages API with **tool use** and **streaming**. Tools are generated from the action registry (ADR-003). Model id configurable; default to a fast tier for latency. 8 s timeout.
- `LocalCommands` pre-filter handles a tiny fixed set locally ("stop", "clear the screen", "stop listening") so they work offline and instantly. This is the seed of the hybrid brain.
- Any action name/args not in the registry or failing schema validation are rejected and logged.
- A `FakeBrain` (scripted) is used in tests.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Fixed keyword/regex command parser | Offline, free, instant | Breaks on rewording; doesn't scale to many modules | Fails "any reasonable rewording" criterion |
| Let the model output free-form JSON | Simple | Brittle parsing, no schema enforcement | Tool use gives validated structure |
| Local model first | Private, free | Weaker intent accuracy today | User chose cloud first, local later |

## Consequences
### Positive
- Adding a module automatically extends what JARVIS understands.
- The brain evaluation set (≈30 phrasings/action) compares Claude vs any local model objectively.
### Negative
- Needs internet + per-use cost for non-local commands.
### Risks
- Prompt injection from news text → only display-only actions exist in slice 1; future state-changing actions require spoken confirmation enforced by the coordinator, not the brain.
- API key leakage → key read from Windows Credential Manager at runtime only.

## Dependencies
Depends on ADR-003 (action registry). Constrains every future module (must declare actions with JSON schemas).
