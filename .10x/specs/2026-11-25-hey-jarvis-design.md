# "Hey Jarvis" wake word — short design (25 Nov 2026)

**Intent.** Manj wants JARVIS to listen without a tap: saying "Hey Jarvis" from idle does what tapping the orb does (Manj, 25 Nov: "can't it be listening").

**Decided by Manj (25 Nov).**
- The mic stays on while JARVIS is idle. Sound is checked on the PC only, and nothing leaves it until the phrase is heard.
- Approach **A**: the keyword spotter in sherpa-onnx (already used for Whisper and Silero), with its small English streaming model and the phrase given as text.
- If it misses too often on his PC, switch to approach B: openWakeWord's purpose-trained "hey jarvis" model, with its feature pipeline rebuilt in Rust.

**Approaches considered.**
- **A. sherpa-onnx keyword spotter (chosen).**
  - Uses `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`: the int8 encoder, decoder and joiner, about 5 MB in use. It is downloaded once as the official archive from GitHub releases, unpacked like the Kokoro voice.
  - The phrase is given as its BPE tokens: `▁HE Y ▁JA R VI S @HEY_JARVIS`.
  - Least new code, and no new library.
  - Accuracy is unproven for this phrase: it may miss, or wake falsely.
- **B. openWakeWord "hey jarvis".** More accurate for this exact phrase, but it needs the mel-spectrogram and embedding models run by hand in Rust, plus a new ONNX runtime crate. About twice the work.
- **C. Porcupine.** Dropped: it needs a licence key and an account.

**Behaviour.**
- JARVIS listens for the phrase only while it is **idle**. It does not while listening, in the follow-up window (it is already listening then), or thinking.
- It does not listen while **mic off**: "stop listening" or "go to sleep" turn the wake word off too, until the orb is tapped.
- It ignores the mic while George speaks, and for the 300 ms echo tail after, so he can't wake himself.
- On the phrase:
  - bring the JARVIS window to the front (show, restore, focus);
  - send `Event::Wake`, which the coordinator already handles like a tap: greeting, then listening.
- **Failure:** if the model can't be downloaded or loaded, the wake word is unavailable and everything else works as now (tap or type). It is logged, and the mic is not kept on when idle.

**Changes.**
- `jarvis-voice::model`: a general `ensure_archive` (download, unpack, check the required files, mark complete). Kokoro's `ensure` uses it.
- `jarvis-ears`:
  - a `WakeDetector` trait and `Ears::set_wake(on)`; idle audio goes to the wake detector when the wake word is on;
  - the model constants;
  - `sherpa::KeywordWake` (Windows).
- `app`:
  - load the spotter alongside the listening models;
  - mic on while listening, in the follow-up window, or idle with a working wake word;
  - the wake word is armed only when idle;
  - on wake: bring the window to the front, then `Event::Wake`.

**Left to the builder.** The spotter thresholds. Start at sherpa-onnx's defaults (score 1.0, threshold 0.25), named as constants for tuning on Manj's PC.
