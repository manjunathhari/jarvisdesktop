# ADR-015: A Python voice helper (Pipecat) beside the Rust app

**Status:** Accepted (approved by Manj on 27 Nov 2026, design revision 2)
**Date:** 2026-11-28
**Feature:** voice-upgrade (slice 1: the helper speaks)
**Author:** 10x-Team (Architect + Engineering Manager)

## Context
Manj asked for a voice that is clean and smooth, fades out when talked over, stops when someone is talking, and stays awake. He set the sleep timer to 0, meaning never sleep.
The review of the old code (design, 27 Nov) found five faults:
- On Windows the app could fall back to the web view's own speech without any sign on screen, and the installed app wrote no log file.
- George paused between sentences, because one sentence was made, played to the end, and only then was the next one made.
- George was deaf while he spoke, because the mic was off during his answer.
- A reply was cut off when its last piece came more than 8 seconds after the request.
- The sleep clock started when the reply text arrived, not when George finished speaking.
Pipecat is an open-source (BSD-2) Python toolkit for voice agents. Its talk-over and turn-taking logic is already proven. Manj chose a Pipecat helper over a Rust-only fix for that reason (27 Nov, 21:46). Slice 1 was built on branch `voice-slice-1`. In this slice only the speaking half is live.

## Decision
- **The helper is a Python program in the repo (`helper/`)**, on a pinned Pipecat version (1.17.0). In Pipecat's pipeline, where an AI model normally sits, there is a bridge to the JARVIS app. The helper never calls a model service and holds no key.
- **CI builds it into a frozen Windows program** (one folder, no Python install needed). The tagged build is `voice-helper-v1`. The app downloads it on first start and checks its SHA-256 before unpacking. The two Kokoro voice files (`kokoro-v1.0.onnx`, `voices-v1.0.bin`) are downloaded the same way, each checked against a pinned SHA-256.
- **The app starts it as a child process.** The two programs talk over the helper's input and output pipes, one JSON message per line. No network port is opened.
- **A supervisor in the app decides which voice speaks.** It tries the helper first. If the helper dies it restarts it once per app run. After a second failure, or if the helper cannot start within 60 seconds, it uses the built-in George. The screen always says which voice is in use. If neither can speak, the screen says "My voice is unavailable." with a "Try again" button.
- **`voice_engine` in `voice.toml`** is the switch: `"pipecat"` or `"builtin"`. In slice 1 only `voice_engine` and `tts_speed` act. The other settings are read and held for slices 2 and 3.
- **The wake word stays in the Rust app**, where it caught 20 of 20 in testing.
- **Three slices:**
  1. The helper speaks (built, this ADR).
  2. The helper listens: mic, hearing, talk-over, the wake-word hand-off.
  3. Speakers and staying awake: echo cancellation, no sleep timer, "go to sleep" as standby.
- **The Windows web-view voice is removed.** On Windows, speech with no voice available shows as a caption only.

### What this changes in earlier ADRs
- **ADR-005 (listening via sherpa-onnx):** from slice 2 onward, this ADR replaces the "all listening through sherpa-onnx" decision. The helper will do the hearing (faster-whisper with Silero speech detection). The Rust wake word stays. Until slice 2 ships, ADR-005 is unchanged and the Rust ears still run.
- **ADR-004 (local audio pipeline):** holds. Sound stays on the PC. Only text goes to the server, as before.
- **The server, memory, approvals, the Board and the cost cap** are untouched.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Fix the Rust voice path only | One small app; same code could serve the phone | Talk-over and turn-taking would be new code, tuned from scratch | It was Claude's recommendation. Manj chose Pipecat because its talk-over logic is already proven |
| Pipecat talks straight to Claude | Fastest demo | Skips the JARVIS server and needs a Claude key on the PC | Dropped in the design |
| A hosted voice service | Nothing to run on the PC | Sound would leave the PC, and the voice path would no longer be free | ADR-004 keeps sound on the PC, and the design's goal is a free voice path on the PC |

## Consequences
### Positive
- Talk-over and turn-taking come ready-made from Pipecat. Slice 2 adds the dip and recover on top.
- The voice engines can be swapped by changing settings.
- A failure never leaves JARVIS silent without saying so: the fallback voice speaks and the screen names the voice in use.
- The app now writes a rolling log file that never records what was said.
- Replies may run up to 30 seconds, with 8 seconds allowed before the first piece arrives.

### Negative
- A second runtime on the PC: a frozen Python program beside the Rust app.
- A first-start download of about 86 MB (helper) plus 354 MB (voice files).
- A 60 second window for the helper to say it is ready.
- Two voices to keep working: the helper's George and the built-in George.
- The helper does not run on the phone node. The phone's voice is its own piece of work.

### Risks
- **Cold start over 60 seconds.** Only Manj's PC can settle this. The reviewer estimated 15 to 40 seconds for a first cold start on an ordinary laptop with antivirus, and a 10 to 20% chance of passing 60 seconds the first time. If that happens he sees "Voice helper unavailable. Using my built-in voice.", George still speaks, and the next start tries again. The app logs `helper_ready_ms` to measure it.
- **Pipecat changes quickly.** The version is pinned and upgrades are deliberate.
- **Echo on speakers** is unsolved until slice 3. Never sleeping makes it matter more.
- **Licences.** Kokoro's pronunciation data (espeak-ng) is GPL-3.0. Fine for personal use; to be checked, with every helper dependency, before JARVIS is offered to other families.
- **Known limits carried to slice 2** are listed in `.10x/reviews/2026-11-27-voice-slice-1.md`.
