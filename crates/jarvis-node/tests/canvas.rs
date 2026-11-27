//! The Canvas server: the one place a Canvas panel's page is served from. Its job is to seal
//! the page, so the tests pin the exact headers and every way a request is turned away.

use std::sync::{Arc, Mutex};

use jarvis_node::board::{self, BoardEvent, BoardStore};
use jarvis_node::canvas::{self, respond, CanvasServer, POLICY};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use uuid::Uuid;

const TOKEN: &str = "0123456789abcdef0123456789abcdef";
const PAGE: &str = "<!doctype html><title>Fictional</title><svg></svg><script>1</script>";

fn show(id: Uuid, revision: u32, kind: &str, content: &str) -> BoardEvent {
    let args = json!({
        "panel_id": id, "revision": revision, "kind": kind, "title": "Rainfall (fictional)",
        "content": content, "content_sha256": board::content_sha256(content),
        "cost_paise": 0, "created_at": "2026-11-27T10:00:00+05:30",
    });
    board::parse("board.show", args.as_object().unwrap()).unwrap()
}

fn store_with(events: &[BoardEvent]) -> (tempfile::TempDir, BoardStore) {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    for event in events {
        store.apply(event).unwrap();
    }
    (dir, store)
}

fn header<'a>(response: &'a canvas::Response, name: &str) -> Option<&'a str> {
    response
        .headers
        .iter()
        .find(|(n, _)| n.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

#[test]
fn a_canvas_panel_is_served_at_its_revision_with_the_exact_policy_headers() {
    let id = Uuid::new_v4();
    let (_dir, store) = store_with(&[show(id, 3, "canvas", PAGE)]);
    let path = format!("/{TOKEN}/{id}/3");
    let response = respond(&store, TOKEN, 47821, "GET", Some("127.0.0.1:47821"), &path);
    assert_eq!(response.status, 200);
    assert_eq!(response.body, PAGE.as_bytes());
    assert_eq!(response.served, Some((id, 3)));
    assert_eq!(
        POLICY,
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; \
         img-src data:; font-src data:; form-action 'none'; base-uri 'none'; \
         sandbox allow-scripts"
    );
    assert_eq!(header(&response, "Content-Security-Policy"), Some(POLICY));
    assert_eq!(
        header(&response, "Content-Type"),
        Some("text/html; charset=utf-8")
    );
    assert_eq!(header(&response, "X-Content-Type-Options"), Some("nosniff"));
    assert_eq!(header(&response, "Referrer-Policy"), Some("no-referrer"));
    assert_eq!(header(&response, "Cache-Control"), Some("no-store"));
    assert_eq!(
        header(&response, "Cross-Origin-Resource-Policy"),
        Some("same-origin")
    );
    assert_eq!(header(&response, "Access-Control-Allow-Origin"), None);
}

#[test]
fn everything_else_is_turned_away_with_nothing_in_it() {
    let canvas_id = Uuid::new_v4();
    let text_id = Uuid::new_v4();
    let (_dir, store) = store_with(&[
        show(canvas_id, 2, "canvas", PAGE),
        show(text_id, 1, "text", "# Private notes"),
    ]);
    let host = Some("127.0.0.1:47821");
    let good = format!("/{TOKEN}/{canvas_id}/2");
    let other_token = format!("/{}/{canvas_id}/2", "f".repeat(32));
    let cases: Vec<(&str, Option<&str>, String, u16)> = vec![
        ("GET", host, format!("/{TOKEN}/{text_id}/1"), 404), // a text panel is never a page
        ("GET", host, format!("/{TOKEN}/{canvas_id}/1"), 404), // not the revision held
        ("GET", host, format!("/{TOKEN}/{canvas_id}/3"), 404),
        ("GET", host, format!("/{TOKEN}/{}/2", Uuid::new_v4()), 404),
        ("GET", host, other_token, 404),
        ("GET", host, format!("/{canvas_id}/2"), 404),
        ("GET", host, format!("{good}/"), 404),
        ("GET", host, format!("{good}/extra"), 404),
        ("GET", host, format!("{good}?x=1"), 404),
        ("GET", host, format!("/{TOKEN}/{canvas_id}/02"), 404),
        (
            "GET",
            host,
            format!("/{TOKEN}/{}/2", canvas_id.simple()),
            404,
        ), // one address a page
        (
            "GET",
            host,
            format!("/{TOKEN}/{}/2", canvas_id.to_string().to_uppercase()),
            404,
        ),
        ("GET", host, format!("/{TOKEN}/{canvas_id}/+2"), 404),
        ("GET", host, format!("/{TOKEN}/{canvas_id}/two"), 404),
        ("GET", host, "/".to_owned(), 404),
        ("GET", host, String::new(), 404),
        ("GET", Some("localhost:47821"), good.clone(), 404), // a name that could be re-pointed
        ("GET", Some("evil.example"), good.clone(), 404),
        ("GET", Some("127.0.0.1:47822"), good.clone(), 404),
        ("GET", Some("127.0.0.1"), good.clone(), 404),
        ("GET", None, good.clone(), 404),
        ("POST", host, good.clone(), 405),
        ("HEAD", host, good.clone(), 405),
        ("OPTIONS", host, good.clone(), 405),
    ];
    for (method, host, path, status) in cases {
        let response = respond(&store, TOKEN, 47821, method, host, &path);
        assert_eq!(response.status, status, "{method} {host:?} {path}");
        assert!(response.body.is_empty(), "{method} {path}");
        assert_eq!(response.served, None);
        assert_eq!(header(&response, "Content-Security-Policy"), Some(POLICY));
        assert_eq!(header(&response, "X-Content-Type-Options"), Some("nosniff"));
    }
}

async fn fetch(port: u16, request: &str) -> String {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut answer = Vec::new();
    stream.read_to_end(&mut answer).await.unwrap();
    String::from_utf8_lossy(&answer).into_owned()
}

async fn started(events: &[BoardEvent]) -> (tempfile::TempDir, Arc<CanvasServer>) {
    let (dir, store) = store_with(events);
    let server = canvas::serve(Arc::new(Mutex::new(store)), &[0])
        .await
        .unwrap();
    (dir, server)
}

#[tokio::test]
async fn the_server_answers_over_the_loopback_and_records_what_it_served() {
    let id = Uuid::new_v4();
    let (_dir, server) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let port = server.port();
    let url = server.url(id, 1);
    let path = url
        .strip_prefix(&format!("http://127.0.0.1:{port}"))
        .expect("an address on the loopback");
    assert_eq!(path.split('/').nth(1).unwrap().len(), 32);
    assert!(!server.take_served(id, 1));

    let answer = fetch(
        port,
        &format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"),
    )
    .await;
    assert!(answer.starts_with("HTTP/1.1 200 OK\r\n"), "{answer}");
    assert!(answer.contains(&format!("Content-Security-Policy: {POLICY}\r\n")));
    assert!(answer.contains("Connection: close\r\n"));
    assert!(answer.contains(&format!("Content-Length: {}\r\n", PAGE.len())));
    assert!(answer.ends_with(PAGE));
    assert!(!server.take_served(id, 2));
    assert!(server.take_served(id, 1));
    // Once: a later "loaded" needs a page really handed out again.
    assert!(!server.take_served(id, 1));
}

#[tokio::test]
async fn a_request_without_the_secret_gets_nothing_and_counts_for_nothing() {
    let id = Uuid::new_v4();
    let (_dir, server) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let port = server.port();
    let guess = format!("/{}/{id}/1", "0".repeat(32));
    let answer = fetch(
        port,
        &format!("GET {guess} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"),
    )
    .await;
    assert!(answer.starts_with("HTTP/1.1 404 "), "{answer}");
    assert!(!answer.contains("Fictional"));
    assert!(!server.take_served(id, 1));
}

#[tokio::test]
async fn each_run_has_its_own_secret() {
    let id = Uuid::new_v4();
    let (_a, first) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let (_b, second) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let secret = |s: &CanvasServer| s.url(id, 1).split('/').nth(3).unwrap().to_owned();
    assert_ne!(secret(&first), secret(&second));
}

#[tokio::test]
async fn junk_and_oversized_requests_are_closed_without_a_page() {
    let id = Uuid::new_v4();
    let (_dir, server) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let port = server.port();
    for request in [
        "\r\n\r\n".to_owned(),
        "GARBAGE\r\n\r\n".to_owned(),
        format!(
            "GET /{} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n",
            "a".repeat(20_000)
        ),
    ] {
        let answer = fetch(port, &request).await;
        assert!(
            !answer.contains("Fictional"),
            "{}",
            &request[..request.len().min(40)]
        );
        assert!(!answer.starts_with("HTTP/1.1 200"));
    }
    // And it still serves afterwards.
    let path = server
        .url(id, 1)
        .replace(&format!("http://127.0.0.1:{port}"), "");
    let answer = fetch(
        port,
        &format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"),
    )
    .await;
    assert!(answer.starts_with("HTTP/1.1 200 OK"));
}

#[tokio::test(start_paused = true)]
async fn a_caller_that_says_nothing_is_dropped() {
    let id = Uuid::new_v4();
    let (_dir, server) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let mut stream = TcpStream::connect(("127.0.0.1", server.port()))
        .await
        .unwrap();
    stream.write_all(b"GET /").await.unwrap();
    tokio::time::sleep(std::time::Duration::from_secs(30)).await;
    let mut rest = Vec::new();
    let read = stream.read_to_end(&mut rest).await;
    assert!(read.is_ok() && !String::from_utf8_lossy(&rest).contains("200 OK"));
}

#[tokio::test]
async fn it_takes_the_next_port_when_one_is_in_use_and_none_is_an_error() {
    let taken = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let busy = taken.local_addr().unwrap().port();
    let (_dir, store) = store_with(&[]);
    let store = Arc::new(Mutex::new(store));
    let server = canvas::serve(store.clone(), &[busy, 0]).await.unwrap();
    assert_ne!(server.port(), busy);
    assert!(canvas::serve(store, &[busy]).await.is_err());
}

#[tokio::test(start_paused = true)]
async fn silent_callers_cannot_keep_a_real_request_waiting_long() {
    let id = Uuid::new_v4();
    let (_dir, server) = started(&[show(id, 1, "canvas", PAGE)]).await;
    let port = server.port();
    let mut idle = Vec::new();
    for _ in 0..40 {
        idle.push(TcpStream::connect(("127.0.0.1", port)).await.unwrap());
    }
    let path = server
        .url(id, 1)
        .replace(&format!("http://127.0.0.1:{port}"), "");
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n");
    let began = tokio::time::Instant::now();
    let answer = fetch(port, &request).await;
    assert!(answer.starts_with("HTTP/1.1 200 OK"));
    assert!(began.elapsed() < std::time::Duration::from_secs(1));
}

const CONF: &str = include_str!("../../../app/src-tauri/tauri.conf.json");
const BOARD_HTML: &str = include_str!("../../../app/board.html");
const CANVAS_TS: &str = include_str!("../../../app/src/board/canvas.ts");

fn frame_sources(policy: &str) -> Vec<&str> {
    policy
        .split(';')
        .map(str::trim)
        .find_map(|d| d.strip_prefix("frame-src "))
        .expect("a frame-src")
        .split_whitespace()
        .collect()
}

#[test]
fn the_board_page_may_frame_the_three_canvas_ports_and_nothing_else() {
    assert_eq!(canvas::PORTS, [47821, 47822, 47823]);
    let ours: Vec<String> = canvas::PORTS
        .iter()
        .map(|p| format!("http://127.0.0.1:{p}"))
        .collect();
    // The app-wide policy (a response header) allows them, beside the main window's YouTube.
    let conf: serde_json::Value = serde_json::from_str(CONF).unwrap();
    let app_wide = frame_sources(conf["app"]["security"]["csp"].as_str().unwrap());
    for origin in &ours {
        assert!(app_wide.contains(&origin.as_str()), "{origin}");
    }
    // A page of the app in a frame would be local to Tauri, with the app's rights.
    for never in [
        "'self'",
        "*",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri:",
    ] {
        assert!(!app_wide.contains(&never), "{never}");
    }
    // The Board's own page narrows it to the three: a Canvas page must not be able to turn
    // its frame into a YouTube page.
    let meta = BOARD_HTML
        .split("http-equiv=\"Content-Security-Policy\"")
        .nth(1)
        .and_then(|rest| rest.split("content=\"").nth(1))
        .and_then(|rest| rest.split('"').next())
        .expect("a policy in board.html");
    assert_eq!(
        frame_sources(meta),
        ours.iter().map(String::as_str).collect::<Vec<_>>()
    );
}

#[test]
fn the_frame_demands_the_same_policy_the_server_sends() {
    assert!(
        CANVAS_TS.contains(&format!("\"{POLICY}\"")),
        "canvas.ts must carry POLICY word for word"
    );
}

#[test]
fn the_board_window_is_built_with_the_seal() {
    let conf: serde_json::Value = serde_json::from_str(CONF).unwrap();
    let board = conf["app"]["windows"]
        .as_array()
        .unwrap()
        .iter()
        .find(|w| w["label"] == "board")
        .unwrap();
    // Built in code, so it can have its own browser data folder and the all-frames script.
    assert_eq!(board["create"], false);
    let args = board["additionalBrowserArgs"].as_str().unwrap();
    assert_eq!(args, canvas::BOARD_BROWSER_ARGS);
    for needed in [
        "--proxy-server=http://127.0.0.1:1",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--enable-features=NoStatePrefetchHoldback",
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection",
    ] {
        assert!(args.contains(needed), "{needed}");
    }
    assert!(!args.contains("autoplay"));
    // The main window keeps its own arguments, untouched.
    let main = &conf["app"]["windows"][0];
    assert_eq!(main["label"], "main");
    assert_eq!(
        main["additionalBrowserArgs"],
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required"
    );
    for gone in [
        "RTCPeerConnection",
        "speechSynthesis",
        "AudioContext",
        "window.top !== window",
    ] {
        assert!(canvas::SEAL_SCRIPT.contains(gone), "{gone}");
    }
}
