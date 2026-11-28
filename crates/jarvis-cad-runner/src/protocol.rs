//! The runner's wire protocol: one JSON request line in, one JSON answer line out.
//!
//! The shapes are fixed by the CAD board slice 1 plan, Task 2 ("Interfaces — Produces"). The
//! server side (`core/cad_engine.py`) checks every field of an answer again before using it.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The only protocol version.
pub const VERSION: u8 = 1;

/// One drawing command, as Claude sent it: an engine command id and its parameters.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Command {
    pub command: String,
    pub params: Map<String, Value>,
}

/// One step of a recipe: a label and its commands, replayed in order.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Step {
    pub label: String,
    pub commands: Vec<Command>,
}

/// A request, as read from the request line.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
pub enum Request {
    Replay {
        v: u8,
        steps: Vec<Step>,
        #[serde(default)]
        picture: bool,
    },
    Catalogue {
        v: u8,
    },
}

impl Request {
    /// Parses a request line; `None` for anything that is not a version 1 request.
    pub fn parse(line: &[u8]) -> Option<Request> {
        let request: Request = serde_json::from_slice(line).ok()?;
        let v = match &request {
            Request::Replay { v, .. } | Request::Catalogue { v } => *v,
        };
        (v == VERSION).then_some(request)
    }

    /// The total number of commands in a replay request (0 for a catalogue).
    pub fn command_count(&self) -> usize {
        match self {
            Request::Replay { steps, .. } => steps.iter().map(|s| s.commands.len()).sum(),
            Request::Catalogue { .. } => 0,
        }
    }
}

/// The reasons a request can fail.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    BadRequest,
    NotAllowed,
    TooManyCommands,
    Timeout,
    Busy,
    EngineFailed,
    TooLarge,
}

/// One command's result.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CommandResult {
    pub step: usize,
    pub n: usize,
    pub command: String,
    pub ok: bool,
    /// One line, at most 300 characters.
    pub text: String,
}

/// The answer to a replay.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ReplayAnswer {
    pub v: u8,
    pub ok: bool,
    pub engine: String,
    pub engine_version: String,
    pub results: Vec<CommandResult>,
    pub failed: usize,
    pub counts: BTreeMap<String, u64>,
    pub items: u64,
    pub extents: Option<[f64; 2]>,
    pub texts: Vec<String>,
    pub png_b64: Option<String>,
    pub svg_b64: Option<String>,
}

/// The answer to a catalogue request: the engine's parameter note for each allowed command.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CatalogueAnswer {
    pub v: u8,
    pub ok: bool,
    pub engine_version: String,
    pub commands: BTreeMap<String, String>,
}

/// A failure.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Failure {
    pub v: u8,
    pub ok: bool,
    pub error: ErrorCode,
}

/// Any answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Answer {
    Replay(Box<ReplayAnswer>),
    Catalogue(CatalogueAnswer),
    Failure(Failure),
}

impl Answer {
    pub fn failure(error: ErrorCode) -> Answer {
        Answer::Failure(Failure {
            v: VERSION,
            ok: false,
            error,
        })
    }

    /// The error code, for a failure.
    pub fn error(&self) -> Option<ErrorCode> {
        match self {
            Answer::Failure(f) => Some(f.error),
            _ => None,
        }
    }

    /// The answer line, ending in `\n`.
    pub fn to_line(&self) -> Vec<u8> {
        // Serialising these types cannot fail (string keys, finite numbers only).
        let mut line = serde_json::to_vec(self)
            .unwrap_or_else(|_| br#"{"v":1,"ok":false,"error":"engine_failed"}"#.to_vec());
        line.push(b'\n');
        line
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_both_requests() {
        let r = Request::parse(br#"{"v":1,"op":"catalogue"}"#).unwrap();
        assert_eq!(r, Request::Catalogue { v: 1 });
        let r = Request::parse(
            br#"{"v":1,"op":"replay","steps":[{"label":"a","commands":[{"command":"line","params":{"x":1}}]}]}"#,
        )
        .unwrap();
        assert_eq!(r.command_count(), 1);
        assert!(matches!(r, Request::Replay { picture: false, .. }));
    }

    #[test]
    fn refuses_other_versions_and_shapes() {
        assert!(Request::parse(br#"{"v":2,"op":"catalogue"}"#).is_none());
        assert!(Request::parse(br#"{"op":"catalogue"}"#).is_none());
        assert!(Request::parse(
            br#"{"v":1,"op":"replay","steps":[{"label":"a","commands":[{"command":"line"}]}]}"#
        )
        .is_none());
        assert!(Request::parse(br#"{"v":1,"op":"replay","steps":[{"label":"a","commands":[{"command":"line","params":null}]}]}"#).is_none());
        assert!(Request::parse(b"[]").is_none());
    }

    #[test]
    fn failure_line_shape() {
        let line = Answer::failure(ErrorCode::TooManyCommands).to_line();
        assert_eq!(
            line,
            b"{\"v\":1,\"ok\":false,\"error\":\"too_many_commands\"}\n"
        );
    }
}
