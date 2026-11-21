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

## Update 2026-11-21 (after first run on the user's PC)
User feedback: no way to exit; having to use the terminal every time is annoying.
- Exit: local command "shut down / exit / quit / goodbye" (spoken goodbye, then close) + Esc key closes immediately.
- No terminal: `release.yml` builds an NSIS installer (3.6 MB) on every push and publishes a GitHub pre-release.
- T13 pulled forward: first-run setup screen asks for the Anthropic key once; stored via `keyring` in Windows Credential Manager; app restarts to load it. Env var still honoured for development.
- Typing box enabled in installed builds until the microphone (Milestone B).
- Installer is unsigned → SmartScreen warning on first install (code signing deferred).

## T11/T12 — George's voice (2026-11-21)
- User chose Kokoro `bm_george` (British male) after listening to samples.
- Engine: sherpa-onnx **1.13.2** (pinned with sherpa-onnx-sys 1.13.2). v2.x drops espeak-ng (lexicon-only), which risks mispronouncing names in headlines; v1.13 keeps espeak-ng → any word works. Licence note: espeak-ng is GPL-3; fine for personal use/public source; revisit (v2 + lexicon) if JARVIS is ever distributed closed-source.
- Model: official `kokoro-en-v0_19` pack (~330 MB model, speaker id 9), downloaded once into %LOCALAPPDATA%\in.mhsurgery.jarvis\models with progress + integrity check (required files, .complete marker), staged unpack.
- Voice worker: ordered sentence queue, epoch-based instant interruption, synth errors skip a sentence; rodio playback polls for cancellation.
- Stand-in web voice speaks until George is ready; UI shows "Getting my voice ready, N%".
- Engine compiled on Windows only (its build downloads prebuilt libs, blocked in the cloud workspace). Verified by the Windows CI build; not yet heard on the device.
- Installer build 5: 7.9 MB.
