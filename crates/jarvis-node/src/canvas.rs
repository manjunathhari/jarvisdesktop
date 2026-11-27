//! The Canvas server: where a Canvas panel's page is served from, so that it is sealed.
//!
//! A Canvas panel is a web page written by the model, partly from what it read on the web, so
//! it is untrusted and must be able to draw and nothing else.
//!
//! **Why a loopback server, and not a URI scheme registered with the app.** Tauri counts any
//! page from a scheme the app registered as *local*, with the app's own right to call its
//! commands, and on Windows it loads its command interface into every frame, sandboxed or not.
//! A page from `http://127.0.0.1:<port>` is not local to Tauri: every command from it is
//! refused. So the page is served from here, and the Board shows it in a sandboxed frame.
//!
//! What seals the page:
//! - its origin is not the app's (above), and the frame's sandbox makes it no origin at all;
//! - the response's own policy ([`POLICY`]): no network of any kind, only inline script and
//!   style, images and fonts only as `data:`, no forms, and the sandbox again;
//! - the Board's policy allows frames only from the three fixed [`PORTS`], so the page cannot
//!   point its frame anywhere else either.
//!
//! What protects the panels from other programs on this PC:
//! - the server listens on the loopback only;
//! - every address carries a secret made fresh for this run, compared in constant time;
//! - the `Host` must be `127.0.0.1:<port>` exactly (no name that could be re-pointed here);
//! - only `GET` of exactly `/<secret>/<panel id>/<revision>`, for a canvas panel at the
//!   revision the store holds now. Anything else is an empty 404.

use std::collections::HashSet;
use std::io;
use std::net::Ipv4Addr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OnceCell, Semaphore};
use uuid::Uuid;

use crate::board::{BoardStore, PanelKind};

/// The only ports the server uses, in order of preference. The app's content policy lets the
/// Board frame these three origins and no other (`tauri.conf.json`, checked by a test).
pub const PORTS: [u16; 3] = [47821, 47822, 47823];

/// The policy every answer carries. The page may run its own inline script and style and
/// show `data:` images and fonts. It may not load, fetch, post or frame anything.
pub const POLICY: &str = "default-src 'none'; script-src 'unsafe-inline'; \
    style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; \
    base-uri 'none'; sandbox allow-scripts";

/// The Board window's browser arguments (`tauri.conf.json` must match; a test checks). The
/// Board needs no outside network at all, and a page's policy cannot stop everything:
/// - a proxy that is not there: nothing leaves this PC, not even the name lookup and
///   connection a blocked navigation or a `prerender` hint still makes (the loopback, where
///   the app and this server are, does not go through a proxy);
/// - no WebRTC outside a proxy;
/// - no prerendering, which also reached other loopback ports.
///
/// No autoplay argument: a Canvas page must not make sound unasked. Different arguments need
/// a different browser data folder from the main window's, which the app gives it.
pub const BOARD_BROWSER_ARGS: &str =
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
    --proxy-server=http://127.0.0.1:1 --webrtc-ip-handling-policy=disable_non_proxied_udp \
    --enable-features=NoStatePrefetchHoldback";

/// Runs first in every frame of the Board window: removes WebRTC, sound, speech and the old
/// clipboard command from Canvas pages and anything they make.
pub const SEAL_SCRIPT: &str = include_str!("canvas_seal.js");

/// The longest request head read. The address is under 100 bytes; WebView2's headers fit.
const HEAD_MAX: usize = 8 * 1024;
/// How long a caller has to send its request, and to take the answer.
const PATIENCE: Duration = Duration::from_secs(2);
/// Connections handled at once; more wait. Many more than a Board has panels, so idle
/// callers cannot hold up a real one for long.
const AT_ONCE: usize = 64;
/// "Served" marks kept (one per panel revision shown this run).
const SERVED_MAX: usize = 4096;

/// An answer, before it is written as HTTP.
#[derive(Debug)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(&'static str, String)>,
    pub body: Vec<u8>,
    /// The panel and revision whose page this answer carries, if it carries one.
    pub served: Option<(Uuid, u32)>,
}

fn answer(status: u16, body: Vec<u8>, served: Option<(Uuid, u32)>) -> Response {
    let mut headers = vec![
        ("Content-Security-Policy", POLICY.to_owned()),
        ("X-Content-Type-Options", "nosniff".to_owned()),
        ("Referrer-Policy", "no-referrer".to_owned()),
        ("Cache-Control", "no-store".to_owned()),
        ("Cross-Origin-Resource-Policy", "same-origin".to_owned()),
        ("Content-Type", "text/html; charset=utf-8".to_owned()),
    ];
    if status == 405 {
        headers.push(("Allow", "GET".to_owned()));
    }
    Response {
        status,
        headers,
        body,
        served,
    }
}

/// Equal, without stopping at the first difference.
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

/// A revision as the address writes it: digits only, no sign, no leading zero.
fn revision(text: &str) -> Option<u32> {
    let plain = !text.is_empty()
        && text.bytes().all(|b| b.is_ascii_digit())
        && (text == "0" || !text.starts_with('0'));
    plain.then(|| text.parse().ok()).flatten()
}

/// Decide the answer to one request. Pure: the store is only read.
pub fn respond(
    store: &BoardStore,
    token: &str,
    port: u16,
    method: &str,
    host: Option<&str>,
    path: &str,
) -> Response {
    if method != "GET" {
        return answer(405, Vec::new(), None);
    }
    let not_found = || answer(404, Vec::new(), None);
    if host != Some(format!("127.0.0.1:{port}").as_str()) {
        return not_found();
    }
    let mut parts = path.split('/');
    let (Some(""), Some(secret), Some(id), Some(rev), None) = (
        parts.next(),
        parts.next(),
        parts.next(),
        parts.next(),
        parts.next(),
    ) else {
        return not_found();
    };
    if !same(secret, token) {
        return not_found();
    }
    let (Ok(parsed), Some(rev)) = (Uuid::try_parse(id), revision(rev)) else {
        return not_found();
    };
    // One address for a page: the id exactly as the Board writes it.
    if parsed.to_string() != id {
        return not_found();
    }
    let id = parsed;
    match store.panel(id) {
        Some(panel) if panel.kind == PanelKind::Canvas && panel.revision == rev => {
            answer(200, panel.content.clone().into_bytes(), Some((id, rev)))
        }
        _ => not_found(),
    }
}

/// The running server.
#[derive(Debug)]
pub struct CanvasServer {
    port: u16,
    token: String,
    served: Mutex<HashSet<(Uuid, u32)>>,
}

impl CanvasServer {
    pub fn port(&self) -> u16 {
        self.port
    }

    /// The address of a panel's page, for the Board's frame.
    pub fn url(&self, panel_id: Uuid, revision: u32) -> String {
        format!(
            "http://127.0.0.1:{}/{}/{panel_id}/{revision}",
            self.port, self.token
        )
    }

    /// Whether that panel's page at that revision was handed out since this was last asked.
    /// The Board asks before it reports a Canvas panel drawn: a frame that "loaded" an empty
    /// 404 is not one. Asking takes the mark, so each drawing needs its own page.
    pub fn take_served(&self, panel_id: Uuid, revision: u32) -> bool {
        self.served
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&(panel_id, revision))
    }

    fn mark(&self, key: (Uuid, u32)) {
        let mut served = self.served.lock().unwrap_or_else(|e| e.into_inner());
        if served.len() >= SERVED_MAX {
            served.clear();
        }
        served.insert(key);
    }
}

fn fresh_token() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|e| io::Error::other(e.to_string()))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Start the server on the first of `ports` that is free (0 asks the system for any), on the
/// loopback only. Must be called inside a tokio runtime; it serves until the runtime ends.
pub async fn serve(store: Arc<Mutex<BoardStore>>, ports: &[u16]) -> io::Result<Arc<CanvasServer>> {
    let mut listener = None;
    for &port in ports {
        if let Ok(bound) = TcpListener::bind((Ipv4Addr::LOCALHOST, port)).await {
            listener = Some(bound);
            break;
        }
    }
    let listener =
        listener.ok_or_else(|| io::Error::new(io::ErrorKind::AddrInUse, "no canvas port free"))?;
    let server = Arc::new(CanvasServer {
        port: listener.local_addr()?.port(),
        token: fresh_token()?,
        served: Mutex::new(HashSet::new()),
    });
    let serving = server.clone();
    let slots = Arc::new(Semaphore::new(AT_ONCE));
    tokio::spawn(async move {
        loop {
            let Ok(slot) = slots.clone().acquire_owned().await else {
                return;
            };
            let Ok((stream, _)) = listener.accept().await else {
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            };
            let (server, store) = (serving.clone(), store.clone());
            tokio::spawn(async move {
                let _slot = slot;
                let _ = tokio::time::timeout(PATIENCE * 2, handle(stream, &server, store)).await;
            });
        }
    });
    Ok(server)
}

/// Read one request head, answer it, close.
async fn handle(
    mut stream: TcpStream,
    server: &CanvasServer,
    store: Arc<Mutex<BoardStore>>,
) -> io::Result<()> {
    let mut head = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];
    let complete = loop {
        if head.windows(4).any(|w| w == b"\r\n\r\n") {
            break true;
        }
        if head.len() >= HEAD_MAX {
            break false;
        }
        match tokio::time::timeout(PATIENCE, stream.read(&mut chunk)).await {
            Ok(Ok(n)) if n > 0 => head.extend_from_slice(&chunk[..n]),
            _ => return Ok(()), // silent, slow or gone: nothing is owed
        }
    };
    let response = match parse(&head).filter(|_| complete) {
        Some((method, path, host)) => {
            let (token, port) = (server.token.clone(), server.port);
            let (method, path, host) =
                (method.to_owned(), path.to_owned(), host.map(str::to_owned));
            // The store does file work under its lock: never wait for it on the runtime.
            tokio::task::spawn_blocking(move || {
                let store = store.lock().unwrap_or_else(|e| e.into_inner());
                let response = respond(&store, &token, port, &method, host.as_deref(), &path);
                if response.status != 200 {
                    // Something asked for what is not a page: worth seeing in the log. Only
                    // the start of the path, and never one that carries the secret.
                    let shown: String = if path.contains(token.as_str()) {
                        "(a path with the secret)".to_owned()
                    } else {
                        path.chars().take(40).collect()
                    };
                    tracing::warn!(status = response.status, %method, path = %shown,
                        "the Canvas server refused a request");
                }
                response
            })
            .await
            .map_err(io::Error::other)?
        }
        None => answer(400, Vec::new(), None),
    };
    let reason = match response.status {
        200 => "OK",
        400 => "Bad Request",
        405 => "Method Not Allowed",
        _ => "Not Found",
    };
    let mut out = format!("HTTP/1.1 {} {reason}\r\n", response.status);
    for (name, value) in &response.headers {
        out.push_str(&format!("{name}: {value}\r\n"));
    }
    out.push_str(&format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        response.body.len()
    ));
    let mut bytes = out.into_bytes();
    bytes.extend_from_slice(&response.body);
    tokio::time::timeout(PATIENCE, stream.write_all(&bytes))
        .await
        .map_err(|_| io::Error::from(io::ErrorKind::TimedOut))??;
    // Marked only once the page has really gone out.
    if let Some(key) = response.served {
        server.mark(key);
    }
    stream.shutdown().await
}

/// The method, path and `Host` of a request head, or `None` if it is not HTTP/1.x.
fn parse(head: &[u8]) -> Option<(&str, &str, Option<&str>)> {
    let head = std::str::from_utf8(head).ok()?;
    let mut lines = head.split("\r\n");
    let mut first = lines.next()?.split(' ');
    let (method, path, version) = (first.next()?, first.next()?, first.next()?);
    if first.next().is_some() || !version.starts_with("HTTP/1.") || method.is_empty() {
        return None;
    }
    let mut host = None;
    for line in lines.take_while(|l| !l.is_empty()) {
        let (name, value) = line.split_once(':')?;
        if name.eq_ignore_ascii_case("host") {
            if host.is_some() {
                return None; // two Hosts: which one would be believed?
            }
            host = Some(value.trim());
        }
    }
    Some((method, path, host))
}

static SHARED: OnceCell<Option<Arc<CanvasServer>>> = OnceCell::const_new();

/// The app's one Canvas server, started on first use on one of [`PORTS`]. `None` if none of
/// the three could be had: Canvas panels then cannot be shown, and say so.
pub async fn shared(store: Arc<Mutex<BoardStore>>) -> Option<Arc<CanvasServer>> {
    SHARED
        .get_or_init(|| async {
            match serve(store, &PORTS).await {
                Ok(server) => Some(server),
                Err(e) => {
                    tracing::warn!(error = %e, "the Canvas server could not start");
                    None
                }
            }
        })
        .await
        .clone()
}
