# Engineering Manager — news-wall plan

## Milestone A — "Brain + screen, typed input" (no audio yet)
Lets the user type "bring me the news" and see the wall. Proves modules, brain and screen.
| # | Task | Depends | Est |
|---|---|---|---|
| T1 | Workspace scaffold, CI (ubuntu+windows), fmt/clippy, .gitignore, README | — | ½d |
| T2 | `jarvis-core` types: ActionSpec, PanelUpdate, ScreenState (numbering), Module/Brain/Voice traits | T1 | ½d |
| T3 | Coordinator state machine + timeouts, with FakeBrain/FakeVoice tests | T2 | ½d |
| T4 | `news` module: sources.toml, fetch + parse (feed-rs) + sanitise + cache, fixture tests | T2 | ½d |
| T5 | `live_tv` module: channel list, embed URL, fallback to next channel | T2 | ¼d |
| T6 | `ClaudeBrain`: tool generation from registry, streaming SSE, validation, timeout; LocalCommands | T2 | ½d |
| T7 | Brain eval set (≈30 phrasings × 7 actions) + runner; ≥90% gate | T6 | ½d |
| T8 | Tauri app + UI: black canvas, orb, caption, CardList/CardDetail/Video panels, dev text-input box | T3–T5 | ½d |

## Milestone B — "Voice in, voice out"
| # | Task | Depends | Est |
|---|---|---|---|
| T9 | Ears capture (cpal) + Silero VAD + whisper-rs transcription; clip tests | T2 | ½d |
| T10 | openWakeWord ONNX pipeline + tap-to-wake; false-wake tests incl. TV noise | T9 | ½d |
| T11 | TTS spike Kokoro vs Piper (quality, speed, licence) → `Voice` impl with interrupt | T2 | ½d |
| T12 | Model download manager with checksums | T9–T11 | ¼d |

## Milestone C — "JARVIS on Windows"
| # | Task | Depends | Est |
|---|---|---|---|
| T13 | `jarvis-platform` Windows: borderless fullscreen over taskbar, Credential Manager key (`jarvis setup-key`) | T8 | ½d |
| T14 | Wire everything; latency telemetry; end-to-end run on user's PC vs 3 s target | all | ½d |
| T15 | QA + security review, Windows installer build in CI | T14 | ½d |

## Risks / unknowns
- ONNX runtime + whisper.cpp packaging on Windows — surface early by building them in Windows CI during T9/T10.
- Target PC GPU unknown → affects Whisper model choice (ask user).
- Feed URLs and YouTube live embed behaviour to be verified in T4/T5.
