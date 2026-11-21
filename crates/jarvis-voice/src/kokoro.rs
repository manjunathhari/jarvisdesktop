//! Kokoro neural voice via sherpa-onnx (Windows build).

use crate::model::KokoroFiles;
use crate::{Audio, SpeechEngine};
use sherpa_onnx::{
    GenerationConfig, OfflineTts, OfflineTtsConfig, OfflineTtsKokoroModelConfig,
    OfflineTtsModelConfig,
};

pub struct Kokoro {
    tts: OfflineTts,
    speaker: i32,
}

fn s(p: &std::path::Path) -> Option<String> {
    Some(p.to_string_lossy().into_owned())
}

impl Kokoro {
    pub fn load(files: &KokoroFiles, speaker: i32) -> Result<Self, String> {
        let threads = std::thread::available_parallelism()
            .map(|n| n.get().clamp(1, 4))
            .unwrap_or(2) as i32;
        let config = OfflineTtsConfig {
            model: OfflineTtsModelConfig {
                kokoro: OfflineTtsKokoroModelConfig {
                    model: s(&files.model),
                    voices: s(&files.voices),
                    tokens: s(&files.tokens),
                    data_dir: s(&files.data_dir),
                    ..Default::default()
                },
                num_threads: threads,
                provider: Some("cpu".into()),
                debug: false,
                ..Default::default()
            },
            max_num_sentences: 1,
            ..Default::default()
        };
        let tts = OfflineTts::create(&config).ok_or("couldn't load the Kokoro voice model")?;
        Ok(Self { tts, speaker })
    }
}

impl SpeechEngine for Kokoro {
    fn synth(&mut self, text: &str) -> Result<Audio, String> {
        let gen = GenerationConfig {
            sid: self.speaker,
            speed: 1.0,
            ..Default::default()
        };
        let audio = self
            .tts
            .generate_with_config(text, &gen, None::<fn(&[f32], f32) -> bool>)
            .ok_or("speech generation failed")?;
        Ok(Audio {
            samples: audio.samples().to_vec(),
            sample_rate: audio.sample_rate() as u32,
        })
    }
}
