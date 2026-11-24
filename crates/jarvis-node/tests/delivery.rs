//! The runtime's delivery rules (spec §8.2), over a real socket against the fake gateway.

mod support;

use std::sync::Arc;
use std::time::Duration;

use jarvis_node::client::{wait_for, ClientConfig, LinkStatus};
use jarvis_node::delivery::{Inbox, ItemFate};
use jarvis_node::journal::Journal;
use jarvis_node::keystore::MemoryKeyStore;
use jarvis_node::node::{self, NodeDeps, NodeHandle, ReplyEvent, UiEvent};
use serde_json::{json, Value};
use support::authz::{keyset_object, keyset_payload, live_action, signed};
use support::gateway::{code_for, now, Gw};
use support::net::{accept, recv, recv_within, send, Ws};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use uuid::Uuid;

// ---------------------------------------------------------------- the seen-list itself

fn inbox(path: &std::path::Path) -> Inbox {
    Inbox::new(Arc::new(std::sync::Mutex::new(
        Journal::open(path).unwrap(),
    )))
}

#[test]
fn item_shown_once_across_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("journal.jsonl");
    let id = Uuid::new_v4();
    assert_eq!(inbox(&path).on_item(id).unwrap(), ItemFate::Show);
    assert_eq!(
        inbox(&path).on_item(id).unwrap(),
        ItemFate::AlreadySeen,
        "after a restart"
    );
}

#[test]
fn cleared_before_item_never_shows() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("journal.jsonl");
    let id = Uuid::new_v4();
    assert!(!inbox(&path).on_cleared(id).unwrap(), "never shown");
    assert_eq!(inbox(&path).on_item(id).unwrap(), ItemFate::Cleared);
}

// ---------------------------------------------------------------- the runtime

struct Rig {
    listener: TcpListener,
    node: NodeHandle,
    ui: mpsc::Receiver<UiEvent>,
    gw: Gw,
    ws: Ws,
    node_id: Uuid,
    dir: tempfile::TempDir,
}

fn core_public() -> [u8; 32] {
    support::test_key("core-1").verifying_key().to_bytes()
}

/// An enrolled node, connected to the fake gateway.
async fn rig() -> Rig {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let cfg = ClientConfig::new(listener.local_addr().unwrap().to_string(), dir.path());
    let (ui_tx, ui) = mpsc::channel(64);
    let deps = NodeDeps {
        keystore: Arc::new(MemoryKeyStore::default()),
        ui: ui_tx,
    };
    let node = node::start(cfg, deps).unwrap();
    let mut gw = Gw::new("gw-1", "gw-1");
    let code = code_for(&core_public(), &gw.public());
    let handle = node.clone();
    let enrolment = tokio::spawn(async move { handle.enrol(&code).await });
    let mut ws = accept(&listener).await;
    send(&mut ws, gw.challenge(now())).await;
    assert_eq!(recv(&mut ws).await["kind"], "enrol");
    let node_id = Uuid::new_v4();
    send(
        &mut ws,
        gw.welcome(node_id, Some(("core-1", core_public())), now()),
    )
    .await;
    enrolment.await.unwrap().unwrap();
    Rig {
        listener,
        node,
        ui,
        gw,
        ws,
        node_id,
        dir,
    }
}

impl Rig {
    async fn push(&mut self, kind: &str, payload: Value) {
        let frame = self.gw.frame(kind, self.node_id, payload, now());
        send(&mut self.ws, frame).await;
    }

    /// The next UI event that isn't a link-status or summary update.
    async fn ui_event(&mut self, within: Duration) -> Option<UiEvent> {
        let deadline = tokio::time::Instant::now() + within;
        loop {
            match tokio::time::timeout_at(deadline, self.ui.recv()).await {
                Ok(Some(UiEvent::Link(_) | UiEvent::Summary(_))) => continue,
                Ok(other) => return other,
                Err(_) => return None,
            }
        }
    }
}

fn item(outbox_id: Uuid, priority: &str) -> Value {
    json!({
        "outbox_id": outbox_id, "priority": priority, "kind": "reminder.due",
        "body": "Fictional test reminder", "expires_at": null, "authz": null,
    })
}

#[tokio::test]
async fn an_item_is_received_shown_once_and_acked_when_shown() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push("outbox.item", item(id, "TODAY")).await;
    let received = recv(&mut r.ws).await;
    assert_eq!(
        (received["kind"].as_str(), &received["payload"]),
        (Some("received"), &json!({"outbox_id": id}))
    );
    support::assert_conforms("received", &received["payload"]);
    match r.ui_event(Duration::from_secs(2)).await {
        Some(UiEvent::Notice {
            outbox_id,
            priority,
            ..
        }) => assert_eq!((outbox_id, priority.as_str()), (id, "TODAY")),
        other => panic!("{other:?}"),
    }
    r.node.shown(id).await.unwrap();
    let ack = recv(&mut r.ws).await;
    assert_eq!(
        (ack["kind"].as_str(), &ack["payload"]),
        (Some("ack"), &json!({"outbox_id": id}))
    );
    support::assert_conforms("ack", &ack["payload"]);

    // A re-send (same outbox_id) is confirmed again but never shown twice.
    r.push("outbox.item", item(id, "TODAY")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    assert_eq!(r.ui_event(Duration::from_millis(300)).await, None);
}

#[tokio::test]
async fn a_silent_item_is_acked_not_shown() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push("outbox.item", item(id, "SILENT")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    let ack = recv(&mut r.ws).await;
    assert_eq!(
        (ack["kind"].as_str(), &ack["payload"]),
        (Some("ack"), &json!({"outbox_id": id}))
    );
    assert_eq!(r.ui_event(Duration::from_millis(300)).await, None);
}

#[tokio::test]
async fn a_cleared_item_is_never_shown_afterwards() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push(
        "outbox.cleared",
        json!({"outbox_id": id, "reason": "SUPERSEDED"}),
    )
    .await;
    assert_eq!(
        r.ui_event(Duration::from_secs(2)).await,
        Some(UiEvent::NoticeCleared {
            outbox_id: id,
            reason: "SUPERSEDED".into()
        })
    );
    r.push("outbox.item", item(id, "URGENT")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received", "still confirmed");
    assert_eq!(r.ui_event(Duration::from_millis(300)).await, None);
}

#[tokio::test]
async fn an_item_with_authz_waits_for_its_handler_and_is_not_marked_seen() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    let mut card = item(id, "URGENT");
    card["authz"] = signed(
        "core-1",
        "core-1",
        live_action(r.node_id, Uuid::new_v4(), now()),
    );
    r.push("outbox.item", card).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    assert_eq!(r.ui_event(Duration::from_millis(300)).await, None);
    let journal = std::fs::read_to_string(r.dir.path().join(node::JOURNAL_FILE)).unwrap();
    assert!(!journal.contains(&id.to_string()), "not in the seen-list");
}

#[tokio::test]
async fn speech_chunks_are_received_and_broadcast() {
    let mut r = rig().await;
    let mut replies = r.node.subscribe_replies();
    let reply_id = Uuid::new_v4();
    let chunk = json!({"reply_id": reply_id, "seq": 1, "type": "SPEECH", "text": "Fictional reply.", "authz": null, "final": true});
    r.push("reply.chunk", chunk).await;
    let received = recv(&mut r.ws).await;
    assert_eq!(received["payload"], json!({"reply_id": reply_id, "seq": 1}));
    support::assert_conforms("received", &received["payload"]);
    let got = tokio::time::timeout(Duration::from_secs(2), replies.recv())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        got,
        ReplyEvent::Speech {
            reply_id,
            seq: 1,
            text: "Fictional reply.".into(),
            final_: true
        }
    );
}

#[tokio::test]
async fn live_actions_are_checked_before_broadcast() {
    let mut r = rig().await;
    let mut replies = r.node.subscribe_replies();
    // Another node's action, and an action signed only by the gateway: refused, not broadcast.
    let other = signed(
        "core-1",
        "core-1",
        live_action(Uuid::new_v4(), Uuid::new_v4(), now()),
    );
    r.push("node.action", json!({"authz": other})).await;
    let gateway_only = signed(
        "gw-1",
        "core-1",
        live_action(r.node_id, Uuid::new_v4(), now()),
    );
    r.push("node.action", json!({"authz": gateway_only})).await;
    // A valid one: broadcast.
    let reply_id = Uuid::new_v4();
    let good = signed("core-1", "core-1", live_action(r.node_id, reply_id, now()));
    r.push("node.action", json!({"authz": good})).await;
    let got = tokio::time::timeout(Duration::from_secs(2), replies.recv())
        .await
        .unwrap()
        .unwrap();
    match got {
        ReplyEvent::Action(obj) => assert_eq!(
            obj.reply_id,
            Some(reply_id),
            "the first broadcast is the valid one"
        ),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn an_action_under_an_untrusted_core_key_asks_reenrol() {
    let mut r = rig().await;
    let rogue = signed(
        "unknown-1",
        "core-9",
        live_action(r.node_id, Uuid::new_v4(), now()),
    );
    r.push("node.action", json!({"authz": rogue})).await;
    let mut status = r.node.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::NeedsReenrol,
            Duration::from_secs(2)
        )
        .await
    );
    let again = tokio::time::timeout(Duration::from_millis(1500), r.listener.accept()).await;
    assert!(again.is_err(), "no reconnect until a new code");
}

#[tokio::test]
async fn a_keyset_is_saved_before_it_is_acked() {
    let mut r = rig().await;
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", Some(r.node_id), now()),
    );
    let outbox_id = Uuid::new_v4();
    r.push("keyset", keyset_payload(outbox_id, &authz)).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    assert_eq!(recv(&mut r.ws).await["kind"], "ack");
    let saved = jarvis_node::state::load(&r.dir.path().join(jarvis_node::client::STATE_FILE))
        .unwrap()
        .unwrap();
    assert!(
        saved.trust.core.contains_key("core-2"),
        "on disk by the time it was acked"
    );
    assert!(r.node.state().borrow().trust.core.contains_key("core-2"));
}

#[tokio::test]
async fn a_keyset_that_cannot_be_saved_is_not_acked() {
    let mut r = rig().await;
    // Make the next state save fail: its temporary file's path is taken by a folder.
    std::fs::create_dir(r.dir.path().join("node-state.json.tmp")).unwrap();
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", None, now()),
    );
    r.push("keyset", keyset_payload(Uuid::new_v4(), &authz))
        .await;
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_secs(2)).await,
        None,
        "closed without received or ack"
    );
}
