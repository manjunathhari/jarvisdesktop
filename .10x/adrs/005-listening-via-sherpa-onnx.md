# ADR-005: Listening via sherpa-onnx — tap-to-talk with Silero VAD + Whisper small.en

**Status:** Accepted (partly supersedes ADR-004's choice of openWakeWord/whisper-rs/ort)
**Date:** 2026-11-21
**Feature:** news-wall (Milestone B: T9/T10)
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
George's voice already runs on sherpa-onnx 1.13.2 (ADR-004 follow-up). The user wants to talk instead of type.
His original brief: "touch the desktop and say Hey Jarvis". Raw audio must stay on the PC.
Linking a second ONNX runtime (via `ort`) next to sherpa-onnx's statically linked one risks symbol clashes.
Hey-Jarvis keyword spotting in sherpa-onnx needs a BPE-tokenised keyword file produced with Python tooling.

## Decision
1. **Tap-to-talk first:** tapping the orb (or, later, a hotkey) opens the mic; JARVIS stops listening when you stop talking.
2. **All listening through sherpa-onnx** (one native runtime): Silero VAD for end-of-speech, Whisper **small.en int8**
   (112 MB encoder + 262 MB decoder, from `csukuangfj/sherpa-onnx-whisper-small.en` on Hugging Face, fetched as
   individual files so only the int8 versions are downloaded) for transcription. Model id is one constant; Moonshine
   base (287 MB, faster) is the drop-in alternative.
3. **Half-duplex for now:** mic frames are ignored while George speaks (+300 ms tail) so JARVIS never hears itself.
   Voice barge-in needs echo cancellation — later. Typed "stop" still interrupts.
4. **Mic follows the coordinator's mode:** on in Listening / FollowUp, off in Idle / Thinking / MicOff.
5. Capture with `cpal` (same version rodio uses), mono, resampled to 16 kHz in our own tested code.
6. **"Hey Jarvis" wake word next**, as a separate step once the KWS keyword file can be generated (spike).

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| openWakeWord + whisper-rs + ort (ADR-004) | Pretrained "hey jarvis" | Second ONNX runtime, more native builds | Link-clash risk; one runtime is simpler |
| Always-on Whisper scanning for "Jarvis" | No keyword model | Constant CPU; JARVIS's own TV audio triggers it | Wasteful and false wakes |
| Cloud speech-to-text | Most accurate | Audio leaves the PC | Violates privacy rule |
| Whisper full archive | Official bundle | Includes fp32 copies (~1.3 GB) | Individual int8 files are ~375 MB |

## Consequences
- Positive: talk instead of type; one runtime for ears + voice; offline; private.
- Negative: ~375 MB one-time download; no voice barge-in yet; tap still needed until the wake word lands.
- Risks: Whisper latency on CPU (mitigate: measure, swap to Moonshine); TV audio in the room picked up during
  Listening (VAD + short listening windows); mic permission prompts on Windows (handled by the OS).
