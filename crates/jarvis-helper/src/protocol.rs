//! Message shapes. They match `helper/jarvis_voice_helper/protocol.py`.

use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u32 = 1;

/// The settings the helper needs to start its speaker (`hello.settings`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HelperSettings {
    pub voice: String,
    pub tts_speed: f32,
    pub model_path: String,
    pub voices_path: String,
    /// Serialised as `null` when absent.
    pub output_device: Option<i32>,
}

/// App to helper.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum ToHelper {
    Hello {
        protocol: u32,
        settings: HelperSettings,
    },
    Say {
        turn: u64,
        text: String,
    },
    Stop,
    Shutdown,
}

/// Helper to app. Unknown fields are ignored; an unknown `type` fails to parse and is dropped.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum FromHelper {
    Ready { helper_version: String },
    Speaking { on: bool },
    Error { code: String, message: String },
}
