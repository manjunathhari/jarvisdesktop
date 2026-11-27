//! `voice.toml` in the app's data folder: the voice's tuning knobs, read once at start so the
//! owner can change them without a new build. A missing file means the defaults. A bad value
//! never stops the app: that one key keeps its default and `load` returns a warning for it.
//!
//! Only `voice_engine` and `tts_speed` take effect in this slice; the rest are read now so the
//! file does not change shape in later slices.
//!
//! Every range is inclusive at both ends. Float keys also accept a whole number (`tts_speed = 1`);
//! integer keys reject a decimal or negative number.
//!
//! | Key | Default | Accepts |
//! |---|---|---|
//! | `voice_engine` | `"pipecat"` | `"pipecat"`, `"builtin"` |
//! | `vad_threshold` | 0.5 | 0.1 to 0.9 |
//! | `vad_min_speech_ms` | 250 | 50 to 2000 |
//! | `vad_pre_roll_ms` | 300 | 0 to 1000 |
//! | `smart_turn` | true | true or false |
//! | `end_of_turn_silence_ms` | 700 | 200 to 3000 |
//! | `max_turn_s` | 30 | 5 to 120 |
//! | `duck_on_speech` | true | true or false |
//! | `duck_volume` | 0.3 | 0.0 to 1.0 |
//! | `duck_fade_ms` | 80 | 0 to 1000 |
//! | `barge_in_after_ms` | 300 | 100 to 2000 |
//! | `stop_fade_ms` | 60 | 0 to 1000 |
//! | `resume_if_false_alarm` | true | true or false |
//! | `resume_fade_ms` | 250 | 0 to 2000 |
//! | `tts_speed` | 1.0 | 0.5 to 2.0 |
//! | `tts_chunk` | `"sentence"` | `"sentence"` |
//! | `first_chunk_max_words` | 12 | 0 to 50 |
//! | `sentence_gap_ms` | 0 | 0 to 1000 |
//! | `echo_cancellation` | true | true or false |
//! | `mic_open_while_speaking` | true | true or false |
//! | `follow_up_window_s` | 0 | 0 to 3600 |
//! | `sleep_phrase` | `"go to sleep"` | 1 to 40 characters, at least one letter or digit |
//! | `ignore_noise_min_words` | 2 | 1 to 5 |

use std::path::Path;

pub const FILE: &str = "voice.toml";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceEngine {
    Pipecat,
    Builtin,
}

#[derive(Debug, Clone, PartialEq)]
pub struct VoiceConfig {
    pub voice_engine: VoiceEngine,
    pub vad_threshold: f32,
    pub vad_min_speech_ms: u32,
    pub vad_pre_roll_ms: u32,
    pub smart_turn: bool,
    pub end_of_turn_silence_ms: u32,
    pub max_turn_s: u32,
    pub duck_on_speech: bool,
    pub duck_volume: f32,
    pub duck_fade_ms: u32,
    pub barge_in_after_ms: u32,
    pub stop_fade_ms: u32,
    pub resume_if_false_alarm: bool,
    pub resume_fade_ms: u32,
    pub tts_speed: f32,
    pub tts_chunk: String,
    pub first_chunk_max_words: u32,
    pub sentence_gap_ms: u32,
    pub echo_cancellation: bool,
    pub mic_open_while_speaking: bool,
    pub follow_up_window_s: u32,
    pub sleep_phrase: String,
    pub ignore_noise_min_words: u32,
}

impl Default for VoiceConfig {
    fn default() -> Self {
        Self {
            voice_engine: VoiceEngine::Pipecat,
            vad_threshold: 0.5,
            vad_min_speech_ms: 250,
            vad_pre_roll_ms: 300,
            smart_turn: true,
            end_of_turn_silence_ms: 700,
            max_turn_s: 30,
            duck_on_speech: true,
            duck_volume: 0.3,
            duck_fade_ms: 80,
            barge_in_after_ms: 300,
            stop_fade_ms: 60,
            resume_if_false_alarm: true,
            resume_fade_ms: 250,
            tts_speed: 1.0,
            tts_chunk: "sentence".to_string(),
            first_chunk_max_words: 12,
            sentence_gap_ms: 0,
            echo_cancellation: true,
            mic_open_while_speaking: true,
            follow_up_window_s: 0,
            sleep_phrase: "go to sleep".to_string(),
            ignore_noise_min_words: 2,
        }
    }
}

/// How one key is checked, and where its value goes. The accessor returns the field, so the same
/// entry sets the value and (on a default config) reads the default for the warning.
enum Kind {
    Float(f64, f64, fn(&mut VoiceConfig) -> &mut f32),
    Int(u32, u32, fn(&mut VoiceConfig) -> &mut u32),
    Bool(fn(&mut VoiceConfig) -> &mut bool),
    Engine(fn(&mut VoiceConfig) -> &mut VoiceEngine),
    Chunk(fn(&mut VoiceConfig) -> &mut String),
    Phrase(fn(&mut VoiceConfig) -> &mut String),
}

const PHRASE_MAX: usize = 40;

const KEYS: [(&str, Kind); 23] = [
    ("voice_engine", Kind::Engine(|c| &mut c.voice_engine)),
    (
        "vad_threshold",
        Kind::Float(0.1, 0.9, |c| &mut c.vad_threshold),
    ),
    (
        "vad_min_speech_ms",
        Kind::Int(50, 2000, |c| &mut c.vad_min_speech_ms),
    ),
    (
        "vad_pre_roll_ms",
        Kind::Int(0, 1000, |c| &mut c.vad_pre_roll_ms),
    ),
    ("smart_turn", Kind::Bool(|c| &mut c.smart_turn)),
    (
        "end_of_turn_silence_ms",
        Kind::Int(200, 3000, |c| &mut c.end_of_turn_silence_ms),
    ),
    ("max_turn_s", Kind::Int(5, 120, |c| &mut c.max_turn_s)),
    ("duck_on_speech", Kind::Bool(|c| &mut c.duck_on_speech)),
    ("duck_volume", Kind::Float(0.0, 1.0, |c| &mut c.duck_volume)),
    ("duck_fade_ms", Kind::Int(0, 1000, |c| &mut c.duck_fade_ms)),
    (
        "barge_in_after_ms",
        Kind::Int(100, 2000, |c| &mut c.barge_in_after_ms),
    ),
    ("stop_fade_ms", Kind::Int(0, 1000, |c| &mut c.stop_fade_ms)),
    (
        "resume_if_false_alarm",
        Kind::Bool(|c| &mut c.resume_if_false_alarm),
    ),
    (
        "resume_fade_ms",
        Kind::Int(0, 2000, |c| &mut c.resume_fade_ms),
    ),
    ("tts_speed", Kind::Float(0.5, 2.0, |c| &mut c.tts_speed)),
    ("tts_chunk", Kind::Chunk(|c| &mut c.tts_chunk)),
    (
        "first_chunk_max_words",
        Kind::Int(0, 50, |c| &mut c.first_chunk_max_words),
    ),
    (
        "sentence_gap_ms",
        Kind::Int(0, 1000, |c| &mut c.sentence_gap_ms),
    ),
    (
        "echo_cancellation",
        Kind::Bool(|c| &mut c.echo_cancellation),
    ),
    (
        "mic_open_while_speaking",
        Kind::Bool(|c| &mut c.mic_open_while_speaking),
    ),
    (
        "follow_up_window_s",
        Kind::Int(0, 3600, |c| &mut c.follow_up_window_s),
    ),
    ("sleep_phrase", Kind::Phrase(|c| &mut c.sleep_phrase)),
    (
        "ignore_noise_min_words",
        Kind::Int(1, 5, |c| &mut c.ignore_noise_min_words),
    ),
];

/// Check one value and store it, or return what is accepted and what the default is.
fn apply(cfg: &mut VoiceConfig, kind: &Kind, value: &toml::Value) -> Result<(), String> {
    use toml::Value;
    let mut default = VoiceConfig::default();
    match kind {
        Kind::Float(min, max, field) => {
            let v = match value {
                Value::Float(f) => Some(*f),
                Value::Integer(i) => Some(*i as f64),
                _ => None,
            };
            match v {
                Some(v) if v.is_finite() && (*min..=*max).contains(&v) => {
                    *field(cfg) = v as f32;
                    Ok(())
                }
                _ => Err(format!(
                    "must be between {min:?} and {max:?}; using {:?}",
                    *field(&mut default)
                )),
            }
        }
        Kind::Int(min, max, field) => match value {
            Value::Integer(i) if (i64::from(*min)..=i64::from(*max)).contains(i) => {
                *field(cfg) = *i as u32;
                Ok(())
            }
            _ => Err(format!(
                "must be a whole number between {min} and {max}; using {}",
                *field(&mut default)
            )),
        },
        Kind::Bool(field) => match value {
            Value::Boolean(b) => {
                *field(cfg) = *b;
                Ok(())
            }
            _ => Err(format!(
                "must be true or false; using {}",
                *field(&mut default)
            )),
        },
        Kind::Engine(field) => match value.as_str() {
            Some("pipecat") => {
                *field(cfg) = VoiceEngine::Pipecat;
                Ok(())
            }
            Some("builtin") => {
                *field(cfg) = VoiceEngine::Builtin;
                Ok(())
            }
            _ => Err(format!(
                "must be \"pipecat\" or \"builtin\"; using {:?}",
                match *field(&mut default) {
                    VoiceEngine::Pipecat => "pipecat",
                    VoiceEngine::Builtin => "builtin",
                }
            )),
        },
        Kind::Chunk(field) => match value.as_str() {
            Some("sentence") => {
                *field(cfg) = "sentence".to_string();
                Ok(())
            }
            _ => Err(format!(
                "must be \"sentence\"; using {:?}",
                field(&mut default)
            )),
        },
        Kind::Phrase(field) => match value.as_str().map(str::trim) {
            Some(s)
                if (1..=PHRASE_MAX).contains(&s.chars().count())
                    && s.chars().any(char::is_alphanumeric) =>
            {
                *field(cfg) = s.to_string();
                Ok(())
            }
            _ => Err(format!(
                "must be 1 to {PHRASE_MAX} characters with at least one letter or digit; using {:?}",
                field(&mut default)
            )),
        },
    }
}

impl VoiceConfig {
    /// `dir/voice.toml`, and what was wrong with it. A missing file is the defaults with no
    /// warning. A file that cannot be read or is not TOML is all defaults with one warning. Any
    /// other problem is one warning for that key, which keeps its default.
    pub fn load(dir: &Path) -> (VoiceConfig, Vec<String>) {
        let text = match std::fs::read_to_string(dir.join(FILE)) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return (VoiceConfig::default(), Vec::new())
            }
            Err(e) => {
                return (
                    VoiceConfig::default(),
                    vec![format!(
                        "voice.toml: couldn't read it ({e}); using the defaults"
                    )],
                )
            }
        };
        let table: toml::Table = match text.parse() {
            Ok(t) => t,
            Err(e) => {
                let reason = e.message();
                return (
                    VoiceConfig::default(),
                    vec![format!(
                        "voice.toml: not valid TOML ({reason}); using the defaults"
                    )],
                );
            }
        };

        let mut cfg = VoiceConfig::default();
        let mut warnings = Vec::new();
        for (name, kind) in &KEYS {
            if let Some(value) = table.get(*name) {
                if let Err(why) = apply(&mut cfg, kind, value) {
                    warnings.push(format!("voice.toml: {name} {why}"));
                }
            }
        }
        for key in table.keys() {
            if !KEYS.iter().any(|(name, _)| name == key) {
                warnings.push(format!("voice.toml: unknown key `{key}` ignored"));
            }
        }
        (cfg, warnings)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load_text(text: &str) -> (VoiceConfig, Vec<String>) {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(FILE), text).unwrap();
        VoiceConfig::load(dir.path())
    }

    /// One warning, naming `key`, starting `voice.toml:`; everything else default.
    fn assert_one_warning_for(text: &str, key: &str) {
        let (cfg, warnings) = load_text(text);
        assert_eq!(warnings.len(), 1, "{text:?} -> {warnings:?}");
        assert!(warnings[0].starts_with("voice.toml:"), "{warnings:?}");
        assert!(warnings[0].contains(key), "{warnings:?}");
        assert_eq!(cfg, VoiceConfig::default(), "{text:?}");
    }

    #[test]
    fn a_missing_file_gives_the_defaults_and_no_warning() {
        let dir = tempfile::tempdir().unwrap();
        let (cfg, warnings) = VoiceConfig::load(dir.path());
        assert_eq!(cfg, VoiceConfig::default());
        assert!(warnings.is_empty());
    }

    #[test]
    fn the_defaults_match_the_design() {
        let d = VoiceConfig::default();
        assert_eq!(d.voice_engine, VoiceEngine::Pipecat);
        assert_eq!(d.vad_threshold, 0.5);
        assert_eq!(d.vad_min_speech_ms, 250);
        assert_eq!(d.vad_pre_roll_ms, 300);
        assert!(d.smart_turn);
        assert_eq!(d.end_of_turn_silence_ms, 700);
        assert_eq!(d.max_turn_s, 30);
        assert!(d.duck_on_speech);
        assert_eq!(d.duck_volume, 0.3);
        assert_eq!(d.duck_fade_ms, 80);
        assert_eq!(d.barge_in_after_ms, 300);
        assert_eq!(d.stop_fade_ms, 60);
        assert!(d.resume_if_false_alarm);
        assert_eq!(d.resume_fade_ms, 250);
        assert_eq!(d.tts_speed, 1.0);
        assert_eq!(d.tts_chunk, "sentence");
        assert_eq!(d.first_chunk_max_words, 12);
        assert_eq!(d.sentence_gap_ms, 0);
        assert!(d.echo_cancellation);
        assert!(d.mic_open_while_speaking);
        assert_eq!(d.follow_up_window_s, 0);
        assert_eq!(d.sleep_phrase, "go to sleep");
        assert_eq!(d.ignore_noise_min_words, 2);
    }

    #[test]
    fn every_key_can_be_set() {
        let (cfg, warnings) = load_text(
            r#"
voice_engine = "builtin"
vad_threshold = 0.6
vad_min_speech_ms = 300
vad_pre_roll_ms = 400
smart_turn = false
end_of_turn_silence_ms = 800
max_turn_s = 40
duck_on_speech = false
duck_volume = 0.5
duck_fade_ms = 100
barge_in_after_ms = 400
stop_fade_ms = 70
resume_if_false_alarm = false
resume_fade_ms = 300
tts_speed = 1.25
tts_chunk = "sentence"
first_chunk_max_words = 10
sentence_gap_ms = 100
echo_cancellation = false
mic_open_while_speaking = false
follow_up_window_s = 15
sleep_phrase = "stand down"
ignore_noise_min_words = 3
"#,
        );
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(
            cfg,
            VoiceConfig {
                voice_engine: VoiceEngine::Builtin,
                vad_threshold: 0.6,
                vad_min_speech_ms: 300,
                vad_pre_roll_ms: 400,
                smart_turn: false,
                end_of_turn_silence_ms: 800,
                max_turn_s: 40,
                duck_on_speech: false,
                duck_volume: 0.5,
                duck_fade_ms: 100,
                barge_in_after_ms: 400,
                stop_fade_ms: 70,
                resume_if_false_alarm: false,
                resume_fade_ms: 300,
                tts_speed: 1.25,
                tts_chunk: "sentence".into(),
                first_chunk_max_words: 10,
                sentence_gap_ms: 100,
                echo_cancellation: false,
                mic_open_while_speaking: false,
                follow_up_window_s: 15,
                sleep_phrase: "stand down".into(),
                ignore_noise_min_words: 3,
            }
        );
    }

    #[test]
    fn builtin_engine_is_read() {
        let (cfg, warnings) = load_text("voice_engine = \"builtin\"\n");
        assert!(warnings.is_empty());
        assert_eq!(cfg.voice_engine, VoiceEngine::Builtin);
    }

    #[test]
    fn the_engine_must_be_lower_case_and_known() {
        assert_one_warning_for("voice_engine = \"Pipecat\"", "voice_engine");
        assert_one_warning_for("voice_engine = \"other\"", "voice_engine");
        assert_one_warning_for("voice_engine = 1", "voice_engine");
        assert_one_warning_for("tts_chunk = \"word\"", "tts_chunk");
    }

    #[test]
    fn a_bad_value_warns_and_uses_the_default() {
        let (cfg, warnings) = load_text("tts_speed = 9\n");
        assert_eq!(cfg.tts_speed, 1.0);
        assert_eq!(
            warnings,
            vec!["voice.toml: tts_speed must be between 0.5 and 2.0; using 1.0".to_string()]
        );
    }

    #[test]
    fn a_wrong_type_warns() {
        let (cfg, warnings) = load_text("smart_turn = \"yes\"\n");
        assert!(cfg.smart_turn);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].starts_with("voice.toml:") && warnings[0].contains("smart_turn"));
    }

    #[test]
    fn range_ends_are_accepted() {
        for (text, check) in [
            (
                "tts_speed = 0.5",
                (|c: &VoiceConfig| c.tts_speed == 0.5) as fn(&VoiceConfig) -> bool,
            ),
            ("tts_speed = 2.0", |c| c.tts_speed == 2.0),
            ("follow_up_window_s = 0", |c| c.follow_up_window_s == 0),
            ("follow_up_window_s = 3600", |c| {
                c.follow_up_window_s == 3600
            }),
            ("vad_threshold = 0.1", |c| c.vad_threshold == 0.1),
            ("vad_threshold = 0.9", |c| c.vad_threshold == 0.9),
            ("duck_volume = 0.0", |c| c.duck_volume == 0.0),
            ("duck_volume = 1.0", |c| c.duck_volume == 1.0),
            ("ignore_noise_min_words = 1", |c| {
                c.ignore_noise_min_words == 1
            }),
            ("ignore_noise_min_words = 5", |c| {
                c.ignore_noise_min_words == 5
            }),
            ("barge_in_after_ms = 100", |c| c.barge_in_after_ms == 100),
            ("barge_in_after_ms = 2000", |c| c.barge_in_after_ms == 2000),
        ] {
            let (cfg, warnings) = load_text(text);
            assert!(warnings.is_empty(), "{text}: {warnings:?}");
            assert!(check(&cfg), "{text}");
        }
    }

    #[test]
    fn just_outside_a_range_is_rejected() {
        assert_one_warning_for("tts_speed = 2.01", "tts_speed");
        assert_one_warning_for("tts_speed = 0.49", "tts_speed");
        assert_one_warning_for("follow_up_window_s = 3601", "follow_up_window_s");
        assert_one_warning_for("vad_threshold = 0.95", "vad_threshold");
        assert_one_warning_for("vad_threshold = 0.09", "vad_threshold");
        assert_one_warning_for("ignore_noise_min_words = 0", "ignore_noise_min_words");
        assert_one_warning_for("ignore_noise_min_words = 6", "ignore_noise_min_words");
        assert_one_warning_for("barge_in_after_ms = 99", "barge_in_after_ms");
    }

    #[test]
    fn an_integer_is_accepted_for_a_float_key() {
        let (cfg, warnings) = load_text("tts_speed = 1\n");
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(cfg.tts_speed, 1.0);
        let (cfg, warnings) = load_text("tts_speed = 2\nduck_volume = 1\n");
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!((cfg.tts_speed, cfg.duck_volume), (2.0, 1.0));
    }

    #[test]
    fn a_float_is_rejected_for_an_integer_key() {
        assert_one_warning_for("duck_fade_ms = 80.5", "duck_fade_ms");
        assert_one_warning_for("max_turn_s = 30.0", "max_turn_s");
    }

    #[test]
    fn a_negative_number_is_rejected() {
        assert_one_warning_for("duck_fade_ms = -1", "duck_fade_ms");
        assert_one_warning_for("tts_speed = -1.0", "tts_speed");
        assert_one_warning_for("follow_up_window_s = -5", "follow_up_window_s");
        // Larger than any u32 must not wrap or panic.
        assert_one_warning_for("follow_up_window_s = 9999999999", "follow_up_window_s");
    }

    #[test]
    fn nan_and_inf_are_rejected() {
        for v in ["nan", "inf", "-inf", "+inf"] {
            assert_one_warning_for(&format!("tts_speed = {v}"), "tts_speed");
        }
    }

    #[test]
    fn one_bad_key_does_not_spoil_the_others() {
        let (cfg, warnings) = load_text("tts_speed = 9\nsmart_turn = false\nmax_turn_s = 60\n");
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(warnings[0].contains("tts_speed"));
        assert_eq!(cfg.tts_speed, 1.0);
        assert!(!cfg.smart_turn);
        assert_eq!(cfg.max_turn_s, 60);
    }

    #[test]
    fn sleep_phrase_is_trimmed_and_bounded() {
        let (cfg, warnings) = load_text("sleep_phrase = \"  go to sleep  \"\n");
        assert!(warnings.is_empty());
        assert_eq!(cfg.sleep_phrase, "go to sleep");

        assert_one_warning_for("sleep_phrase = \"\"", "sleep_phrase");
        assert_one_warning_for("sleep_phrase = \"   \"", "sleep_phrase");
        assert_one_warning_for(
            &format!("sleep_phrase = \"{}\"", "a".repeat(41)),
            "sleep_phrase",
        );
        assert_one_warning_for("sleep_phrase = \"!!!\"", "sleep_phrase");
        assert_one_warning_for("sleep_phrase = 5", "sleep_phrase");

        let forty = "é".repeat(40); // 80 bytes, 40 characters
        let (cfg, warnings) = load_text(&format!("sleep_phrase = \"{forty}\"\n"));
        assert!(warnings.is_empty(), "{warnings:?}");
        assert_eq!(cfg.sleep_phrase, forty);
        assert_one_warning_for(
            &format!("sleep_phrase = \"{}\"", "é".repeat(41)),
            "sleep_phrase",
        );
    }

    #[test]
    fn an_unknown_key_warns_and_the_rest_still_loads() {
        let (cfg, warnings) = load_text("voce_engine = \"builtin\"\nsmart_turn = false\n");
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(warnings[0].starts_with("voice.toml:") && warnings[0].contains("voce_engine"));
        assert!(!cfg.smart_turn);
        assert_eq!(cfg.voice_engine, VoiceEngine::Pipecat);
    }

    #[test]
    fn a_file_that_is_not_toml_gives_defaults_and_one_warning() {
        let (cfg, warnings) = load_text("this is = = not toml [[[\n");
        assert_eq!(cfg, VoiceConfig::default());
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].starts_with("voice.toml:"));
    }

    #[test]
    fn a_duplicate_key_is_one_warning_and_all_defaults() {
        let (cfg, warnings) = load_text("smart_turn = false\nsmart_turn = true\nmax_turn_s = 60\n");
        assert_eq!(cfg, VoiceConfig::default());
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(warnings[0].starts_with("voice.toml:"));
    }

    #[test]
    fn a_table_where_a_value_is_expected_warns() {
        assert_one_warning_for("[tts_speed]\nx = 1\n", "tts_speed");
        assert_one_warning_for("smart_turn = [true]", "smart_turn");
        // A table or array under an unknown name is one unknown-key warning.
        assert_one_warning_for("[extra]\nx = 1\n", "extra");
    }

    #[test]
    fn a_file_that_cannot_be_read_gives_defaults_and_one_warning() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join(FILE)).unwrap(); // a folder, not a file
        let (cfg, warnings) = VoiceConfig::load(dir.path());
        assert_eq!(cfg, VoiceConfig::default());
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].starts_with("voice.toml:"));
    }

    #[test]
    fn an_empty_file_is_all_defaults() {
        let (cfg, warnings) = load_text("");
        assert_eq!(cfg, VoiceConfig::default());
        assert!(warnings.is_empty());
    }
}
