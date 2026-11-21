# Architect — news-wall

ADRs: 001 (shell), 002 (brain), 003 (module contract), 004 (audio).

## Workspace layout
```
jarvisdesktop/
├── Cargo.toml                 # workspace
├── crates/
│   ├── jarvis-core/           # types, ScreenState, Module + Brain + Voice traits, Coordinator state machine
│   ├── jarvis-brain/          # ClaudeBrain, LocalCommands, FakeBrain, eval runner
│   ├── jarvis-ears/           # capture, wake word, VAD, whisper
│   ├── jarvis-voice/          # TTS engine(s)
│   ├── jarvis-modules/        # news, live_tv
│   └── jarvis-platform/       # Windows: fullscreen, credential store; stubs elsewhere
├── app/
│   ├── src-tauri/             # Tauri binary: wires crates, IPC commands/events
│   └── ui/                    # Vite + TypeScript: canvas, orb, caption, panel kinds
├── config/sources.toml        # news feeds + live channel list
└── evals/brain/*.yaml         # phrasing → expected action
```

## Coordinator state machine
`Idle → Awake(greeting) → Listening → Thinking → Acting/Speaking → FollowUp(10 s) → Idle`
Interrupts: "stop"/new speech during Speaking → stop voice → Listening. `MicOff` state entered by "stop listening", left only by tap.

## Data flow
1. Ears emits `Wake` / `Utterance(text)` events.
2. Coordinator → Brain with `(text, ScreenState summary, session turns)`.
3. BrainEvent stream: `SpeechChunk` → Voice; `Action` → validated against registry → Module.handle.
4. Module returns `PanelUpdate`s → Coordinator updates `ScreenState` (assigns numbers) → emits `screen://state` to UI.
5. UI emits `ui://tap` → Coordinator treats as Wake.

## Failure modes
Per spec §4 table. Coordinator owns timeouts (brain 8 s, listen silence 5 s, follow-up 10 s). Module errors never crash the loop; they produce a spoken fallback.

## Boundaries
- Frontend: no network calls except embedded YouTube player; CSP restricts everything else.
- Only `jarvis-brain` talks to Anthropic; only `jarvis-modules` fetch feeds.
