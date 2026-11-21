# Senior Engineer — news-wall implementation approach

- **T1:** Cargo workspace with crates per architect layout; Tauri app created with `create-tauri-app` vanilla-ts template, then trimmed. CI matrix ubuntu-latest + windows-latest: fmt, clippy, test.
- **T2:** Keep traits object-safe (`async_trait`) so modules are `Box<dyn Module>`. `ScreenState` assigns global item numbers 1..n left-to-right/top-to-bottom; renumber on every update; brain gets a compact text summary ("4: The Hindu — headline…").
- **T3:** Coordinator is a pure-ish state machine (`fn step(state, event) -> (state, Vec<Effect>)`) driven by a tokio loop. Pure step function makes every transition unit-testable without async.
- **T4:** Fetch all feeds concurrently with per-feed 5 s timeout; keep last good result per source; refresh every 5 min; sanitise summaries with ammonia; dedupe by link. Fixtures: saved XML for each source + one malformed.
- **T5:** Channel = {name, youtube_channel_id}; embed `https://www.youtube-nocookie.com/embed/live_stream?channel=ID` (verify behaviour; fallback: resolve current live video id). Offline detection via UI player event → coordinator → `next_channel`.
- **T6:** Hand-rolled reqwest client against Messages API (`tools`, `stream: true`); parse SSE `content_block_*` events; emit SpeechChunk on text deltas, Action on completed tool_use blocks. System prompt: persona (concise, British-butler JARVIS tone, addresses user as "Manj"), screen summary, rule "only use tools; never invent numbers not on screen".
- **T7:** YAML eval cases; runner calls real API behind `--live` flag only; reports accuracy per action.
- **T8:** UI subscribes to `screen://state`; renders panels via a `switch(kind)`; dev-only text box (compiled out in release) to type utterances.
- **T9–T10:** 16 kHz mono ring buffer; openWakeWord needs melspectrogram → embedding → wakeword ONNX models in sequence (port the reference pipeline; verify against its Python output on test clips).
- **T11:** Spike criteria: naturalness (user listens to samples), real-time factor < 0.3 on CPU, licence permissive.
- **T13:** Tauri window config: decorations false, fullscreen true, always-on-top optional; `keyring` crate for credentials.
- **Tricky parts:** openWakeWord port correctness (T10); SSE streaming tool-call assembly (T6); Windows packaging of native libs (T12/T15).
