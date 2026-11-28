//! The engine child process and its newline-delimited JSON-RPC (MCP over stdio).
//!
//! Ported from the slice 0 test (`spikes/cad-engine/measure.py`, class `Mcp`). Everything the
//! engine writes is untrusted: every line read is bounded, lines are decoded into small typed
//! shapes that skip what they do not name (never a whole `Value`), at most
//! [`MAX_STRAY_LINES`] lines that are not the awaited reply are skipped, and nothing from it is
//! used as a path or a length.
//!
//! The engine runs in a process group of its own; [`Engine::kill`] (and dropping an `Engine`)
//! sends SIGKILL to the whole group, so helpers it started go with it.

use std::fmt;
use std::path::Path;
use std::process::Stdio;
#[cfg(unix)]
use std::sync::atomic::{AtomicI32, Ordering};

use serde::de::{Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use serde_json::json;
use serde_json::value::RawValue;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

/// The protocol version the engine was measured with (CADCraft 0.12.0).
const PROTOCOL_VERSION: &str = "2025-06-18";

/// Lines that do not answer the pending request, skipped before giving up.
pub const MAX_STRAY_LINES: usize = 64;

/// A tool reply's text, at most this many bytes (cut on a character boundary).
pub const MAX_TOOL_TEXT: usize = 4 * 1024 * 1024;

/// Why talking to the engine failed. All of them answer `engine_failed`.
#[derive(Debug)]
pub enum EngineError {
    Start(std::io::Error),
    Io(std::io::Error),
    /// The engine closed its output.
    Closed,
    /// A line longer than the limit.
    LineTooLong,
    /// Too many lines that did not answer the pending request.
    TooManyStrayLines,
    /// The `initialize` reply was not a result.
    NotInitialised,
    /// The engine's version is not one line of `[0-9A-Za-z.+-]`, 1 to 30 characters.
    BadVersion,
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EngineError::Start(e) => write!(f, "could not start the engine: {e}"),
            EngineError::Io(e) => write!(f, "engine pipe: {e}"),
            EngineError::Closed => write!(f, "the engine closed its output"),
            EngineError::LineTooLong => write!(f, "the engine wrote a line over the limit"),
            EngineError::TooManyStrayLines => {
                write!(
                    f,
                    "the engine wrote {MAX_STRAY_LINES} lines that did not answer"
                )
            }
            EngineError::NotInitialised => write!(f, "the engine did not initialise"),
            EngineError::BadVersion => write!(f, "the engine's version is not in the pattern"),
        }
    }
}

impl std::error::Error for EngineError {}

/// What a `tools/call` came back with.
#[derive(Debug, Default)]
pub struct ToolReply {
    /// The tool said it failed, the call was a JSON-RPC error, or the reply was malformed.
    pub is_error: bool,
    /// All text content items, joined with `\n`, at most [`MAX_TOOL_TEXT`] bytes.
    pub text: String,
    /// The `data` of the first image content item, as sent (base64, unchecked).
    pub image: Option<String>,
}

/// The engine's process group, killed on drop (unix; nothing elsewhere).
struct Group {
    #[cfg(unix)]
    pgid: i32,
    #[cfg(unix)]
    live: bool,
}

/// The process group of the engine now running, for the SIGTERM handler (0: none).
#[cfg(unix)]
static CURRENT_GROUP: AtomicI32 = AtomicI32::new(0);

impl Group {
    fn of(child: &Child) -> Group {
        #[cfg(unix)]
        {
            let pgid = child.id().and_then(|p| i32::try_from(p).ok()).unwrap_or(0);
            if pgid > 0 {
                CURRENT_GROUP.store(pgid, Ordering::SeqCst);
            }
            Group {
                pgid,
                live: pgid > 0,
            }
        }
        #[cfg(not(unix))]
        {
            let _ = child;
            Group {}
        }
    }

    /// SIGKILL to every process in the group.
    fn kill(&self) {
        #[cfg(unix)]
        if self.live {
            // SAFETY: a plain system call. The group leader is our unreaped child (or a zombie
            // of it), so the id cannot have been reused by another group yet.
            unsafe {
                libc::killpg(self.pgid, libc::SIGKILL);
            }
        }
    }

    /// After the leader is reaped: the id is no longer ours to signal.
    fn forget(&mut self) {
        #[cfg(unix)]
        if self.live {
            let _ =
                CURRENT_GROUP.compare_exchange(self.pgid, 0, Ordering::SeqCst, Ordering::SeqCst);
            self.live = false;
        }
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.kill();
        self.forget();
    }
}

/// Kills the process group of the engine now running, if there is one (the SIGTERM handler).
pub fn kill_current_group() {
    #[cfg(unix)]
    {
        let pgid = CURRENT_GROUP.swap(0, Ordering::SeqCst);
        if pgid > 0 {
            // SAFETY: a plain system call on the group the runner started.
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
    }
}

/// A running engine. Dropping it kills its process group (and the child, reaped by tokio).
pub struct Engine {
    // Declared first: dropped (group killed) before the child handle.
    group: Group,
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
    max_line: usize,
    version: String,
}

#[derive(Serialize)]
struct Rpc<'a, P> {
    jsonrpc: &'static str,
    id: u64,
    method: &'a str,
    params: P,
}

#[derive(Serialize)]
struct Notification<'a> {
    jsonrpc: &'static str,
    method: &'a str,
}

#[derive(Serialize)]
struct ToolCall<'a, A> {
    name: &'a str,
    arguments: A,
}

impl Engine {
    /// Starts `<path> mcp` with an empty environment in `workdir`, in a process group of its
    /// own. Not yet initialised: see [`Engine::initialise`].
    pub fn spawn(path: &Path, workdir: &Path, max_line: usize) -> Result<Engine, EngineError> {
        let mut command = Command::new(path);
        command
            .arg("mcp")
            .env_clear()
            .current_dir(workdir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn().map_err(EngineError::Start)?;
        let group = Group::of(&child);
        let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            return Err(EngineError::Closed);
        };
        Ok(Engine {
            group,
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 0,
            max_line,
            version: String::new(),
        })
    }

    /// The `initialize` exchange; checks the engine's version.
    pub async fn initialise(&mut self) -> Result<(), EngineError> {
        let line = self
            .request(
                "initialize",
                json!({
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jarvis-cad-runner", "version": env!("CARGO_PKG_VERSION")},
                }),
            )
            .await?;
        let version = initialised_version(&line)?;
        if !good_version(&version) {
            return Err(EngineError::BadVersion);
        }
        self.version = version;
        let note = Notification {
            jsonrpc: "2.0",
            method: "notifications/initialized",
        };
        self.send(&note).await
    }

    /// The engine's version, from its `initialize` reply (`^[0-9A-Za-z.+-]{1,30}$`).
    pub fn version(&self) -> &str {
        &self.version
    }

    /// Calls one tool and returns its reply.
    pub async fn call<A: Serialize>(
        &mut self,
        name: &str,
        arguments: A,
    ) -> Result<ToolReply, EngineError> {
        let line = self
            .request("tools/call", ToolCall { name, arguments })
            .await?;
        Ok(tool_reply(&line))
    }

    /// Kills the engine's process group, then the child, and waits for the child to go. The
    /// stored group id is cleared before the wait: once the leader is reaped the id may be
    /// reused, and the SIGTERM handler must not signal it then.
    pub async fn kill(mut self) {
        self.group.kill();
        self.group.forget();
        let _ = self.child.start_kill();
        let _ = self.child.wait().await;
    }

    async fn send<M: Serialize>(&mut self, message: &M) -> Result<(), EngineError> {
        let mut line = serde_json::to_vec(message).map_err(|e| EngineError::Io(e.into()))?;
        line.push(b'\n');
        self.stdin.write_all(&line).await.map_err(EngineError::Io)?;
        self.stdin.flush().await.map_err(EngineError::Io)
    }

    /// Sends a request and returns the line that answers it.
    async fn request<P: Serialize>(
        &mut self,
        method: &str,
        params: P,
    ) -> Result<Vec<u8>, EngineError> {
        self.next_id += 1;
        let wanted = self.next_id;
        let message = Rpc {
            jsonrpc: "2.0",
            id: wanted,
            method,
            params,
        };
        self.send(&message).await?;
        let wanted = wanted.to_string();
        for _ in 0..MAX_STRAY_LINES {
            let line = read_bounded_line(&mut self.stdout, self.max_line)
                .await?
                .ok_or(EngineError::Closed)?;
            if answers(&line, &wanted) {
                return Ok(line);
            }
        }
        Err(EngineError::TooManyStrayLines)
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

/// A JSON-RPC message, decoded only as far as the runner needs: borrowed raw text, other
/// fields skipped.
#[derive(Deserialize)]
struct Envelope<'a> {
    #[serde(borrow, default)]
    id: Option<&'a RawValue>,
    #[serde(default)]
    method: Option<IgnoredAny>,
    #[serde(borrow, default)]
    result: Option<&'a RawValue>,
    #[serde(borrow, default)]
    error: Option<&'a RawValue>,
}

/// Whether the line is the reply (not a request or notification) with this id.
fn answers(line: &[u8], wanted: &str) -> bool {
    match serde_json::from_slice::<Envelope>(line) {
        Ok(m) => m.method.is_none() && m.id.is_some_and(|id| id.get().trim() == wanted),
        Err(_) => false, // not JSON: a log line
    }
}

/// A string if the value is one; anything else is skipped and gives `None`. Never fails, and
/// never builds more than the string itself.
#[derive(Debug, Default)]
pub(crate) struct Lenient(pub(crate) Option<String>);

impl<'de> Deserialize<'de> for Lenient {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Lenient, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Lenient;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_str<E>(self, s: &str) -> Result<Lenient, E> {
                Ok(Lenient(Some(s.to_string())))
            }
            fn visit_string<E>(self, s: String) -> Result<Lenient, E> {
                Ok(Lenient(Some(s)))
            }
            fn visit_bool<E>(self, _: bool) -> Result<Lenient, E> {
                Ok(Lenient(None))
            }
            fn visit_i64<E>(self, _: i64) -> Result<Lenient, E> {
                Ok(Lenient(None))
            }
            fn visit_u64<E>(self, _: u64) -> Result<Lenient, E> {
                Ok(Lenient(None))
            }
            fn visit_f64<E>(self, _: f64) -> Result<Lenient, E> {
                Ok(Lenient(None))
            }
            fn visit_unit<E>(self) -> Result<Lenient, E> {
                Ok(Lenient(None))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Lenient, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(Lenient(None))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Lenient, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(Lenient(None))
            }
        }
        d.deserialize_any(V)
    }
}

/// `true` only for the JSON value `true`; anything else is skipped and gives `false`.
#[derive(Debug, Default)]
struct LenientTrue(bool);

impl<'de> Deserialize<'de> for LenientTrue {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<LenientTrue, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = LenientTrue;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_bool<E>(self, b: bool) -> Result<LenientTrue, E> {
                Ok(LenientTrue(b))
            }
            fn visit_str<E>(self, _: &str) -> Result<LenientTrue, E> {
                Ok(LenientTrue(false))
            }
            fn visit_i64<E>(self, _: i64) -> Result<LenientTrue, E> {
                Ok(LenientTrue(false))
            }
            fn visit_u64<E>(self, _: u64) -> Result<LenientTrue, E> {
                Ok(LenientTrue(false))
            }
            fn visit_f64<E>(self, _: f64) -> Result<LenientTrue, E> {
                Ok(LenientTrue(false))
            }
            fn visit_unit<E>(self) -> Result<LenientTrue, E> {
                Ok(LenientTrue(false))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<LenientTrue, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(LenientTrue(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<LenientTrue, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(LenientTrue(false))
            }
        }
        d.deserialize_any(V)
    }
}

#[derive(Deserialize)]
struct InitResult {
    #[serde(rename = "serverInfo", default)]
    server_info: Option<ServerInfo>,
}

#[derive(Deserialize)]
struct ServerInfo {
    #[serde(default)]
    version: Lenient,
}

#[derive(Deserialize)]
struct ToolResult {
    #[serde(default)]
    content: Content,
    #[serde(rename = "isError", default)]
    is_error: LenientTrue,
}

/// Content items looked at; the rest of a longer list is skipped unread.
pub const MAX_CONTENT_ITEMS: usize = 16;

/// A `content` list, read one item at a time: the text items' texts joined with `\n` (at most
/// [`MAX_TOOL_TEXT`] bytes) and the first image's data, from the first [`MAX_CONTENT_ITEMS`]
/// items. Nothing is kept per item, so a list of a million `{}` costs nothing.
#[derive(Debug, Default)]
struct Content {
    text: String,
    image: Option<String>,
}

impl<'de> Deserialize<'de> for Content {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Content, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Content;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a list of content items")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Content, A::Error> {
                let mut content = Content::default();
                let mut first = true;
                let mut looked = 0;
                while looked < MAX_CONTENT_ITEMS {
                    let Some(item) = seq.next_element::<ContentItem>()? else {
                        return Ok(content);
                    };
                    looked += 1;
                    match (item.kind.0.as_deref(), item.text.0, item.data.0) {
                        (Some("text"), Some(t), _) => {
                            let room = MAX_TOOL_TEXT.saturating_sub(content.text.len() + 1);
                            if !first && room > 0 {
                                content.text.push('\n');
                            }
                            first = false;
                            content.text.push_str(&cap(t, room));
                        }
                        (Some("image"), _, Some(data)) if content.image.is_none() => {
                            content.image = Some(data)
                        }
                        _ => {}
                    }
                }
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(content)
            }
        }
        d.deserialize_seq(V)
    }
}

#[derive(Deserialize)]
struct ContentItem {
    #[serde(rename = "type", default)]
    kind: Lenient,
    #[serde(default)]
    text: Lenient,
    #[serde(default)]
    data: Lenient,
}

#[derive(Deserialize)]
struct RpcError {
    #[serde(default)]
    message: Lenient,
}

/// The version in an `initialize` reply line.
fn initialised_version(line: &[u8]) -> Result<String, EngineError> {
    let envelope: Envelope =
        serde_json::from_slice(line).map_err(|_| EngineError::NotInitialised)?;
    let result = envelope
        .result
        .filter(|r| r.get().starts_with('{'))
        .ok_or(EngineError::NotInitialised)?;
    let init: InitResult =
        serde_json::from_str(result.get()).map_err(|_| EngineError::NotInitialised)?;
    init.server_info
        .and_then(|s| s.version.0)
        .ok_or(EngineError::BadVersion)
}

/// `^[0-9A-Za-z.+-]{1,30}$` (Task 1's `DrawingContent.engine_version`).
pub fn good_version(version: &str) -> bool {
    (1..=30).contains(&version.len())
        && version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'+' || b == b'-')
}

fn malformed(text: &str) -> ToolReply {
    ToolReply {
        is_error: true,
        text: text.into(),
        image: None,
    }
}

/// Cuts a string to at most `max` bytes, on a character boundary.
fn cap(mut text: String, max: usize) -> String {
    if text.len() > max {
        let mut end = max;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
    }
    text
}

/// The text and image of a `tools/call` reply line (`whole_text` and `image_of` in the slice 0
/// test).
fn tool_reply(line: &[u8]) -> ToolReply {
    let Ok(envelope) = serde_json::from_slice::<Envelope>(line) else {
        return malformed("the engine's reply was malformed");
    };
    if let Some(error) = envelope.error {
        let message = serde_json::from_str::<RpcError>(error.get())
            .ok()
            .and_then(|e| e.message.0)
            .unwrap_or_else(|| "engine error".into());
        return malformed(&cap(message, MAX_TOOL_TEXT));
    }
    let Some(result) = envelope.result.filter(|r| r.get().starts_with('{')) else {
        return malformed("no result");
    };
    let Ok(result) = serde_json::from_str::<ToolResult>(result.get()) else {
        return malformed("the engine's reply was malformed");
    };
    ToolReply {
        is_error: result.is_error.0,
        text: result.content.text,
        image: result.content.image,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn line(v: Value) -> Vec<u8> {
        serde_json::to_vec(&v).unwrap()
    }

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
        let r = tool_reply(&line(json!({"id": 1, "result": {"content": [
            {"type": "text", "text": "a"}, {"type": "image", "data": "QQ=="}, {"type": "text", "text": "b"}
        ]}})));
        assert!(!r.is_error);
        assert_eq!(r.text, "a\nb");
        assert_eq!(r.image.as_deref(), Some("QQ=="));
        let r = tool_reply(&line(
            json!({"result": {"isError": true, "content": [{"type": "text", "text": "no"}]}}),
        ));
        assert!(r.is_error);
        let r = tool_reply(&line(json!({"error": {"code": 1, "message": "bad"}})));
        assert!(r.is_error && r.text == "bad");
        assert!(tool_reply(&line(json!({"result": 3}))).is_error);
        assert!(tool_reply(b"not json").is_error);
    }

    #[test]
    fn odd_shapes_in_a_reply_are_skipped_not_built() {
        // Wrong types where strings and a bool belong, and unknown fields with big values.
        let r = tool_reply(&line(json!({"pad": [[0, 0], {"a": [1]}], "result": {
            "isError": "yes", "extra": [0, 0, 0],
            "content": [
                {"type": 5, "text": "skipped"},
                {"type": "text", "text": [1, 2]},
                {"type": "text", "text": "kept", "more": {"x": 1}},
                {"type": "image", "data": {"no": 1}},
            ]
        }})));
        assert!(!r.is_error);
        assert_eq!(r.text, "kept");
        assert!(r.image.is_none());
    }

    #[test]
    fn only_the_first_content_items_are_looked_at() {
        let many: Vec<Value> = (0..100)
            .map(|i| json!({"type": "text", "text": format!("t{i}")}))
            .collect();
        let r = tool_reply(&line(json!({"result": {"content": many}})));
        assert_eq!(r.text.split('\n').count(), MAX_CONTENT_ITEMS);
        assert!(r.text.ends_with(&format!("t{}", MAX_CONTENT_ITEMS - 1)));
        // An image after the items looked at is not seen; a million empty items cost nothing.
        let mut items: Vec<Value> = vec![json!({}); MAX_CONTENT_ITEMS];
        items.push(json!({"type": "image", "data": "QQ=="}));
        let r = tool_reply(&line(json!({"result": {"content": items}})));
        assert!(r.image.is_none() && r.text.is_empty() && !r.is_error);
        let empty = format!(
            r#"{{"result":{{"content":[{}{{}}]}}}}"#,
            "{},".repeat(1_000_000)
        );
        let r = tool_reply(empty.as_bytes());
        assert!(!r.is_error && r.text.is_empty());
        // `content` that is not a list makes the reply malformed.
        assert!(tool_reply(&line(json!({"result": {"content": {"a": 1}}}))).is_error);
    }

    #[test]
    fn replies_are_matched_by_id_and_not_by_method() {
        assert!(answers(br#"{"jsonrpc":"2.0","id":3,"result":{}}"#, "3"));
        assert!(!answers(br#"{"jsonrpc":"2.0","id":4,"result":{}}"#, "3"));
        assert!(!answers(br#"{"jsonrpc":"2.0","id":"3","result":{}}"#, "3"));
        assert!(!answers(br#"{"id":3,"method":"ping"}"#, "3"));
        assert!(!answers(br#"{"method":"notifications/message"}"#, "3"));
        assert!(!answers(b"log line", "3"));
    }

    #[test]
    fn versions_are_checked() {
        for good in ["0.12.0", "1.0.0-rc.1+build.5", "a", &"9".repeat(30)] {
            assert!(good_version(good), "{good}");
        }
        for bad in ["", "0.12.0 (abc)", &"9".repeat(31), "1_0", "é", "1\n"] {
            assert!(!good_version(bad), "{bad:?}");
        }
        let v = initialised_version(&line(
            json!({"id": 1, "result": {"serverInfo": {"version": "0.12.0"}}}),
        ));
        assert_eq!(v.unwrap(), "0.12.0");
        assert!(matches!(
            initialised_version(&line(
                json!({"id": 1, "result": {"serverInfo": {"version": 12}}})
            )),
            Err(EngineError::BadVersion)
        ));
        assert!(matches!(
            initialised_version(&line(json!({"id": 1, "result": [1]}))),
            Err(EngineError::NotInitialised)
        ));
    }

    #[test]
    fn text_is_capped_on_a_character_boundary() {
        assert_eq!(cap("ééé".into(), 3), "é");
        assert_eq!(cap("abc".into(), 5), "abc");
    }
}
