# JARVIS voice, slice 1 (the helper speaks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

*Status: written 27 Nov 2026, awaiting Manj's review. Repo `manjunathhari/jarvisdesktop`, `main` = `27ee333`. Branch: `voice-slice-1`.*

**Goal:** George speaks every reply through a Pipecat helper program on the PC, without gaps, with the Windows stand-in voice gone and a visible fallback to the built-in George if the helper cannot run.

**Architecture:** A Python program (`helper/`, Pipecat 1.17.0) runs a two-stage pipeline: Kokoro v1.0 voice, then the local speaker. The Tauri app downloads a frozen build of it, starts it as a child process, and talks to it in JSON lines over its stdin and stdout. A supervisor in the app picks the helper or the built-in voice, restarts the helper once, and falls back. Hearing stays in the Rust ears in this slice.

**Tech Stack:** Python 3.12, uv, `pipecat-ai[kokoro,local]==1.17.0`, PyInstaller, pytest, ruff. Rust: new crate `jarvis-helper`; `sha2` (workspace), `tracing-appender = "0.2"`. GitHub Actions.

**Spec:** `.10x/specs/2026-11-27-voice-upgrade-design.md` (project doc `claude/voice-upgrade-design.md`), revision 2, approved by Manj 27 Nov.

## Operating rule

As in Plan B and the Board: one task at a time, an independent review and its fixes after each, then stop for Manj. Work stays local: no push, tag, merge, PR or install without his separate approval. If the code or Pipecat shows a step here is wrong, stop, write the mismatch and a proposed change in the task report, and wait.

## Global Constraints

- **Public repo:** no secrets, addresses or tailnet IPs in any committed file.
- **Sound stays on the PC (ADR-004).** The helper opens no network port and makes no network call. All downloads are done by the app.
- **The helper never calls Claude** and holds no key.
- **Pinned versions:** Python 3.12; `pipecat-ai[kokoro,local]==1.17.0`; `helper/uv.lock` committed.
- **Existing behaviour is frozen** except where a task says otherwise: wake word, ears, approvals, news wall and Board behave as at `27ee333`. No existing test is weakened.
- **Hearing is unchanged in this slice.** The Rust ears keep ignoring the mic while George speaks, using the helper's speaking signal.
- **No Windows voice on Windows.** The app never emits `jarvis://speak` on Windows.
- **Nothing said is logged** at `info` or above, in the app or the helper: no utterance text, no reply text.
- **Protocol:** UTF-8, one JSON object per line, `protocol` = 1. The helper's stdout carries protocol lines only; its logs go to stderr. Either side ignores an unknown `type` or field.
- **Voice:** Kokoro v1.0, voice `bm_george`, language `en-gb`, speed from `tts_speed`.
- **Exact screen copy:** "Getting my voice helper ready, {percent}%" · "Starting my voice helper…" · "Voice helper unavailable. Using my built-in voice." · "My voice is unavailable." with a button "Try again".
- **CI:** existing jobs unchanged. New workflow `helper.yml`: `ruff check`, `ruff format --check`, `pytest` on ubuntu-latest and windows-latest; freeze and self-test on windows-latest.
- **Live tests:** scripted, fictional phrases only.

## Review Focus

1. **The helper dies while George is mid-sentence.** The ears must not stay deaf: `is_speaking()` is false the moment the process is gone. (Task 4)
2. **"Stop" with several sentences queued.** Sound ends within 300 ms, nothing queued plays later, and the next reply plays normally. (Task 2)
3. **Awkward reply text:** line breaks, quotes, emoji, 2,000 characters, or only spaces. Each `say` is exactly one line; blank text is never sent. (Tasks 1 and 4)
4. **No speaker, or the helper blocked from starting** (antivirus, missing file). JARVIS falls back and says so on screen; he is never silently mute. (Tasks 2 and 8)
5. **A broken or tampered helper download.** A file whose SHA-256 differs is deleted and never run; the next start tries again. (Task 5)

## File Structure

| Path | Responsibility |
|---|---|
| `helper/pyproject.toml`, `helper/uv.lock` | Helper project and pinned dependencies |
| `helper/jarvis_voice_helper/protocol.py` | Message types, parse and encode |
| `helper/jarvis_voice_helper/main.py` | stdin/stdout loop, exit codes |
| `helper/jarvis_voice_helper/pipeline.py` | Pipecat pipeline: Kokoro to speaker; speaking events; stop |
| `helper/jarvis_voice_helper/selftest.py` | Synthesise a phrase to a WAV file without a sound device |
| `helper/jarvis-voice-helper.spec` | PyInstaller build |
| `.github/workflows/helper.yml` | Helper tests, freeze, self-test, release asset |
| `crates/jarvis-voice/src/lib.rs` | New trait `Speaks`, implemented by `Voice` |
| `crates/jarvis-voice/src/model.rs` | `ensure_archive_checked` (SHA-256) |
| `crates/jarvis-helper/src/protocol.rs` | The same messages in Rust |
| `crates/jarvis-helper/src/link.rs` | Read and write the pipes; `HelperVoice` |
| `crates/jarvis-helper/src/process.rs` | Start the helper program |
| `crates/jarvis-helper/src/install.rs` | Download the helper and the Kokoro v1.0 files |
| `crates/jarvis-helper/src/bin/fake-helper.rs` | Test double that speaks the protocol |
| `app/src-tauri/src/voice_config.rs` | `voice.toml` |
| `app/src-tauri/src/logfile.rs` | Rolling log file |
| `app/src-tauri/src/voice_mode.rs` | Supervisor: choose, start, watch, fall back |

---

### Task 1: Helper protocol and main loop

**Files:** Create `helper/pyproject.toml`, `helper/uv.lock`, `helper/jarvis_voice_helper/{__init__,protocol,main}.py`. Test: `helper/tests/test_protocol.py`, `helper/tests/test_main.py`.

**Interfaces — Produces:**
- App to helper: `{"type":"hello","protocol":1,"settings":{"voice":"bm_george","tts_speed":1.0,"model_path":str,"voices_path":str,"output_device":null}}`, `{"type":"say","turn":int,"text":str}`, `{"type":"stop"}`, `{"type":"shutdown"}`.
- Helper to app: `{"type":"ready","protocol":1,"helper_version":str,"voice":str}`, `{"type":"speaking","on":bool,"turn":int}`, `{"type":"error","code":str,"message":str}` with `code` one of `bad_hello`, `model_missing`, `no_output_device`, `internal`.
- `protocol.parse(line: str) -> Hello | Say | Stop | Shutdown | None` (None for an unknown type or bad JSON); `protocol.encode(msg) -> str` (one line, no trailing newline, `ensure_ascii=False`).
- `class Speaker(Protocol)`: `async start(settings) -> None`, `async say(turn: int, text: str) -> None`, `async stop() -> None`, `async close() -> None`; it reports through a callback `on_speaking(on: bool, turn: int)`.
- `async main.run(reader, writer, speaker_factory) -> int`. Exit codes: 0 after `shutdown` or stdin end of file, 2 `bad_hello`, 3 `no_output_device`, 4 `model_missing`, 1 anything else.

- [ ] **Step 1: Write the failing tests**

```python
def test_say_round_trips_awkward_text():
    text = 'He said "hi".\nNew line 🙂 ' + "x" * 2000
    line = protocol.encode(Say(turn=7, text=text))
    assert "\n" not in line and protocol.parse(line) == Say(turn=7, text=text)

def test_unknown_type_and_bad_json_are_ignored():
    assert protocol.parse('{"type":"later"}') is None and protocol.parse("{oops") is None

async def test_ready_follows_hello_then_say_reaches_the_speaker(): ...   # FakeSpeaker.said == [(1, "Hello.")]
async def test_first_line_not_hello_exits_2_with_bad_hello(): ...
async def test_stdin_end_of_file_closes_the_speaker_and_exits_0(): ...
async def test_blank_say_is_dropped(): ...                                # text "   " never reaches the speaker
async def test_speaker_start_error_maps_to_its_exit_code(): ...          # NoOutputDevice -> error line + 3
```

- [ ] **Step 2: Run** `cd helper && uv run pytest -q`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement** `protocol.py` (dataclasses) and `main.run` as specified. The loop reads lines without blocking the event loop; stdout is flushed after every line.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check && uv run ruff format --check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(helper): protocol and main loop`.

### Task 2: Pipecat pipeline (Kokoro to speaker)

**Files:** Create `helper/jarvis_voice_helper/pipeline.py`, `selftest.py`. Modify `main.py` (`__main__` entry, `--selftest PATH`). Test: `helper/tests/test_pipeline.py`.

**Interfaces — Consumes:** `Speaker`, exit codes (Task 1). **Produces:** `class PipecatSpeaker(Speaker)`; `build_pipeline(tts, sink) -> PipelineTask`; `selftest.run(out_wav: Path, model_path, voices_path) -> int`.

Decisions: the pipeline is `[reporter, KokoroTTSService(model_path=..., voices_path=..., settings=Settings(voice=..., language=EN_GB)), LocalAudioTransport(LocalAudioTransportParams(audio_out_enabled=True)).output()]`. `say` queues one `TTSSpeakFrame(text)`; `stop` queues `InterruptionFrame()`. `reporter` is the first processor and turns the upstream `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` into `on_speaking`, with the turn of the most recent `say`. A missing model or voices file raises `ModelMissing` before Pipecat is started (the helper must never download). A PyAudio open failure raises `NoOutputDevice`. `selftest` swaps the speaker output for a sink that collects audio frames.

- [ ] **Step 1: Write the failing tests** (a fake TTS service yielding 0.5 s of tone per sentence, and a collecting sink in place of the device)

```python
async def test_three_sentences_play_in_order_with_one_start_and_one_stop(): ...
    # sink holds 3 sentences' audio in order; on_speaking == [(True, 1), (False, 1)]
async def test_stop_silences_within_300_ms_and_drops_the_queue(): ...
    # say x3, stop after 100 ms: no audio frame later than 300 ms after stop; (False, 1) reported
async def test_say_after_stop_plays(): ...
def test_missing_model_file_raises_model_missing(tmp_path): ...
def test_output_open_failure_raises_no_output_device(monkeypatch): ...   # PyAudio.open raises OSError
```

- [ ] **Step 2: Run** `uv run pytest tests/test_pipeline.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `pipeline.py` and `selftest.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then, with the two Kokoro v1.0 files fetched by hand into a temp folder, `uv run python -m jarvis_voice_helper --selftest out.wav --model … --voices …` writes a WAV of "Good evening, Manj. All systems are ready." longer than 1.5 s whose peak is above 0.05.
- [ ] **Step 5: Commit** `feat(helper): Pipecat pipeline with Kokoro George`.

### Task 3: Freeze the helper in CI

**Files:** Create `helper/jarvis-voice-helper.spec`, `.github/workflows/helper.yml`. Modify `.github/workflows/ci.yml` and `release.yml` (`paths-ignore` gains `helper/**` and `.github/workflows/helper.yml`).

**Interfaces — Produces:** asset `jarvis-voice-helper-win-x64.tar.bz2` whose top folder `jarvis-voice-helper/` holds `jarvis-voice-helper.exe`; a release tagged `voice-helper-v<N>`; the asset's SHA-256 printed in the job summary.

Decisions: `helper.yml` runs on changes under `helper/**`, on tags `voice-helper-v*`, and on demand. Job `test` (both OS; Linux installs `portaudio19-dev`). Job `freeze` (windows-latest): PyInstaller one-folder build, console program; then the **frozen** exe runs `--selftest` against Kokoro files downloaded in the job, and the WAV is checked as in Task 2; then it is packed and uploaded as a workflow artifact. Only a `voice-helper-v*` tag publishes a release.

- [ ] **Step 1:** Write `helper.yml` and the spec file. Locally: `uv run pyinstaller jarvis-voice-helper.spec` builds on Linux and `dist/jarvis-voice-helper/jarvis-voice-helper --selftest out.wav …` passes (proves the bundle carries espeak-ng data and the onnx runtime).
- [ ] **Step 2:** With Manj's approval, push the branch and confirm `helper.yml` is green on windows-latest, including the frozen self-test.
- [ ] **Step 3: Commit** `ci(helper): test, freeze and self-test`.

**Stop condition:** if the frozen self-test cannot be made green on Windows, stop and report. The design's fallback (a private Python environment set up by the app) changes Task 5 and needs Manj's decision.

### Task 4: Rust side of the bridge

**Files:** Create `crates/jarvis-helper/{Cargo.toml,src/lib.rs,src/protocol.rs,src/link.rs,src/process.rs,src/bin/fake-helper.rs}`, `crates/jarvis-helper/tests/process.rs`. Modify root `Cargo.toml` (member + workspace dep), `crates/jarvis-voice/src/lib.rs`.

**Interfaces — Produces:**
- `jarvis_voice::Speaks`: `pub trait Speaks: Send + Sync { fn speak(&self, text: &str); fn stop(&self); fn is_speaking(&self) -> bool; }`, implemented for `Voice`.
- `protocol::{ToHelper, FromHelper, HelperSettings}` with serde tags matching Task 1.
- `link::HelperLink::start(reader: impl Read + Send + 'static, writer: impl Write + Send + 'static, on_event: Box<dyn Fn(LinkEvent) + Send>) -> HelperLink`; `enum LinkEvent { Ready { helper_version: String }, Speaking(bool), Error { code: String, message: String }, Closed }`.
- `link::HelperVoice` (`impl Speaks`): `speak` sends `say` with the current turn; `stop` sends `stop` and adds 1 to the turn; `is_speaking` is the last `speaking.on`, forced false on `Closed`.
- `process::spawn(exe: &Path, settings: &HelperSettings, on_event, on_stderr_line: Box<dyn Fn(String) + Send>) -> Result<Running, String>`; `Running { pub voice: Arc<HelperVoice>, .. }` with `shutdown(self)` (sends `shutdown`, waits 2 s, then kills). On Windows the child is created with `CREATE_NO_WINDOW`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn say_is_one_line_whatever_the_text() { /* "a\n\"b\" 🙂" + 2,000 chars -> exactly one '\n', parses back equal */ }
#[test] fn blank_text_is_not_sent() {}
#[test] fn speaking_follows_the_helper() { /* on:true -> is_speaking(); on:false -> !is_speaking() */ }
#[test] fn a_closed_pipe_clears_speaking_and_reports_closed() {}
#[test] fn stop_bumps_the_turn() { /* say(turn 0), stop, say -> turn 1 */ }
#[test] fn unknown_lines_are_ignored() {}
// tests/process.rs, against the fake-helper binary (env!("CARGO_BIN_EXE_fake-helper"))
#[test] fn spawn_reaches_ready() {}
#[test] fn a_helper_that_dies_reports_closed_and_is_not_speaking() { /* --die-while-speaking */ }
#[test] fn shutdown_ends_the_process() {}
#[test] fn a_missing_exe_is_an_error_not_a_panic() {}
```

- [ ] **Step 2: Run** `cargo test -p jarvis-helper`. Expected: FAIL to compile.
- [ ] **Step 3: Implement.** `fake-helper` answers `hello` with `ready`, answers each `say` with `speaking on` then `off`, and takes `--die-while-speaking` and `--never-ready`.
- [ ] **Step 4: Run** `cargo test -p jarvis-helper -p jarvis-voice && cargo clippy --workspace --all-targets -- -D warnings`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(helper): Rust bridge and HelperVoice`.

### Task 5: Install the helper and the voice files

**Files:** Modify `crates/jarvis-voice/src/model.rs`. Create `crates/jarvis-helper/src/install.rs`.

**Interfaces — Consumes:** `Download`, `ensure_files`, `ModelFile`, `ensure_archive`. **Produces:**
- `model::ensure_archive_checked(models_dir, dir_name, url, inner, required, sha256_hex: &str, dl, percent) -> Result<PathBuf, String>`: as `ensure_archive`, but the downloaded file is hashed before unpacking; on a mismatch the file is deleted and the error is `"the {dir_name} download failed its integrity check"`.
- `install::{HELPER_TAG, HELPER_URL, HELPER_SHA256, HELPER_DIR = "jarvis-voice-helper", KOKORO_V1_DIR = "kokoro-onnx-v1_0"}`. `HELPER_TAG = "voice-helper-v1"`; `HELPER_URL` is that tag's `jarvis-voice-helper-win-x64.tar.bz2` on this repo's Releases; `HELPER_SHA256` starts as 64 zeros and is pinned in Task 9.
- Kokoro files: `kokoro-v1.0.onnx` and `voices-v1.0.bin` from `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/`.
- `install::ensure(models_dir: &Path, dl: &dyn Download, percent: &(dyn Fn(u8) + Send + Sync)) -> Result<HelperFiles, String>`; `HelperFiles { exe, model, voices: PathBuf }`.

- [ ] **Step 1: Write the failing tests** (the fake `Download` already used in `model.rs` tests)

```rust
#[tokio::test] async fn a_matching_hash_installs_and_marks_complete() {}
#[tokio::test] async fn a_wrong_hash_installs_nothing_and_leaves_no_file() {}
#[tokio::test] async fn a_failed_install_is_retried_on_the_next_call() {}
#[tokio::test] async fn ensure_returns_the_three_paths_and_downloads_once() {}
```

- [ ] **Step 2: Run** `cargo test -p jarvis-voice -p jarvis-helper`. Expected: FAIL.
- [ ] **Step 3: Implement** (share one private function between `ensure_archive` and `ensure_archive_checked`).
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(helper): verified install`.

### Task 6: `voice.toml`

**Files:** Create `app/src-tauri/src/voice_config.rs`. Modify `app/src-tauri/src/lib.rs` (`mod`).

**Interfaces — Produces:** `VoiceConfig::load(dir: &Path) -> (VoiceConfig, Vec<String>)` (settings, warnings); `VoiceConfig::default()`; `enum VoiceEngine { Pipecat, Builtin }`; one public field per key.

Keys, defaults and accepted ranges (a value outside its range, of the wrong type, or an unknown key gives one warning naming the key, and the default is used):

| Key | Default | Accepts |
|---|---|---|
| `voice_engine` | "pipecat" | "pipecat", "builtin" |
| `vad_threshold` | 0.5 | 0.1 to 0.9 |
| `vad_min_speech_ms` | 250 | 50 to 2000 |
| `vad_pre_roll_ms` | 300 | 0 to 1000 |
| `smart_turn` | true | bool |
| `end_of_turn_silence_ms` | 700 | 200 to 3000 |
| `max_turn_s` | 30 | 5 to 120 |
| `duck_on_speech` | true | bool |
| `duck_volume` | 0.3 | 0.0 to 1.0 |
| `duck_fade_ms` | 80 | 0 to 1000 |
| `barge_in_after_ms` | 300 | 100 to 2000 |
| `stop_fade_ms` | 60 | 0 to 1000 |
| `resume_if_false_alarm` | true | bool |
| `resume_fade_ms` | 250 | 0 to 2000 |
| `tts_speed` | 1.0 | 0.5 to 2.0 |
| `tts_chunk` | "sentence" | "sentence" |
| `first_chunk_max_words` | 12 | 0 to 50 |
| `sentence_gap_ms` | 0 | 0 to 1000 |
| `echo_cancellation` | true | bool |
| `mic_open_while_speaking` | true | bool |
| `follow_up_window_s` | 0 | 0 to 3600 |
| `sleep_phrase` | "go to sleep" | 1 to 40 characters |
| `ignore_noise_min_words` | 2 | 1 to 5 |

Only `voice_engine` and `tts_speed` take effect in this slice; the rest are read now so the file does not change shape in slices 2 and 3.

- [ ] **Step 1: Write the failing tests**: `a_missing_file_gives_the_defaults_and_no_warning`, `a_bad_value_warns_and_uses_the_default` (`tts_speed = 9`), `a_wrong_type_warns` (`smart_turn = "yes"`), `an_unknown_key_warns_and_the_rest_still_loads`, `a_file_that_is_not_toml_gives_defaults_and_one_warning`, `builtin_engine_is_read`.
- [ ] **Step 2: Run** `cargo test -p jarvis-app voice_config`. Expected: FAIL.
- [ ] **Step 3: Implement** by reading a `toml::Table` and checking key by key.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(app): voice.toml`.

### Task 7: Log file, and the 8-second reply cap

**Files:** Create `app/src-tauri/src/logfile.rs`. Modify `app/src-tauri/Cargo.toml`, `app/src-tauri/src/lib.rs` (`run`, and the `heard` line at 449), `crates/jarvis-core/src/runtime.rs`, `crates/jarvis-node/src/server_brain.rs` (comment only).

**Interfaces — Produces:** `logfile::init(dir: Option<&Path>) -> Option<WorkerGuard>`: `info` and above to stderr and to `<data>/logs/jarvis.log`, rolled daily, 7 files kept. `runtime::BRAIN_TIMEOUT = 30 s` (the whole reply) and new `runtime::BRAIN_FIRST_EVENT_TIMEOUT = 8 s` (until the brain's first speech or action).

- [ ] **Step 1: Write the failing tests** in `runtime.rs`, using paused time: `a_reply_that_starts_in_time_may_run_past_8_seconds` (first speech at 1 s, done at 12 s: `BrainDone`, no failure line spoken); `a_silent_brain_fails_at_8_seconds`; `a_reply_still_running_at_30_seconds_fails`. In `logfile.rs`: `writes_a_line_into_the_logs_folder`.
- [ ] **Step 2: Run** `cargo test -p jarvis-core runtime && cargo test -p jarvis-app logfile`. Expected: FAIL.
- [ ] **Step 3: Implement.** Change `tracing::info!(%text, "heard")` to `debug!`. The existing timeout test keeps its meaning under the first-event limit.
- [ ] **Step 4: Run** `cargo test --workspace`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(app): log file; replies may run to 30 s`.

### Task 8: The supervisor and the app wiring

**Files:** Create `app/src-tauri/src/voice_mode.rs`. Modify `app/src-tauri/src/lib.rs` (`VoiceSlot`, `TauriUi::emit`, `say_aloud`, `prepare_voice`, the ears' `speaking` closure, set-up, a `retry_voice` command), `app/src-tauri/src/server_mode.rs` (`TurnClock`), `app/src/main.ts`, `app/src-tauri/capabilities/*` if the command needs listing.

**Interfaces — Consumes:** `Speaks`, `process::spawn`, `Running`, `LinkEvent`, `install::ensure`, `VoiceConfig`. **Produces:**
- `type VoiceSlot = Arc<Mutex<Option<Arc<dyn Speaks>>>>`.
- `voice_mode::Supervisor::new(ops: Box<dyn VoiceOps>, set_status: Box<dyn Fn(Status) + Send + Sync>, slot: VoiceSlot)`; `trait VoiceOps { async fn start_helper(&self) -> Result<Arc<dyn Speaks>, String>; async fn start_builtin(&self) -> Result<Arc<dyn Speaks>, String>; }`; `Supervisor::start(engine: VoiceEngine)`, `Supervisor::helper_closed()`, `Supervisor::retry()`.
- `enum Status { HelperDownloading(u8), HelperStarting, Downloading(u8), Loading, Ready, HelperUnavailable, Unavailable }`, sent as `jarvis://voice_status` with `state` `helper_downloading`, `helper_starting`, `downloading`, `loading`, `ready`, `helper_unavailable`, `unavailable`.

Rules: with `Pipecat`, try the helper (install, spawn, `ready` within 60 s). On failure show `HelperUnavailable`, then start the built-in voice. When a running helper closes, clear the slot, restart it **once per app run**; a second close, or a failed restart, goes to the built-in voice for the rest of the run. If the built-in voice also fails, show `Unavailable`; `retry()` starts again from the top. While the slot is empty on Windows, speech is shown as a caption only. On app exit the helper is shut down. `TurnClock` logs `speak_to_sound_ms` from the first `say` of a turn to the helper's `speaking on`. The helper's stderr lines go to the log at `info`, prefixed `helper:`.

- [ ] **Step 1: Write the failing tests** (fake `VoiceOps` with scripted results)

```rust
#[tokio::test] async fn helper_ok_fills_the_slot_and_reports_ready() {}
#[tokio::test] async fn helper_failing_falls_back_to_builtin_and_says_so() { /* statuses end [.., HelperUnavailable]; slot holds builtin */ }
#[tokio::test] async fn a_helper_that_closes_is_restarted_once() {}
#[tokio::test] async fn a_second_close_goes_to_builtin_for_good() {}
#[tokio::test] async fn both_failing_reports_unavailable_and_retry_starts_over() {}
#[tokio::test] async fn builtin_engine_never_starts_the_helper() {}
#[cfg(windows)] #[test] fn no_voice_means_caption_only_never_the_windows_voice() {}
```

- [ ] **Step 2: Run** `cargo test -p jarvis-app voice_mode`. Expected: FAIL.
- [ ] **Step 3: Implement** the supervisor, then the wiring. In `main.ts` add the four status lines from Global Constraints and the "Try again" button (`invoke("retry_voice")`); the existing `jarvis://speak` listener stays for non-Windows builds.
- [ ] **Step 4: Run** `cargo test --workspace --no-fail-fast`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo fmt --all -- --check`, `cd app && npm run build && npx vitest run`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(app): voice supervisor, helper first, built-in fallback`.

### Task 9: Release, records and the live check

**Files:** Modify `crates/jarvis-helper/src/install.rs` (pin), `.10x/status.md`, `.10x/handoff.md`. Create `.10x/adrs/015-pipecat-voice-helper.md`, `.10x/reviews/2026-11-27-voice-slice-1.md`.

- [ ] **Step 1: Write the failing test** `the_helper_hash_is_pinned` in `install.rs`: `HELPER_SHA256` is 64 lower-case hex characters and not all zeros. Run it: FAIL.
- [ ] **Step 2:** With Manj's approval, push tag `voice-helper-v1`; `helper.yml` publishes the asset. Copy the SHA-256 from the job summary into `HELPER_SHA256`. Run the test: PASS. Commit `chore(helper): pin voice-helper-v1`.
- [ ] **Step 3:** Write ADR-015 (Python helper on the desktop; replaces the "all listening through sherpa-onnx" part of ADR-005 from slice 2; ADR-004 holds) and update `status.md` and `handoff.md`. Commit.
- [ ] **Step 4:** Whole-branch independent review; apply fixes; record it in `.10x/reviews/`.
- [ ] **Step 5:** With Manj's approval: open the PR, merge, and let `release.yml` build the installer.
- [ ] **Step 6: Live check on Manj's PC**, fictional phrases only:
  1. First start shows "Getting my voice helper ready", then "Starting my voice helper…", then nothing.
  2. A six-sentence reply: no gap longer than a natural pause, no clicks, not the Windows voice.
  3. A reply longer than 8 seconds is spoken to the end.
  4. Typed "stop" mid-reply: silent at once; the next reply plays.
  5. End `jarvis-voice-helper.exe` in Task Manager: it comes back once and George still speaks. End it again: "Voice helper unavailable. Using my built-in voice." and George still speaks.
  6. `voice_engine = "builtin"` in `voice.toml`, restart: built-in George, no helper in Task Manager.
  7. Wake word, a spoken approval and the news wall behave as before.
  8. `logs/jarvis.log` exists, shows `speak_to_sound_ms`, and contains none of the phrases spoken.

## Not in this slice
Mic, faster-whisper, Smart Turn, talk-over, dip and recover, the wake-word hand-off (slice 2); echo cancellation, never sleeping, "go to sleep" as standby, the one-word rule (slice 3); `first_chunk_max_words` (added only if `speak_to_sound_ms` shows a slow first word).
