# ADR-003: Module contract (actions + fetcher + panel)

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** news-wall
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
User will add news, video, calendar, Gmail, social and more. Each addition must be cheap and must not make the app heavy or button-laden.

## Decision
Every capability is a Rust type implementing a `Module` trait:
- `actions() -> Vec<ActionSpec>` — name, description, JSON schema for args (feeds the brain's tools).
- `handle(action, ctx) -> Result<ModuleOutput>` — performs the action, returns panel updates + optional speech.
- optional `background(ctx)` — periodic refresh (e.g. feed cache).
Modules render through a small fixed vocabulary of **panel kinds** (`CardList`, `CardDetail`, `Video`), drawn by the frontend from data. Modules never ship their own HTML. Every visible item gets a stable on-screen number held in `ScreenState`.

Slice 1 modules: `news`, `live_tv`, plus built-in `screen` actions (`close_panel`, `clear_screen`).

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Embed each service's real website in a webview | Full features for free | Heavy, button-laden, many sites refuse embedding, needs Tauri `unstable` multi-webview | Conflicts with "light, voice-only" |
| Plugins as separate processes (e.g. MCP servers) | Isolation, language-agnostic | More moving parts on day one | YAGNI now; contract can be bridged to MCP later |
| Modules supply custom HTML | Flexible look | Frontend grows per module; security surface | Fixed panel kinds keep it light and safe |

## Consequences
### Positive
- New module = one Rust file/crate + config; no frontend change unless a new panel kind is truly needed.
- Uniform numbering makes every screen voice-addressable.
### Negative
- Panel vocabulary may need to grow (e.g. `Calendar` view) — add kinds deliberately.
### Risks
- Trait churn as modules grow → keep trait minimal; version it if needed.

## Dependencies
Used by ADR-002. Constrains frontend (renders only panel kinds).
