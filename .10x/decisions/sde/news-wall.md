# SDE — news-wall: what was built (Milestone A, 2026-11-21)

Branch `milestone-a`. 81 tests (core 33, modules 30, brain 18), clippy -D warnings clean, tsc clean.

## Built
- **jarvis-core:** `ScreenState` (slot panels, reading-order numbering, brain summary, UI view); `Registry` refusing unknown actions and validating args against each action's JSON schema; pure `Coordinator::step` state machine (wake/tap greeting, listen timeout, follow-up window, stale-timer guard, empty-speech retry, local commands, one-level follow-up actions, history of 6 turns); `runtime()` executing effects (brain with 8 s timeout, dispatch, timers, UI events); `Brain` trait.
- **jarvis-modules:** news (concurrent fetch, last-good cache, round-robin across sources, dedupe by link, topic filter, lazy first fetch, starts live TV if none playing), live TV (loose channel matching, next/wrap), screen actions (close panel, clear). `config/sources.toml` embedded.
- **jarvis-brain:** `ClaudeBrain` (Messages API, tool use, streaming SSE → sentence-sized speech + assembled tool calls), history merging to keep roles alternating, `jarvis-eval` live runner with 72 cases and a 90% gate.
- **app:** Tauri 2 window (fullscreen, borderless, black, strict CSP, minimal capabilities), commands `tap` / `say` / `typed_input_enabled`, 5-minute background news refresh; UI ≈9 KB: orb states, captions, numbered wall, video panel (kept alive across updates), story overlay, dev typing box.

## Deviations from plan (and why)
- Frontend lives in `app/` (not `app/ui/`) — standard Tauri layout so the CLI finds `src-tauri`.
- Runtime lives in `jarvis-core` (not the app) — testable without a window; app is thin glue.
- Local commands live in the coordinator (core), not `jarvis-brain` — they must work with no brain at all.
- No `ammonia`: feed text is reduced to plain text by `text::plain_text`, and the UI inserts only `textContent`, so no HTML reaches the page.
- Blanket `impl Module for Arc<M>` added so the news module can be registered and refreshed in the background.
- Voice in Milestone A = web view `speechSynthesis` (Windows voices). Replaced by local TTS in T11.
- API key read from `ANTHROPIC_API_KEY` env var until T13 moves it to Credential Manager.

## Tech debt / not yet verified
- Feed URLs and YouTube channel IDs not live-checked (build machine can't reach them; web lookups awaited user approval).
- Claude API stream format implemented from the standard Messages API; first real `jarvis-eval` run confirms it.
- No tool_result round trip: single-shot tool use per utterance (fine for slice 1).
- Offline live stream is not auto-detected yet (needs a player event); "next channel" by voice works.
- Eval set is ~10 phrasings/action vs the planned ~30.
- TDD note: the screen model's first tests were written alongside the code; they were proven by breaking the code and watching them fail. Everything after was strict test-first.
