# ADR-004: Local-first audio pipeline (wake word, VAD, STT, TTS on the PC)

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** news-wall
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
Raw audio must never leave the PC; the system must be always-on yet light; later the whole stack should run offline.

## Decision
- Capture: `cpal` (cross-platform audio) at 16 kHz mono, ring buffer.
- Wake word: **openWakeWord** pretrained "hey jarvis" ONNX models via the `ort` crate. Threshold configurable. Tap-to-wake event from frontend is equivalent.
- End-of-speech: **Silero VAD** (ONNX, via `ort`) to detect when the user stops talking.
- STT: **whisper.cpp** via `whisper-rs`; model size configurable (start small/base English-capable multilingual; larger if GPU present).
- TTS: local natural voice behind a `Voice` trait. Engine picked by a short spike between **Kokoro** (Apache-2.0) and **Piper** (licence of the chosen version checked). Must support stop-on-interrupt.
- All of this lives in `jarvis-ears` and `jarvis-voice` crates, platform-neutral.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Cloud STT/TTS | Best quality, less CPU | Audio leaves PC; cost; online-only | Violates privacy rule and local direction |
| Web Speech API inside webview | Zero setup | Unreliable in WebView2, goes to Microsoft cloud | Same as above, plus flaky |
| Realtime speech-to-speech cloud API | Snappiest conversation | Continuous audio upload | Opposite of local-first goal |
| Windows SAPI voices for TTS | Built in, instant | Robotic | User chose natural local voice |

## Consequences
### Positive
- Privacy by construction; same pipeline works offline once the brain is local.
### Negative
- Model files (tens–hundreds of MB) must be downloaded on first run or bundled.
- CPU cost of Whisper on machines without GPU → model size tuned to PC.
### Risks
- Wake-word false positives from TV audio → threshold tuning + clip tests; consider muting wake detection while JARVIS's own video is playing loudly (echo) — evaluate in testing.
- ONNX runtime packaging on Windows → verify in Windows CI early (task T2).

## Dependencies
Constrains Ears/Voice crate APIs; independent of the brain choice.
