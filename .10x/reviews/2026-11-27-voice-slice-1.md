# Voice slice 1: review record

Date: 27 to 28 November 2026. Branch `voice-slice-1`, head `ed6e471`. Design: `.10x/specs/2026-11-27-voice-upgrade-design.md` (revision 2). Plan: `.10x/plans/2026-11-27-voice-slice-1.md`. Decision: `.10x/adrs/015-pipecat-voice-helper.md`.

## Where the branch stands
- Nine planned tasks, plus four small additions: 8c (pin the Kokoro files), 8d (`sample_rate_hz`), 9a (the release job) and the final-review fix wave.
- `main` (`a4276f1`) was merged into the branch at `41afaac`.
- CI is green on Windows and Linux at `ed6e471` (run 46115401713).
- NOT merged to `main`. NOT installed. The live check on Manj's PC is NOT done. Each needs his separate go-ahead.

## How it was reviewed
- Each task was built by one agent and checked by a separate, independent reviewer, who read the code against the plan.
- A task with findings went through a fix loop and a scoped re-review until it was clean.
- After Task 9, one independent whole-branch review ran on the full change (`a4276f1` to `41afaac`), using the most capable model. It read every non-test source file the branch touches. It did not run the tests.
- One fix wave applied the findings that had to land before merge. A scoped re-review then checked the wave.
- Tasks 4, 5, 7, 8, 8c, 8d and 9a were also checked by CI on both systems at each push.

## Tasks and outcomes
| Task | What | What the review caught |
|---|---|---|
| 1 | Helper protocol and main loop | Clean. Minor items on strict parsing and awkward text were done in Task 2. |
| 2 | Pipecat pipeline, Kokoro George | Two Important: Pipecat's own log text could carry what was said (now withheld), and a dead pipeline left the helper alive and mute (now reported, and the helper exits). Re-review clean. |
| 3 | Freeze the helper in CI | Windows tests hung. A real defect: on Windows the helper would have stopped at start-up after the first message. Fixed at the root in two Windows rounds. Review clean. |
| 4 | Rust side of the bridge | One Important: a blocked write could hang shutdown for ever. Fixed with a writer thread and a kill that frees a stuck helper. CI run 46045683433 green. |
| 5 | Verified install | Clean. The hash is checked before anything is unpacked. CI run 46047788779 green. |
| 6 | `voice.toml` | Clean. All 23 defaults and ranges matched the plan. |
| 7 | Log file and reply cap | A Minor raised to a fix: with `RUST_LOG=debug` the heard text could reach the file. The file layer is now capped at info. CI run 46051232829 green. |
| 8a | Supervisor logic | Clean. Nine scenarios traced; no silent resting state reachable. One item carried to 8b (helper generation numbers). |
| 8b | App wiring | Two Important page gaps: a lasting voice line could be hidden, and an old status could overwrite a newer one. Fixed with ruling R18 added. CI run 46055399361 green. |
| 8c | Pin the Kokoro files | Clean. The helper build checks the runner's own copy against the pins. CI run 46095611696; helper run 46095611759. |
| 8d | `sample_rate_hz` | Clean. Manj's own settings block now loads with no warning. CI run 46096812617 green. |
| 9a | Release job | Clean. No path can replace a published asset. Helper run 46097597302 green. |
| 9 | Release, pin, merge of `main` | Tag published by Manj, asset pinned (`826c3b1`), tagged build run 46113584835, CI run 46114260919 green on `41afaac`. |
| Final | Whole branch, then fix wave | 0 Critical, 2 Important, 10 Minor. Both Important fixed and re-reviewed clean. |

## The helper release
- Tag `voice-helper-v1` on commit `cfa7d12`, published as a pre-release by Manj on 28 Nov 2026 from GitHub's Releases page. This session could not push tags (HTTP 403).
- Asset `jarvis-voice-helper-win-x64.tar.bz2`, 85,816,946 bytes, SHA-256 `8e3ff1def8dc35930ce59a44aa849d62480395011d47a5d64b4dd303659fd8ae`, pinned in `crates/jarvis-helper/src/install.rs`.
- The same value came from the build log, the release notes, GitHub's asset digest and an independent download.
- Kokoro v1.0 files pinned: `kokoro-v1.0.onnx` `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5`; `voices-v1.0.bin` `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d`.
- End-to-end proof on the build machine: the real install code, with the real addresses and the three real pins, installed the helper and both voice files. A second call did nothing.

## Manj's decisions on 28 Nov
- Pin the Kokoro files: yes.
- Branch `listen-60` (the 60-second listening window) stays out of `main` and out of this merge.
- His recommended 22-setting block was checked against the build. 21 matched. `sample_rate_hz` was missing and was added. The block now loads with no warning, and a test holds it word for word.
- Only `voice_engine` and `tts_speed` act in this slice. The other settings are read and held for slices 2 and 3.

## The final review's two important findings
- **I1. The helper's voice library could log what was said.**
  - The Kokoro library (`kokoro_onnx`) logs each sentence's phonemes (the reply as sounds) when the environment variable `LOG_LEVEL=DEBUG` is set on the PC.
  - Helper output goes to the log file, so each reply could have landed there for 7 days.
  - Fix (`c952ea2`): the app never passes `LOG_LEVEL` to the helper. A test holds it.
  - The Python half (cap that library's own logging) needs a new helper build, so it goes to slice 2.
- **I2. The built-in voice counted as working with no sound output.**
  - The screen said "Using my built-in voice." while nothing could speak, and showed no "Try again".
  - Fix (`62f7acc`): the app opens the sound output first. If that fails, the screen says "My voice is unavailable." with "Try again".
- Also fixed in the wave:
  - The helper's start time is logged as `helper_ready_ms` (`ed6e471`).
  - An installed folder with a required file missing (for example quarantined by antivirus) is installed again instead of trusted (`3117679`).
  - A stale code comment on how writes are queued (`dfbfada`).
- Scoped re-review: all five addressed, no new Critical or Important. CI green on `ed6e471`.

## Carried to slice 2 (known limits)
- The helper may report "finished speaking" 50 to 200 ms before the sound ends, so the ears may catch the tail.
- Nothing notices a helper that is alive but stuck.
- On first start JARVIS is caption-only during the 440 MB download and up to 60 s of start-up.
- Downloads have no idle timeout.
- The helper ends with the app only because its input closes. A Windows Job Object would guarantee it.
- Before the next helper tag: `uv sync --locked`, and pinned action versions.
- The Python half of I1.
- `first_chunk_max_words` is added only if the log shows a slow first word.
- Smaller items from the review, such as old helper folders never removed and an unbounded line buffer, are in the whole-branch review and go to slice 2.

## What could not be proven off the PC
- Whether the helper says "ready" inside 60 s on a first cold start. The reviewer estimates 15 to 40 s on an ordinary laptop with antivirus, and perhaps a 10 to 20% chance of passing 60 s the first time. If it does, Manj sees "Voice helper unavailable. Using my built-in voice.", George still speaks, and the next start tries again.
- Real sound output: gaps, clicks, and the 300 ms stop bound on the real device.
- Windows antivirus behaviour on the frozen helper.
- A real helper's speaking events, and whether a missing output device shows as a helper error.
- Tauri's exit path, so whether the last log lines are written at exit.
- The log file lives at `%LOCALAPPDATA%\in.mhsurgery.jarvis\logs\jarvis.<date>.log` (daily, 7 kept, info and above, never what was said). Live-check item 8 in the plan names `jarvis.log`; the real name is `jarvis.<date>.log`.

## Rulings that changed the plan
Each ruling was made by the controller during the build. "Cost if wrong" is as the ledger gave it.
- **R1.** `Settings` is a dataclass with exactly the five hello fields (voice, tts_speed, model_path, voices_path, output_device). The plan did not name the type. Cost if wrong: a rename.
- **R2.** `pipecat-ai[kokoro]==1.17.0` is the base dependency, and the audio extra (PyAudio) is optional, installed in CI and the frozen build. Tests then run where the audio headers are missing. Cost if wrong: one line in `pyproject` and `helper.yml`.
- **R3.** An exception from say or stop after ready ends the helper with one `internal` error line and exit 1. The app's supervisor then restarts or falls back. Cost if wrong: one helper restart per recoverable speak error.
- **R4.** The pipeline uses `PipelineWorker` run by `WorkerRunner`, because `PipelineTask` and `PipelineRunner` are deprecated since Pipecat 1.3.0. Cost if wrong: none.
- **R5.** A helper that is alive but mute after its pipeline dies is an Important finding, not Minor. The helper writes one `internal` error and exits non-zero. Reason: the design's failure test is "JARVIS going silent". Cost if wrong: a few lines.
- **R6.** The Windows start-up deadlock is fixed at the root. The helper moves the app's pipe to a private copy for its reader and points standard input at NUL. A smoke test of the real program with real Kokoro and a null output runs in CI against the frozen exe. Reason: nothing else exercised native-library loading under a blocked reader. Cost if wrong: one diagnostic flag and one CI step.
- **R7.** The `Speaks` trait lives in `jarvis-core`, not `jarvis-voice` as the plan said. Reason: `jarvis-voice` cannot compile on the build machine, so a trait there would make `jarvis-helper` untestable locally. Cost if wrong: move one small file.
- **R8.** `process::spawn` takes an extra `args` list (production passes none) so tests can start the fake helper with its flags. Cost if wrong: one parameter.
- **R9.** Two wall-clock test assertions go into the Task 4 fix round. Reason: a flaky test on a loaded Windows runner costs a CI round each time. Cost if wrong: none.
- **R10.** The download-and-unpack code moves unchanged into a new pure crate, `jarvis-fetch`, and `jarvis-voice` re-exports it. Reason: the integrity check is this slice's one security-relevant piece and must have a local test run. Cost if wrong: a file move; CI proves the re-export.
- **R11.** The helper installs into a folder named after its tag (`voice-helper-v1`), not a fixed name. Reason: with a fixed name and a complete marker, a later helper version would never be downloaded. Cost if wrong: one constant.
- **R12.** `voice_config.rs` lives in the pure crate `jarvis-helper`, not in the app crate. Reason: the app crate cannot compile on the build machine. Cost if wrong: move one file.
- **R13.** The log folder is worked out before Tauri starts, from the app identifier and the per-user data folder. Set-up then checks it matches and warns if not. With daily rolling, files are named `jarvis.<date>.log`, not `jarvis.log`. Cost if wrong: the log lands in another folder, and the set-up check says so.
- **R14.** The log file has its own cap at info, whatever `RUST_LOG` says. Reason: the design says the file never records what was said, and `RUST_LOG=debug` would have written the heard text for 7 days. Cost if wrong: debug detail is unavailable in the file (still on stderr).
- **R15.** Task 8 was built in two dispatches with a review each: 8a the supervisor as pure logic, 8b the app wiring. Reason: the plan put the supervisor in the app crate, where none of its tests could run locally. Cost if wrong: one extra review.
- **R16.** The supervisor keeps the last status so the window can ask for it on load (`voice_status`). Reason: a lasting state such as "Voice helper unavailable" sent before the page listens would never be shown. Cost if wrong: one getter.
- **R17.** On non-Windows builds nothing changes: no supervisor, the web stand-in voice stays. The plan's "no Windows voice" rule is about Windows. Cost if wrong: not stated in the ledger.
- **R18.** On Windows with no data folder, the page now says the voice is unavailable instead of "ready". Reason: it is the silently-mute case the design forbids. Cost if wrong: none.
- **R19.** The Kokoro pins are added as Task 8c. The pins are the SHA-256 of the files downloaded on 27 Nov and used for the self-test. An upstream cross-check was not possible from this session, so `helper.yml` gained a step where the CI runner's own download must match. Cost if wrong: CI fails loudly and nothing ships. That step passed.
- **R20.** A complete voice-file folder is not re-hashed at every start. The check runs at download time only, as for the helper archive. Cost if wrong: on-disk tampering after install is not caught, which is outside this slice's threat model.
- **R21.** `sample_rate_hz` is added as a 24th key that accepts only 24000, with a test holding Manj's block word for word. The design's table had dropped that key without saying so. Cost if wrong: one inert key.
- **R22.** The release job accepts a release that the owner creates himself on GitHub. If a release for the tag exists, the asset is uploaded to it without `--clobber`, so a published asset is never silently replaced. Otherwise it creates one as before. Reason: this session could not push tags (HTTP 403). Cost if wrong: one small workflow edit.
- **R23.** `main` is merged into the branch, not rebased, before the final review. Reason: the review and CI see exactly what will land. Cost if wrong: one merge commit in history.
- **R24.** One fix wave carries I1 (Rust side), I2, M1 (log `helper_ready_ms`), M2 (a marker with a missing required file means not installed) and M8 (stale comment). The rest go to slice 2. Cost if wrong: the slice-2 items stay as known limits for the live check.
- **R25.** M2's change also applies to the built-in voice and hearing models: a marked folder with a required file missing is installed again. Judged a repair of a broken state. Cost if wrong: one extra download on a PC whose model folder was damaged.

## The live check
Eight items, in the plan's Task 9, Step 6. It runs on Manj's PC with fictional phrases only, after the installer is built and installed, each with his go-ahead.
