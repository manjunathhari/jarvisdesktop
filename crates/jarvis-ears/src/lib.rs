//! JARVIS's ears (ADR-005): microphone → 16 kHz mono → gate (never listen to
//! George) → voice-activity detection → Whisper → text.
//!
//! The detector and transcriber are traits so the gating and segment logic is
//! tested without a microphone or model files.

pub mod capture;
pub mod resample;

/// Where the listening models come from (fetched once, individually).
pub mod models {
    pub const WHISPER_DIR: &str = "whisper-small.en";
    pub const WHISPER_ENCODER: &str = "small.en-encoder.int8.onnx";
    pub const WHISPER_DECODER: &str = "small.en-decoder.int8.onnx";
    pub const WHISPER_TOKENS: &str = "small.en-tokens.txt";
    pub const WHISPER_ENCODER_URL: &str =
        "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-small.en/resolve/main/small.en-encoder.int8.onnx";
    pub const WHISPER_DECODER_URL: &str =
        "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-small.en/resolve/main/small.en-decoder.int8.onnx";
    pub const WHISPER_TOKENS_URL: &str =
        "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-small.en/resolve/main/small.en-tokens.txt";
    pub const VAD_DIR: &str = "silero-vad";
    pub const VAD_URL: &str =
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx";
    pub const VAD_FILE: &str = "silero_vad.onnx";
}

#[cfg(windows)]
pub mod sherpa;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};

pub const SAMPLE_RATE: u32 = 16_000;
/// Keep ignoring the mic this long after George stops (room echo).
pub const SPEAKING_TAIL: Duration = Duration::from_millis(300);
/// Shorter segments are coughs and clicks, not commands.
pub const MIN_SEGMENT: usize = (SAMPLE_RATE as usize) * 3 / 10;

/// Finds where speech starts and stops.
pub trait SpeechDetector: Send + 'static {
    fn accept(&mut self, samples: &[f32]);
    /// A finished stretch of speech, if one is ready.
    fn pop_segment(&mut self) -> Option<Vec<f32>>;
    fn reset(&mut self);
}

/// Turns 16 kHz mono speech into text.
pub trait Transcriber: Send + 'static {
    fn transcribe(&mut self, samples: &[f32]) -> Result<String, String>;
}

/// Whisper's placeholders for non-speech; never treated as a command.
pub fn is_noise(text: &str) -> bool {
    let t = text.trim();
    t.is_empty()
        || (t.starts_with('[') && t.ends_with(']'))
        || (t.starts_with('(') && t.ends_with(')'))
        || !t.chars().any(char::is_alphanumeric)
}

enum Msg {
    Audio(Vec<f32>),
    Listen(bool),
}

/// Handle to the ears thread. Feed it 16 kHz mono audio; it calls `on_text`.
#[derive(Clone)]
pub struct Ears {
    tx: mpsc::Sender<Msg>,
    listening: Arc<AtomicBool>,
}

impl Ears {
    /// `speaking` reports whether JARVIS's own voice is playing right now.
    pub fn start(
        detector: Box<dyn SpeechDetector>,
        transcriber: Box<dyn Transcriber>,
        speaking: Arc<dyn Fn() -> bool + Send + Sync>,
        on_text: Box<dyn FnMut(String) + Send>,
    ) -> Self {
        let (tx, rx) = mpsc::channel::<Msg>();
        let listening = Arc::new(AtomicBool::new(false));
        let flag = listening.clone();
        let (mut detector, mut transcriber, mut on_text) = (detector, transcriber, on_text);
        thread::Builder::new()
            .name("jarvis-ears".into())
            .spawn(move || {
                let mut on = false;
                let mut last_spoke: Option<Instant> = None;
                for msg in rx {
                    match msg {
                        Msg::Listen(v) => {
                            on = v;
                            flag.store(v, Ordering::SeqCst);
                            if !v {
                                detector.reset();
                            }
                        }
                        Msg::Audio(samples) => {
                            if !on {
                                continue;
                            }
                            if speaking() {
                                last_spoke = Some(Instant::now());
                                detector.reset();
                                continue;
                            }
                            if last_spoke.is_some_and(|t| t.elapsed() < SPEAKING_TAIL) {
                                continue;
                            }
                            detector.accept(&samples);
                            while let Some(segment) = detector.pop_segment() {
                                if segment.len() < MIN_SEGMENT {
                                    continue;
                                }
                                match transcriber.transcribe(&segment) {
                                    Ok(text) if !is_noise(&text) => {
                                        on_text(text.trim().to_string())
                                    }
                                    Ok(_) => {}
                                    Err(e) => tracing::warn!(error = %e, "transcription failed"),
                                }
                            }
                        }
                    }
                }
            })
            .expect("spawn ears thread");
        Self { tx, listening }
    }

    pub fn push_audio(&self, samples: Vec<f32>) {
        let _ = self.tx.send(Msg::Audio(samples));
    }

    pub fn set_listening(&self, on: bool) {
        self.listening.store(on, Ordering::SeqCst);
        let _ = self.tx.send(Msg::Listen(on));
    }

    pub fn is_listening(&self) -> bool {
        self.listening.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// Treats any chunk whose first sample is > 0.5 as speech; a chunk of
    /// silence after speech ends the segment.
    #[derive(Default)]
    struct FakeVad {
        current: Vec<f32>,
        ready: Vec<Vec<f32>>,
        resets: Arc<Mutex<usize>>,
    }
    impl SpeechDetector for FakeVad {
        fn accept(&mut self, samples: &[f32]) {
            if samples.first().copied().unwrap_or(0.0) > 0.5 {
                self.current.extend_from_slice(samples);
            } else if !self.current.is_empty() {
                self.ready.push(std::mem::take(&mut self.current));
            }
        }
        fn pop_segment(&mut self) -> Option<Vec<f32>> {
            if self.ready.is_empty() {
                None
            } else {
                Some(self.ready.remove(0))
            }
        }
        fn reset(&mut self) {
            self.current.clear();
            self.ready.clear();
            *self.resets.lock().unwrap() += 1;
        }
    }

    /// "Transcribes" a segment as its length in samples.
    struct FakeWhisper {
        reply: Option<&'static str>,
    }
    impl Transcriber for FakeWhisper {
        fn transcribe(&mut self, samples: &[f32]) -> Result<String, String> {
            Ok(self
                .reply
                .map(str::to_string)
                .unwrap_or_else(|| format!("{} samples", samples.len())))
        }
    }

    struct Rig {
        ears: Ears,
        heard: Arc<Mutex<Vec<String>>>,
        speaking: Arc<AtomicBool>,
        resets: Arc<Mutex<usize>>,
    }

    fn rig(reply: Option<&'static str>) -> Rig {
        let heard = Arc::new(Mutex::new(vec![]));
        let speaking = Arc::new(AtomicBool::new(false));
        let resets = Arc::new(Mutex::new(0));
        let (h, s) = (heard.clone(), speaking.clone());
        let ears = Ears::start(
            Box::new(FakeVad {
                resets: resets.clone(),
                ..Default::default()
            }),
            Box::new(FakeWhisper { reply }),
            Arc::new(move || s.load(Ordering::SeqCst)),
            Box::new(move |t| h.lock().unwrap().push(t)),
        );
        Rig {
            ears,
            heard,
            speaking,
            resets,
        }
    }

    const SPEECH: f32 = 0.9;
    fn speech(ms: usize) -> Vec<f32> {
        vec![SPEECH; SAMPLE_RATE as usize * ms / 1000]
    }
    fn silence() -> Vec<f32> {
        vec![0.0; 1600]
    }

    fn settle() {
        thread::sleep(Duration::from_millis(80));
    }

    #[test]
    fn speech_while_listening_becomes_text() {
        let r = rig(None);
        r.ears.set_listening(true);
        r.ears.push_audio(speech(1000));
        r.ears.push_audio(silence());
        settle();
        assert_eq!(*r.heard.lock().unwrap(), vec!["16000 samples".to_string()]);
        assert!(r.ears.is_listening());
    }

    #[test]
    fn nothing_is_heard_when_not_listening() {
        let r = rig(None);
        r.ears.push_audio(speech(1000));
        r.ears.push_audio(silence());
        settle();
        assert!(r.heard.lock().unwrap().is_empty());
    }

    #[test]
    fn jarvis_never_hears_its_own_voice() {
        let r = rig(None);
        r.ears.set_listening(true);
        r.speaking.store(true, Ordering::SeqCst);
        r.ears.push_audio(speech(1000));
        r.ears.push_audio(silence());
        settle();
        assert!(r.heard.lock().unwrap().is_empty());
    }

    #[test]
    fn the_echo_tail_after_jarvis_stops_is_ignored_too() {
        let r = rig(None);
        r.ears.set_listening(true);
        r.speaking.store(true, Ordering::SeqCst);
        r.ears.push_audio(silence());
        settle();
        r.speaking.store(false, Ordering::SeqCst);
        r.ears.push_audio(speech(500)); // within the tail
        r.ears.push_audio(silence());
        settle();
        assert!(
            r.heard.lock().unwrap().is_empty(),
            "{:?}",
            r.heard.lock().unwrap()
        );
        thread::sleep(SPEAKING_TAIL);
        r.ears.push_audio(speech(500));
        r.ears.push_audio(silence());
        settle();
        assert_eq!(r.heard.lock().unwrap().len(), 1);
    }

    #[test]
    fn coughs_and_clicks_are_too_short_to_count() {
        let r = rig(None);
        r.ears.set_listening(true);
        r.ears.push_audio(speech(100));
        r.ears.push_audio(silence());
        settle();
        assert!(r.heard.lock().unwrap().is_empty());
    }

    #[test]
    fn whisper_noise_placeholders_are_dropped() {
        let r = rig(Some("[BLANK_AUDIO]"));
        r.ears.set_listening(true);
        r.ears.push_audio(speech(800));
        r.ears.push_audio(silence());
        settle();
        assert!(r.heard.lock().unwrap().is_empty());
    }

    #[test]
    fn turning_listening_off_discards_half_heard_speech() {
        let r = rig(None);
        r.ears.set_listening(true);
        r.ears.push_audio(speech(800));
        r.ears.set_listening(false);
        r.ears.push_audio(silence());
        settle();
        assert!(r.heard.lock().unwrap().is_empty());
        assert!(*r.resets.lock().unwrap() >= 1);
        assert!(!r.ears.is_listening());
    }

    #[test]
    fn noise_placeholders() {
        for t in [
            "",
            "  ",
            "[BLANK_AUDIO]",
            "(music)",
            "[ Silence ]",
            "(inaudible)",
            "...",
            "♪",
        ] {
            assert!(is_noise(t), "{t:?}");
        }
        for t in ["bring me the news", "Open number 4.", "stop"] {
            assert!(!is_noise(t), "{t:?}");
        }
    }
}
