//! The app's half of the conversation with the voice helper: one JSON object per line over the
//! helper's stdin and stdout (UTF-8, `protocol` 1; either side ignores unknown types and fields).

pub mod install;
pub mod link;
pub mod process;
pub mod protocol;
pub mod supervisor;
pub mod voice_config;

pub use link::{HelperLink, HelperVoice, LinkEvent};
pub use process::{spawn, Running};
pub use protocol::{FromHelper, HelperSettings, ToHelper};
