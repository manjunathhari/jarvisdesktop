//! Silero VAD, Whisper and the "Hey Jarvis" keyword spotter via sherpa-onnx (Windows build).

use crate::models::{KWS_DECODER, KWS_ENCODER, KWS_JOINER, KWS_TOKENS, WAKE_KEYWORDS};
use crate::{SpeechDetector, Transcriber, WakeDetector, SAMPLE_RATE};
use sherpa_onnx::{
    KeywordSpotter, KeywordSpotterConfig, OfflineModelConfig, OfflineRecognizer,
    OfflineRecognizerConfig, OfflineWhisperModelConfig, OnlineModelConfig, OnlineStream,
    OnlineTransducerModelConfig, SileroVadModelConfig, VadModelConfig, VoiceActivityDetector,
};
use std::path::Path;

const WINDOW: usize = 512;

fn s(p: &Path) -> Option<String> {
    Some(p.to_string_lossy().into_owned())
}

pub struct SileroVad {
    vad: VoiceActivityDetector,
    pending: Vec<f32>,
}

impl SileroVad {
    pub fn load(model: &Path) -> Result<Self, String> {
        let config = VadModelConfig {
            silero_vad: SileroVadModelConfig {
                model: s(model),
                threshold: 0.5,
                min_silence_duration: 0.45,
                min_speech_duration: 0.25,
                window_size: WINDOW as i32,
                max_speech_duration: 15.0,
            },
            sample_rate: SAMPLE_RATE as i32,
            num_threads: 1,
            provider: Some("cpu".into()),
            debug: false,
            ..Default::default()
        };
        let vad = VoiceActivityDetector::create(&config, 30.0)
            .ok_or("couldn't load the speech detector")?;
        Ok(Self {
            vad,
            pending: Vec::new(),
        })
    }
}

impl SpeechDetector for SileroVad {
    fn accept(&mut self, samples: &[f32]) {
        self.pending.extend_from_slice(samples);
        while self.pending.len() >= WINDOW {
            self.vad.accept_waveform(&self.pending[..WINDOW]);
            self.pending.drain(..WINDOW);
        }
    }

    fn pop_segment(&mut self) -> Option<Vec<f32>> {
        if self.vad.is_empty() {
            return None;
        }
        let samples = self.vad.front().map(|seg| seg.samples().to_vec());
        self.vad.pop();
        samples
    }

    fn reset(&mut self) {
        self.vad.clear();
        self.vad.reset();
        self.pending.clear();
    }
}

pub struct Whisper {
    recognizer: OfflineRecognizer,
}

impl Whisper {
    /// `dir` holds the Whisper small.en int8 files (see `crate::models`).
    pub fn load(dir: &Path) -> Result<Self, String> {
        let threads = std::thread::available_parallelism()
            .map(|n| n.get().clamp(1, 4))
            .unwrap_or(2) as i32;
        let config = OfflineRecognizerConfig {
            model_config: OfflineModelConfig {
                whisper: OfflineWhisperModelConfig {
                    encoder: s(&dir.join(crate::models::WHISPER_ENCODER)),
                    decoder: s(&dir.join(crate::models::WHISPER_DECODER)),
                    language: Some("en".into()),
                    task: Some("transcribe".into()),
                    tail_paddings: -1,
                    ..Default::default()
                },
                tokens: s(&dir.join(crate::models::WHISPER_TOKENS)),
                num_threads: threads,
                provider: Some("cpu".into()),
                debug: false,
                ..Default::default()
            },
            ..Default::default()
        };
        let recognizer = OfflineRecognizer::create(&config).ok_or("couldn't load Whisper")?;
        Ok(Self { recognizer })
    }
}

impl Transcriber for Whisper {
    fn transcribe(&mut self, samples: &[f32]) -> Result<String, String> {
        let stream = self.recognizer.create_stream();
        stream.accept_waveform(SAMPLE_RATE as i32, samples);
        self.recognizer.decode(&stream);
        stream
            .get_result()
            .map(|r| r.text)
            .ok_or_else(|| "no transcription".into())
    }
}

/// How sure the spotter must be before it wakes. Tuned 25 Nov on synthetic speech (two voices,
/// two speeds, quiet and noisy) after "Hey Jarvis" proved weak at the defaults (0.25 / 1.0):
/// raise the threshold for fewer false wakes, lower it for fewer misses.
pub const WAKE_THRESHOLD: f32 = 0.15;
pub const WAKE_SCORE: f32 = 1.5;

/// "Hey Jarvis" via sherpa-onnx's streaming keyword spotter (design 2026-11-25, approach A).
pub struct KeywordWake {
    // Dropped before the spotter that created it (fields drop in order).
    stream: OnlineStream,
    kws: KeywordSpotter,
}

impl KeywordWake {
    /// `dir` holds the spotter's model files (`models::KWS_FILES`).
    pub fn load(dir: &Path) -> Result<Self, String> {
        let config = KeywordSpotterConfig {
            model_config: OnlineModelConfig {
                transducer: OnlineTransducerModelConfig {
                    encoder: s(&dir.join(KWS_ENCODER)),
                    decoder: s(&dir.join(KWS_DECODER)),
                    joiner: s(&dir.join(KWS_JOINER)),
                },
                tokens: s(&dir.join(KWS_TOKENS)),
                num_threads: 1,
                provider: Some("cpu".into()),
                ..Default::default()
            },
            keywords_threshold: WAKE_THRESHOLD,
            keywords_score: WAKE_SCORE,
            keywords_buf: Some(format!("{WAKE_KEYWORDS}\n")),
            ..Default::default()
        };
        let kws = KeywordSpotter::create(&config).ok_or("couldn't load the wake-word spotter")?;
        let stream = kws.create_stream();
        Ok(Self { stream, kws })
    }
}

impl WakeDetector for KeywordWake {
    fn accept(&mut self, samples: &[f32]) -> bool {
        self.stream.accept_waveform(SAMPLE_RATE as i32, samples);
        while self.kws.is_ready(&self.stream) {
            self.kws.decode(&self.stream);
            if let Some(r) = self.kws.get_result(&self.stream) {
                if !r.keyword.is_empty() {
                    tracing::info!(keyword = %r.keyword, "wake word heard");
                    self.kws.reset(&self.stream);
                    return true;
                }
            }
        }
        false
    }

    fn reset(&mut self) {
        self.kws.reset(&self.stream);
    }
}
