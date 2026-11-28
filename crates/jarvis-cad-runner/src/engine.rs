//! The engine child process and its newline-delimited JSON-RPC (MCP over stdio).
//!
//! Ported from the slice 0 test (`spikes/cad-engine/measure.py`, class `Mcp`). Everything the
//! engine writes is untrusted: every line read is bounded, lines that are not the awaited reply
//! are skipped, and nothing from it is used as a path or a length.

use std::path::Path;
use std::process::Stdio;

use serde_json::{json, Value};
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

/// The protocol version the engine was measured with (CADCraft 0.12.0).
const PROTOCOL_VERSION: &str = "2025-06-18";

/// Why talking to the engine failed. All of them answer `engine_failed`.
#[derive(Debug)]
pub enum EngineError {
    Start(std::io::Error),
    Io(std::io::Error),
    /// The engine closed its output.
    Closed,
    /// A line longer than the limit.
    LineTooLong,
    /// The `initialize` reply was not a result.
    NotInitialised,
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::Start(e) => write!(f, "could not start the engine: {e}"),
            EngineError::Io(e) => write!(f, "engine pipe: {e}"),
            EngineError::Closed => write!(f, "the engine closed its output"),
            EngineError::LineTooLong => write!(f, "the engine wrote a line over the limit"),
            EngineError::NotInitialised => write!(f, "the engine did not initialise"),
        }
    }
}

impl std::error::Error for EngineError {}

/// What a `tools/call` came back with.
#[derive(Debug, Default)]
pub struct ToolReply {
    /// The tool said it failed, or the call was a JSON-RPC error.
    pub is_error: bool,
    /// All text content items, joined with `\n`, read whole (replies can pass 4,000 characters).
    pub text: String,
    /// The `data` of the first image content item, as sent (base64, unchecked).
    pub image: Option<String>,
}

/// A running engine, initialised. Dropping it kills the child.
pub struct Engine {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
    max_line: usize,
    version: String,
}

impl Engine {
    /// Starts `<path> mcp` with an empty environment in `workdir`, and initialises it.
    pub async fn start(
        path: &Path,
        workdir: &Path,
        max_line: usize,
    ) -> Result<Engine, EngineError> {
        let mut child = Command::new(path)
            .arg("mcp")
            .env_clear()
            .current_dir(workdir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(EngineError::Start)?;
        let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            return Err(EngineError::Closed);
        };
        let mut engine = Engine {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 0,
            max_line,
            version: String::new(),
        };
        let reply = engine
            .request(
                "initialize",
                json!({
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jarvis-cad-runner", "version": env!("CARGO_PKG_VERSION")},
                }),
            )
            .await?;
        let Some(result) = reply.get("result").filter(|r| r.is_object()) else {
            return Err(EngineError::NotInitialised);
        };
        let version = result
            .pointer("/serverInfo/version")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        engine.version = crate::replay::one_line(version, 40);
        if engine.version.is_empty() {
            engine.version = "unknown".into();
        }
        engine
            .send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            .await?;
        Ok(engine)
    }

    /// The engine's version, from its `initialize` reply (one line, at most 40 characters).
    pub fn version(&self) -> &str {
        &self.version
    }

    /// Calls one tool and returns its reply.
    pub async fn call(&mut self, name: &str, arguments: Value) -> Result<ToolReply, EngineError> {
        let reply = self
            .request("tools/call", json!({"name": name, "arguments": arguments}))
            .await?;
        Ok(tool_reply(&reply))
    }

    /// Kills the child and waits for it to go.
    pub async fn kill(mut self) {
        let _ = self.child.kill().await;
    }

    async fn send(&mut self, message: &Value) -> Result<(), EngineError> {
        let mut line = serde_json::to_vec(message).map_err(|e| EngineError::Io(e.into()))?;
        line.push(b'\n');
        self.stdin.write_all(&line).await.map_err(EngineError::Io)?;
        self.stdin.flush().await.map_err(EngineError::Io)
    }

    async fn request(&mut self, method: &str, params: Value) -> Result<Value, EngineError> {
        self.next_id += 1;
        let wanted = self.next_id;
        self.send(&json!({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params}))
            .await?;
        loop {
            let line = read_bounded_line(&mut self.stdout, self.max_line)
                .await?
                .ok_or(EngineError::Closed)?;
            let Ok(reply) = serde_json::from_slice::<Value>(&line) else {
                continue; // not JSON: a log line
            };
            if reply.get("id").and_then(Value::as_u64) == Some(wanted)
                && reply.get("method").is_none()
            {
                return Ok(reply);
            }
        }
    }
}

/// Reads one line (without its `\n`), at most `max` bytes. `Ok(None)` at end of input.
pub async fn read_bounded_line<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    max: usize,
) -> Result<Option<Vec<u8>>, EngineError> {
    let mut line = Vec::new();
    loop {
        let buf = reader.fill_buf().await.map_err(EngineError::Io)?;
        if buf.is_empty() {
            return Ok(if line.is_empty() { None } else { Some(line) });
        }
        let (take, done) = match buf.iter().position(|b| *b == b'\n') {
            Some(i) => (i, true),
            None => (buf.len(), false),
        };
        if line.len() + take > max {
            return Err(EngineError::LineTooLong);
        }
        line.extend_from_slice(&buf[..take]);
        reader.consume(if done { take + 1 } else { take });
        if done {
            return Ok(Some(line));
        }
    }
}

/// The text and image of a `tools/call` reply (`whole_text` and `image_of` in the slice 0 test).
fn tool_reply(reply: &Value) -> ToolReply {
    if let Some(error) = reply.get("error") {
        let message = error
            .get("message")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| error.to_string());
        return ToolReply {
            is_error: true,
            text: message,
            image: None,
        };
    }
    let Some(result) = reply.get("result").filter(|r| r.is_object()) else {
        return ToolReply {
            is_error: true,
            text: "no result".into(),
            image: None,
        };
    };
    let mut texts = Vec::new();
    let mut image = None;
    for item in result
        .get("content")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        match item.get("type").and_then(Value::as_str) {
            Some("text") => {
                if let Some(t) = item.get("text").and_then(Value::as_str) {
                    texts.push(t);
                }
            }
            Some("image") if image.is_none() => {
                image = item.get("data").and_then(Value::as_str).map(str::to_string);
            }
            _ => {}
        }
    }
    ToolReply {
        is_error: result
            .get("isError")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        text: texts.join("\n"),
        image,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bounded_lines() {
        let mut input: &[u8] = b"one\ntwo\nlast";
        assert_eq!(
            read_bounded_line(&mut input, 10).await.unwrap().unwrap(),
            b"one"
        );
        assert_eq!(
            read_bounded_line(&mut input, 10).await.unwrap().unwrap(),
            b"two"
        );
        assert_eq!(
            read_bounded_line(&mut input, 10).await.unwrap().unwrap(),
            b"last"
        );
        assert!(read_bounded_line(&mut input, 10).await.unwrap().is_none());

        let mut input: &[u8] = b"0123456789\n";
        assert_eq!(
            read_bounded_line(&mut input, 10)
                .await
                .unwrap()
                .unwrap()
                .len(),
            10
        );
        let mut input: &[u8] = b"0123456789A\n";
        assert!(matches!(
            read_bounded_line(&mut input, 10).await,
            Err(EngineError::LineTooLong)
        ));
    }

    #[test]
    fn tool_replies() {
        let r = tool_reply(&json!({"result": {"content": [
            {"type": "text", "text": "a"}, {"type": "image", "data": "QQ=="}, {"type": "text", "text": "b"}
        ]}}));
        assert!(!r.is_error);
        assert_eq!(r.text, "a\nb");
        assert_eq!(r.image.as_deref(), Some("QQ=="));
        let r = tool_reply(
            &json!({"result": {"isError": true, "content": [{"type": "text", "text": "no"}]}}),
        );
        assert!(r.is_error);
        let r = tool_reply(&json!({"error": {"code": 1, "message": "bad"}}));
        assert!(r.is_error && r.text == "bad");
        assert!(tool_reply(&json!({"result": 3})).is_error);
    }
}
