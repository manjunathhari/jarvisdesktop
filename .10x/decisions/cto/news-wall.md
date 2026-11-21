# CTO — news-wall (ACCEPTED 2026-11-21)

## Problem
User wants a voice-only "JARVIS" desktop replacing icon/browser interaction. Slice 1 proves the full voice loop with one capability (news) so later capabilities plug in cheaply.

## Build vs buy
- No existing product gives a blank, voice-only, extensible desktop on Windows with a swappable brain → **build**.
- Buy/reuse the hard parts as open-source components: openWakeWord (Apache-2.0), whisper.cpp (MIT), local TTS engine (decided by spike, permissive licence required), Tauri 2 (MIT/Apache-2.0).
- Rent the brain: Claude API (user has key). Swap path to local models kept open via `Brain` trait.

## Tech direction
- Rust workspace, Tauri 2 shell on WebView2. See ADR-001..004.
- Default Claude model chosen for latency (fast tier), configurable.

## Risks
| Risk | Mitigation |
|---|---|
| End-to-end latency > 3 s | Pre-cached feeds, streamed brain reply, Whisper model sized to PC, measure from day one |
| Wake-word false triggers from TV audio | Tunable threshold; clip-library tests incl. TV noise; tap fallback |
| Windows-only features can't be tested in cloud workspace | Core crates platform-neutral, tested on Linux + Windows CI runner; final checks on user's PC |
| Feed/stream URLs change | All sources in one config file; per-source failure isolated |
| Prompt injection via news text | Brain limited to registered display-only actions in slice 1 |

## Opportunity cost
Small: slice 1 is the foundation for every later module.
