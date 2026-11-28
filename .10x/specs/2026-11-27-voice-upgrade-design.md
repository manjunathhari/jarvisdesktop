# JARVIS voice upgrade — design

*27 Nov 2026, revision 2. Status: **approved by Manj, 27 Nov 2026 (21:50)**, including "go to sleep" as standby. Plan for slice 1: `.10x/plans/2026-11-27-voice-slice-1.md`. Code read at `main` = `27ee333`. Revision 1 proposed fixing the voice inside the Rust app; Manj chose the Pipecat helper route instead (27 Nov, 21:46), so this revision replaces it.*

## What Manj asked for
In his words: the voice must be **clean and smooth**, **fade out when talked over**, **stop when someone is talking**, and stay awake. He set the sleep timer to **0, never sleeps**. He chose **Pipecat as a helper in front of his own server**, because its talk-over and turn-taking are already proven and it leaves room for the future.

## What is wrong today (from the code)
1. **The Windows voice can take over silently.** When George is not loaded the app speaks through the web view's `speechSynthesis`. The screen shows nothing, and the installed app writes no log file.
2. **George pauses between sentences.** One sentence is made, played to the end, and only then is the next one made.
3. **He is deaf while he talks.** The mic is off while he answers.
4. **Replies are cut at 8 seconds.** The app gives up on a reply whose last piece arrives more than 8 s after the request (`BRAIN_TIMEOUT`).
5. **The sleep clock starts too early**, when the reply text arrives, not when George finishes.

## The route
```
Microphone -> Pipecat helper (hearing, turns, talk-over, George) -> Speakers
                    ^  |
              reply |  | your words, as text
                    |  v
               JARVIS app (screen, Board, approvals, wake word)
                    ^  |
                    |  v
               JARVIS server on Hostinger -> Claude
```
- The helper is a second program on the PC. It owns the mic and the speakers while JARVIS is awake.
- In Pipecat's pipeline the place where an AI model normally sits is filled by **a bridge to the JARVIS app**. The helper never calls Claude and holds no key.
- The server, memory, approvals, the Board and the cost cap are untouched.
- Sound never leaves the PC (ADR-004 holds). Only text goes to the server, as today.

## Goals
- George is the only voice on Windows, with no gaps or clicks inside a reply.
- When someone speaks over him he fades down, stops, and listens to what was said.
- Once awake he stays awake until told "go to sleep".
- Everything in the voice path stays free and on the PC.
- Voice engines can be swapped later by changing settings, not by rewriting the app.

## Not in this build
- Pipecat talking straight to Claude (it skips the server).
- Voice on the phone node. The helper does not run on Android; the phone's voice will be its own piece of work.
- The gapless fix inside the Rust voice. The built-in George stays as he is, as the fallback.
- Cancelling an interrupted reply on the server. The desktop drops the rest; the server still finishes it. Logged as a server follow-up.
- Telling voices apart. "Someone" means any voice in the room.

## What would make it a failure
- George interrupting himself because he hears his own voice. Talk-over on speakers ships only at zero self-interruptions in the test below.
- JARVIS going silent because the helper did not start. The app must fall back and say so on screen.

## Approaches
| | Trade-off | Decision |
|---|---|---|
| Fix the Rust voice path | One small app; same code for the phone. Talk-over and turn-taking would be new code, tuned from scratch. | Not chosen. It was Claude's recommendation. |
| **Pipecat helper in front of the server** | Proven talk-over and turn-taking, Smart Turn, engines swappable. Adds a second program, a bridge, and a separate phone voice later. | **Chosen by Manj, 27 Nov** |
| Pipecat straight to Claude | Fastest demo. Skips the server and needs a Claude key on the PC. | Dropped |

## Decisions inside the chosen route
All of these are Claude's calls unless marked.

**The helper**
- A Python program in the repo (`helper/`), on a pinned Pipecat version (BSD-2).
- CI builds it into a ready-to-run Windows program. The app downloads it on first start, the same way it downloads the voice models, so nothing is installed by hand and the installer stays small. If freezing it proves unreliable, the fallback is that the app sets up a private Python environment itself.
- The app starts the helper, watches it, and restarts it once if it dies.
- **Fallback:** if the helper cannot run, the app uses today's built-in George and shows "Voice helper unavailable". The Windows voice is removed either way.
- `voice_engine = "pipecat"` or `"builtin"` in the settings file. This is the on/off switch Manj asked for.

**The bridge**
- The app and the helper talk over the helper's own input and output pipes, one message per line. No network port is opened.
- Helper to app: ready, heard text, speech started, interrupted, George started and finished, errors.
- App to helper: say this text (with the turn it belongs to), stop, listen on and off, settings.
- Spoken approvals ("yes", "no") and "stop" arrive as heard text and are handled by the app as today.

**Engines inside the helper**
- Voice: Kokoro v1.0, voice `bm_george`, through Pipecat's Kokoro service.
- Hearing: faster-whisper, `small.en`, 8-bit, on the CPU. Same model as today.
- Speech detection: Silero VAD.
- End of turn: Smart Turn v3, which is Pipecat's default and runs on the CPU. A fixed-silence rule is the fallback.

**Wake word**
- Stays in the Rust app, where it caught 20 of 20. In standby the app listens for "Jarvis" as today and the helper's mic is closed. On waking, the app closes its mic and the helper opens its own. "Go to sleep" reverses that.

**Talk-over**
- Pipecat stops George when someone speaks, and drops the rest of what he was going to say. That part is ready-made.
- The dip to 30% before stopping, and coming back to full volume after a cough or "mm-hm", are small additions of ours on top. Pipecat does not do those by itself.
- The interrupting words are kept and become the next request. Late pieces of the old reply from the server are dropped by the app.

**Echo cancellation**
- Pipecat's local mic input has none of its own. It has a slot for an input filter, and that is where a WebRTC echo canceller goes, with George's own output as the reference.
- Chosen by a test on Manj's PC. If it cannot reach zero self-interruptions on speakers: full talk-over on headphones, and on speakers only the word "Jarvis" stops him.

**Staying awake** (Manj: `follow_up_window_s = 0`)
- Once JARVIS has answered he keeps listening until someone says "go to sleep". Everything said in the room is treated as a request; each is a call to Claude against the monthly cap. Manj accepted this.
- The 10 s wait after a wake greeting stays: if nothing at all is said he returns to standby.
- **"Go to sleep" becomes standby** (the wake word still works). "Mic off", "mute" and "stop listening" remain the hard off. *(Approved with the design.)*
- A number above 0 brings the timer back, counted from when George stops speaking.

**Other**
- The 8 s cap on a reply is raised so long answers are not cut.
- The app writes a small rolling log file (voice and helper status, errors, timings; not what was said). The helper's log goes into it.
- Timings are logged from the end of Manj's last word to George's first sound.
- A new ADR (next free number) records Python on the desktop and replaces the "all listening through sherpa-onnx" part of ADR-005.

## Voice settings file
`voice.toml` in the app's data folder, beside `node.toml`. The app reads it at start and passes it to the helper. A missing line uses the default; a bad value is logged and replaced by the default.

| Setting | Start value | Note |
|---|---|---|
| `voice_engine` | "pipecat" | "builtin" switches the helper off. |
| `vad_threshold` | 0.5 | 0.6 if the room is noisy. |
| `vad_min_speech_ms` | 250 | |
| `vad_pre_roll_ms` | 300 | Keeps the first word of an interruption. |
| `smart_turn` | true | Pipecat decides when you have finished. *(Claude's call.)* |
| `end_of_turn_silence_ms` | 700 | Manj's value. Used only when `smart_turn` is false. |
| `max_turn_s` | 30 | |
| `duck_on_speech` / `duck_volume` / `duck_fade_ms` | true / 0.3 / 80 | |
| `barge_in_after_ms` | 300 | |
| `stop_fade_ms` | 60 | |
| `resume_if_false_alarm` / `resume_fade_ms` | true / 250 | |
| `tts_speed` | 1.0 | |
| `tts_chunk` | "sentence" | |
| `first_chunk_max_words` | 12 | Switched off if the split is audible. |
| `sentence_gap_ms` | 0 | |
| `echo_cancellation` / `mic_open_while_speaking` | true / true | Both false if the echo test fails. |
| `follow_up_window_s` | **0** *(Manj)* | 0 = never sleeps. |
| `sleep_phrase` | "go to sleep" | Sends JARVIS to standby. |
| `ignore_noise_min_words` | 2, with exceptions | One-word utterances are ignored unless they are a known command ("stop", "clear", "mute", "quit") or the answer to a pending approval ("yes", "no"). *(Claude's call.)* |

## Build order
Three slices. Each is its own branch, passes CI on Windows and Linux, and ends with a live check on Manj's PC. Pushing, merging and installing each need his go-ahead.

1. **The helper speaks.** CI builds the helper; the app downloads, starts and watches it; the bridge; George speaks replies through Pipecat; status on screen, log file, fallback to built-in George, no Windows voice, the 8 s cap, the settings file. Hearing is still the Rust ears, as today.
2. **The helper listens.** Mic, faster-whisper, Smart Turn, talk-over with dip and recover, the wake-word hand-off, spoken approvals and "stop". Checked on headphones.
3. **Speakers and staying awake.** The echo test and filter, no sleep timer, "go to sleep" as standby, the one-word rule.

## Live checks on Manj's PC (fictional phrases only)
- A six-sentence reply: no gap longer than a natural pause, no clicks, never the Windows voice.
- A reply longer than 8 seconds is spoken to the end.
- The helper is closed by hand: JARVIS says so on screen and built-in George still speaks.
- Ten talk-overs: he has stopped within about half a second each time, and the interrupting words come through as the next request in at least 9.
- A cough and an "mm-hm": he dips and recovers.
- George speaks for 2 minutes on speakers with the live TV on: **0** self-interruptions.
- After a reply he is still listening after 5 silent minutes; "go to sleep" sends him to standby; "Jarvis" wakes him.
- A wake with nothing said: back in standby after 10 s.

## Left to the builder
Exact Pipecat version and how its turn and interruption settings are used; how the helper is frozen; the message format of the bridge; which WebRTC echo canceller; where the dip and recover sit in the pipeline; test design on both sides (Python tests for the helper, Rust tests for the bridge and fallback).

## Risks
- **Freezing a Python program** with these audio and model libraries for Windows can be fragile. Slice 1 proves it first; the private-environment fallback exists for this.
- **Pipecat changes quickly.** The version is pinned, and upgrades are deliberate.
- **Echo on speakers** is unsolved until the test in slice 3. Never sleeping makes it matter more: anything of George's that leaks through would come back as a request.
- **Load on the PC.** Whisper, Kokoro and Smart Turn share the CPU with the app. Timings from slice 1 and 2 show whether a smaller hearing model is needed.
- **Two owners of the mic.** The hand-off between the app's wake word and the helper must never leave both open or both closed.
- **Other voices cost money.** People talking in the room while he is awake become paid requests.
- **Licences.** Kokoro's pronunciation data (espeak-ng) is GPL-3.0. Fine for personal use; to be checked, with every helper dependency, before JARVIS is offered to other families.
