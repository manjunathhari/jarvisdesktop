//! The connection driver over a real socket, against a scripted fake gateway on 127.0.0.1.

mod support;

use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use jarvis_node::client::{self, wait_for, ClientConfig, EnrolError, LinkStatus, NotConnected};
use jarvis_node::keystore::{KeyStore, MemoryKeyStore};
use jarvis_node::session::Output;
use jarvis_node::state;
use serde_json::{json, Value};
use support::gateway::{code_for, now, Gw};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;
use uuid::Uuid;

type Ws = WebSocketStream<TcpStream>;

fn core_public() -> [u8; 32] {
    support::test_key("core-1").verifying_key().to_bytes()
}

async fn accept(listener: &TcpListener) -> Ws {
    let (tcp, _) = tokio::time::timeout(Duration::from_secs(10), listener.accept())
        .await
        .expect("the node connects")
        .unwrap();
    tokio_tungstenite::accept_async(tcp).await.unwrap()
}

async fn recv(ws: &mut Ws) -> Value {
    loop {
        match tokio::time::timeout(Duration::from_secs(5), ws.next())
            .await
            .expect("a frame")
        {
            Some(Ok(Message::Text(t))) => return serde_json::from_str(t.as_str()).unwrap(),
            Some(Ok(_)) => continue,
            other => panic!("connection ended: {other:?}"),
        }
    }
}

async fn send(ws: &mut Ws, frame: String) {
    ws.send(Message::text(frame)).await.unwrap();
}

async fn close(ws: &mut Ws, code: u16) {
    let _ = ws
        .send(Message::Close(Some(CloseFrame {
            code: CloseCode::from(code),
            reason: "test".into(),
        })))
        .await;
}

struct Node {
    client: client::Client,
    keystore: Arc<MemoryKeyStore>,
    frames: mpsc::UnboundedReceiver<Output>,
    dir: tempfile::TempDir,
}

async fn start(heartbeat: Duration) -> (TcpListener, Node) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = ClientConfig::new(listener.local_addr().unwrap().to_string(), dir.path());
    cfg.heartbeat_timeout = heartbeat;
    let keystore = Arc::new(MemoryKeyStore::default());
    let (tx, frames) = mpsc::unbounded_channel();
    let client = client::spawn(cfg, keystore.clone(), tx);
    (
        listener,
        Node {
            client,
            keystore,
            frames,
            dir,
        },
    )
}

/// Enrol the node through the fake gateway; returns the gateway's socket and the node ID.
async fn enrol(listener: &TcpListener, node: &Node, gw: &mut Gw) -> (Ws, Uuid) {
    let code = code_for(&core_public(), &gw.public());
    let client = node.client.clone();
    let enrolment = tokio::spawn(async move { client.enrol(&code).await });
    let mut ws = accept(listener).await;
    send(&mut ws, gw.challenge(now())).await;
    let enrol = recv(&mut ws).await;
    assert_eq!(enrol["kind"], "enrol");
    let node_id = Uuid::new_v4();
    send(
        &mut ws,
        gw.welcome(node_id, Some(("core-1", core_public())), now()),
    )
    .await;
    enrolment.await.unwrap().expect("enrolment succeeds");
    (ws, node_id)
}

#[tokio::test]
async fn not_enrolled_waits_and_refuses_sends() {
    let (_listener, node) = start(Duration::from_secs(95)).await;
    let mut status = node.client.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::NotEnrolled,
            Duration::from_secs(2)
        )
        .await
    );
    let before = std::time::Instant::now();
    assert_eq!(node.client.send("ping", json!({})).await, Err(NotConnected));
    assert!(
        before.elapsed() < Duration::from_millis(200),
        "fails at once"
    );
    assert_eq!(
        node.client.enrol("not a code").await,
        Err(EnrolError::BadCode)
    );
}

#[tokio::test]
async fn enrols_saves_and_reconnects_after_4401() {
    let (listener, node) = start(Duration::from_secs(95)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let (mut ws, node_id) = enrol(&listener, &node, &mut gw).await;
    // The key and the state are saved only now, after the verified welcome.
    assert!(node.keystore.load().unwrap().is_some());
    let saved = state::load(&node.dir.path().join(client::STATE_FILE))
        .unwrap()
        .unwrap();
    assert_eq!(saved.node_id, Some(node_id));
    assert!(saved.trust.core.contains_key("core-1") && saved.trust.gateway.contains_key("gw-1"));
    let mut status = node.client.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::Connected,
            Duration::from_secs(2)
        )
        .await
    );

    // 4401 (any reason) means reconnect, never re-enrol.
    close(&mut ws, 4401).await;
    let mut gw2 = Gw::new("gw-1", "gw-1"); // same key, new connection: seq starts again
    let mut ws = accept(&listener).await;
    send(&mut ws, gw2.challenge(now())).await;
    let hello = recv(&mut ws).await;
    assert_eq!(hello["kind"], "hello");
    assert_eq!(hello["seq"], 1);
    assert_eq!(hello["node_id"], node_id.to_string());
    send(&mut ws, gw2.welcome(node_id, None, now())).await;
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::Connected,
            Duration::from_secs(2)
        )
        .await
    );
    // The app's messages now go out signed, with the next seq.
    node.client
        .send(
            "utterance",
            json!({"text": "fictional test phrase", "screen_summary": null}),
        )
        .await
        .unwrap();
    let utterance = recv(&mut ws).await;
    assert_eq!(
        (utterance["kind"].as_str(), utterance["seq"].as_i64()),
        (Some("utterance"), Some(2))
    );
}

#[tokio::test]
async fn stops_on_4409_and_reports_needs_reenrol() {
    let (listener, node) = start(Duration::from_secs(95)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let (mut ws, _) = enrol(&listener, &node, &mut gw).await;
    close(&mut ws, 4409).await;
    let mut status = node.client.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::NeedsReenrol,
            Duration::from_secs(2)
        )
        .await
    );
    // It doesn't come back (the first backoff would be 1 s).
    let again = tokio::time::timeout(Duration::from_millis(2500), listener.accept()).await;
    assert!(again.is_err(), "no reconnect after 4409");
    // The old identity is kept until a new enrolment replaces it.
    assert!(node.keystore.load().unwrap().is_some());
}

#[tokio::test]
async fn enrol_failure_discards_the_new_key() {
    let (listener, node) = start(Duration::from_secs(95)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let code = code_for(&core_public(), &gw.public());
    let client = node.client.clone();
    let enrolment = tokio::spawn(async move { client.enrol(&code).await });
    let mut ws = accept(&listener).await;
    send(&mut ws, gw.challenge(now())).await;
    recv(&mut ws).await;
    // The gateway substitutes its own key for the core key.
    send(
        &mut ws,
        gw.welcome(Uuid::new_v4(), Some(("core-1", gw.public())), now()),
    )
    .await;
    assert_eq!(
        enrolment.await.unwrap(),
        Err(EnrolError::Refused("core_fingerprint_mismatch"))
    );
    assert!(node.keystore.load().unwrap().is_none(), "no key kept");
    assert!(
        !node.dir.path().join(client::STATE_FILE).exists(),
        "no state kept"
    );
    let mut status = node.client.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::NotEnrolled,
            Duration::from_secs(2)
        )
        .await
    );
}

#[tokio::test]
async fn missed_heartbeats_reconnect() {
    let (listener, node) = start(Duration::from_millis(300)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let (_silent, node_id) = enrol(&listener, &node, &mut gw).await;
    // The gateway goes silent: within the heartbeat window plus the 1 s backoff, it reconnects.
    let mut gw2 = Gw::new("gw-1", "gw-1");
    let mut ws = accept(&listener).await;
    send(&mut ws, gw2.challenge(now())).await;
    let hello = recv(&mut ws).await;
    assert_eq!(
        (hello["kind"].as_str(), hello["node_id"].as_str()),
        (Some("hello"), Some(node_id.to_string().as_str()))
    );
}

#[tokio::test]
async fn slow_consumer_does_not_stall_pong() {
    // R12-10: the app never reads its frames here, yet every ping is answered at once.
    let (listener, node) = start(Duration::from_secs(95)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let (mut ws, node_id) = enrol(&listener, &node, &mut gw).await;
    for _ in 0..200 {
        let item = json!({"outbox_id": Uuid::new_v4(), "reason": "EXPIRED"});
        send(&mut ws, gw.frame("outbox.cleared", node_id, item, now())).await;
    }
    let started = std::time::Instant::now();
    send(&mut ws, gw.frame("ping", node_id, json!({}), now())).await;
    let pong = recv(&mut ws).await;
    assert_eq!(pong["kind"], "pong");
    assert!(started.elapsed() < Duration::from_secs(1));
    drop(node.frames); // the frames were queued, never read
}

#[tokio::test]
async fn a_bad_signature_reconnects_and_frames_reach_the_app() {
    let (listener, mut node) = start(Duration::from_secs(95)).await;
    let mut gw = Gw::new("gw-1", "gw-1");
    let (mut ws, node_id) = enrol(&listener, &node, &mut gw).await;
    assert!(matches!(
        node.frames.recv().await,
        Some(Output::Ready { enrolled: true, .. })
    ));
    let item = json!({"outbox_id": Uuid::new_v4(), "reason": "EXPIRED"});
    send(
        &mut ws,
        gw.frame("outbox.cleared", node_id, item.clone(), now()),
    )
    .await;
    match node.frames.recv().await {
        Some(Output::Frame { kind, payload }) => {
            assert_eq!((kind.as_str(), payload), ("outbox.cleared", item))
        }
        other => panic!("{other:?}"),
    }
    let mut tampered: Value =
        serde_json::from_str(&gw.frame("ping", node_id, json!({}), now())).unwrap();
    tampered["payload"] = json!({"x": 1});
    send(&mut ws, tampered.to_string()).await;
    // Close and reconnect (not re-enrol).
    let mut gw2 = Gw::new("gw-1", "gw-1");
    let mut ws = accept(&listener).await;
    send(&mut ws, gw2.challenge(now())).await;
    assert_eq!(recv(&mut ws).await["kind"], "hello");
}
