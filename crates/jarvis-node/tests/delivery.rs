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
async fn an_item_with_authz_is_not_received_until_it_can_be_handled() {
    // No `received`, so the gateway's sweep keeps offering it (Tasks 7 and 8 handle it).
    let mut r = rig().await;
    let id = Uuid::new_v4();
    let mut card = item(id, "URGENT");
    card["authz"] = signed(
        "core-1",
        "core-1",
        live_action(r.node_id, Uuid::new_v4(), now()),
    );
    r.push("outbox.item", card).await;
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_millis(500)).await,
        None,
        "nothing sent back"
    );
    assert_eq!(r.ui_event(Duration::from_millis(100)).await, None);
    let journal = std::fs::read_to_string(r.dir.path().join(node::JOURNAL_FILE)).unwrap();
    assert!(!journal.contains(&id.to_string()), "not in the seen-list");
}

/// Reconnect the rig's node (the gateway closes 4401), through a new welcome; returns the
/// frames the node sent after the welcome, within `within`.
async fn reconnect(r: &mut Rig, within: Duration) -> Vec<Value> {
    support::net::close(&mut r.ws, 4401).await;
    r.gw = Gw::new("gw-1", "gw-1");
    r.ws = accept(&r.listener).await;
    send(&mut r.ws, r.gw.challenge(now())).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "hello");
    let welcome = r.gw.welcome(r.node_id, None, now());
    send(&mut r.ws, welcome).await;
    let mut got = Vec::new();
    while let Some(frame) = recv_within(&mut r.ws, within).await {
        got.push(frame);
    }
    got
}

fn acks(frames: &[Value]) -> Vec<Value> {
    frames
        .iter()
        .filter(|f| f["kind"] == "ack")
        .map(|f| f["payload"]["outbox_id"].clone())
        .collect()
}

#[tokio::test]
async fn an_ack_is_re_sent_after_a_reconnect_until_cleared() {
    // The gateway never re-sends an item it has a `received` for, so the node keeps owing
    // the ack (e.g. the app died, or the link dropped, before it arrived).
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push("outbox.item", item(id, "URGENT")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    r.ui_event(Duration::from_secs(2)).await.expect("shown");
    r.node.shown(id).await.unwrap();
    assert_eq!(recv(&mut r.ws).await["kind"], "ack"); // pretend this one was lost
    let got = reconnect(&mut r, Duration::from_millis(500)).await;
    assert_eq!(acks(&got), vec![json!(id)], "re-sent after the welcome");
    // Once the server clears it here, it is owed no more.
    r.push(
        "outbox.cleared",
        json!({"outbox_id": id, "reason": "ACKNOWLEDGED"}),
    )
    .await;
    r.ui_event(Duration::from_secs(2)).await;
    let got = reconnect(&mut r, Duration::from_millis(500)).await;
    assert!(acks(&got).is_empty(), "{got:?}");
}

#[tokio::test]
async fn shown_while_disconnected_is_acked_on_reconnect() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push("outbox.item", item(id, "TODAY")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    r.ui_event(Duration::from_secs(2)).await.expect("shown");
    support::net::close(&mut r.ws, 4401).await;
    let mut status = r.node.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s != LinkStatus::Connected,
            Duration::from_secs(2)
        )
        .await
    );
    // Answered at once, even while the node is dialling the (unanswering) gateway.
    let t = std::time::Instant::now();
    assert!(r.node.shown(id).await.is_err(), "not connected right now");
    assert!(
        t.elapsed() < Duration::from_millis(500),
        "took {:?}",
        t.elapsed()
    );
    // The reconnect (driven by the node's own backoff) carries the owed ack.
    r.gw = Gw::new("gw-1", "gw-1");
    r.ws = accept(&r.listener).await;

    send(&mut r.ws, r.gw.challenge(now())).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "hello");
    let welcome = r.gw.welcome(r.node_id, None, now());
    send(&mut r.ws, welcome).await;
    let ack = recv(&mut r.ws).await;
    assert_eq!(
        (ack["kind"].as_str(), &ack["payload"]),
        (Some("ack"), &json!({"outbox_id": id}))
    );
}

#[tokio::test]
async fn a_re_sent_item_whose_ack_is_owed_is_acked_again() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    r.push("outbox.item", item(id, "TODAY")).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    r.ui_event(Duration::from_secs(2)).await.expect("shown");
    r.node.shown(id).await.unwrap();
    assert_eq!(recv(&mut r.ws).await["kind"], "ack");
    r.push("outbox.item", item(id, "TODAY")).await; // the received was lost: a sweep re-send
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    let ack = recv(&mut r.ws).await;
    assert_eq!(
        (ack["kind"].as_str(), &ack["payload"]),
        (Some("ack"), &json!({"outbox_id": id}))
    );
    assert_eq!(
        r.ui_event(Duration::from_millis(300)).await,
        None,
        "not shown again"
    );
}

#[test]
fn owed_acks_expire_after_the_retry_window() {
    use jarvis_node::delivery::ACK_RETRY_WINDOW;
    let dir = tempfile::tempdir().unwrap();
    let inbox = inbox(&dir.path().join("journal.jsonl"));
    let now = now();
    let (old, fresh) = (Uuid::new_v4(), Uuid::new_v4());
    inbox
        .mark_acked(old, now - ACK_RETRY_WINDOW - chrono::Duration::seconds(1))
        .unwrap();
    inbox
        .mark_acked(fresh, now - chrono::Duration::hours(1))
        .unwrap();
    assert_eq!(inbox.owed_acks(now), vec![fresh]);
    assert!(!inbox.ack_owed(old, now) && inbox.ack_owed(fresh, now));
    inbox.on_cleared(fresh).unwrap();
    assert!(inbox.owed_acks(now).is_empty());
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
    r.push("node.action", json!({"authz": other, "seq": 2}))
        .await;
    let gateway_only = signed(
        "gw-1",
        "core-1",
        live_action(r.node_id, Uuid::new_v4(), now()),
    );
    r.push("node.action", json!({"authz": gateway_only, "seq": 2}))
        .await;
    // A valid one: broadcast.
    let reply_id = Uuid::new_v4();
    let good = signed("core-1", "core-1", live_action(r.node_id, reply_id, now()));
    r.push("node.action", json!({"authz": good, "seq": 2}))
        .await;
    let got = tokio::time::timeout(Duration::from_secs(2), replies.recv())
        .await
        .unwrap()
        .unwrap();
    match got {
        ReplyEvent::Action { seq, authz } => assert_eq!(
            (seq, authz.reply_id),
            (2, Some(reply_id)),
            "the first broadcast is the valid one"
        ),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn a_node_action_needs_a_seq_and_a_live_authz() {
    // R12-36: `{authz, seq}`, seq >= 1, nothing else; approved actions come in outbox items.
    let mut r = rig().await;
    let mut replies = r.node.subscribe_replies();
    let me = r.node_id;
    let good = || signed("core-1", "core-1", live_action(me, Uuid::new_v4(), now()));
    r.push("node.action", json!({"authz": good()})).await;
    r.push("node.action", json!({"authz": good(), "seq": 0}))
        .await;
    r.push("node.action", json!({"authz": good(), "seq": "2"}))
        .await;
    r.push(
        "node.action",
        json!({"authz": good(), "seq": 2, "extra": 1}),
    )
    .await;
    let mut approved = live_action(r.node_id, Uuid::new_v4(), now());
    approved["reply_id"] = Value::Null;
    approved["execution_key"] = json!(Uuid::new_v4());
    approved["approval_id"] = json!(Uuid::new_v4());
    r.push(
        "node.action",
        json!({"authz": signed("core-1", "core-1", approved), "seq": 2}),
    )
    .await;
    let reply_id = Uuid::new_v4();
    let last = signed("core-1", "core-1", live_action(r.node_id, reply_id, now()));
    r.push("node.action", json!({"authz": last, "seq": 7}))
        .await;
    let got = tokio::time::timeout(Duration::from_secs(2), replies.recv())
        .await
        .unwrap()
        .unwrap();
    match got {
        ReplyEvent::Action { seq, authz } => {
            assert_eq!(
                (seq, authz.reply_id),
                (7, Some(reply_id)),
                "only the last one"
            )
        }
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
    r.push("node.action", json!({"authz": rogue, "seq": 2}))
        .await;
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

#[tokio::test]
async fn server_brain_over_the_real_node() {
    // Task 6 end to end on the wire: utterance out; speech, node.action and receipt through
    // the real NodeHandle (the fictional phrases only).
    use jarvis_core::action::Action;
    use jarvis_core::brain::{Brain, BrainEvent, BrainRequest};
    use jarvis_node::server_brain::ServerBrain;
    let mut r = rig().await;
    let brain = ServerBrain::new(Arc::new(r.node.clone()));
    let (tx, mut events) = mpsc::channel(16);
    let request = BrainRequest {
        utterance: "show me the fictional news".into(),
        screen_summary: String::new(),
        history: vec![],
        tools: vec![],
    };
    let done = tokio::spawn(async move { brain.think(request, tx).await });
    let utterance = recv(&mut r.ws).await;
    support::assert_envelope_conforms(&utterance);
    assert_eq!(
        (utterance["kind"].as_str(), &utterance["payload"]),
        (
            Some("utterance"),
            &json!({"text": "show me the fictional news", "screen_summary": null})
        )
    );
    let reply_id = Uuid::new_v4();
    let chunk = |seq: i64, text: &str, last: bool| json!({"reply_id": reply_id, "seq": seq, "type": "SPEECH", "text": text, "authz": null, "final": last});
    r.push("reply.chunk", chunk(1, "Opening the news.", false))
        .await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    let mut obj = live_action(r.node_id, reply_id, now());
    obj["args"] = json!({"tool": "show_news", "input": {"channel": "headlines"}});
    r.push(
        "node.action",
        json!({"authz": signed("core-1", "core-1", obj), "seq": 2}),
    )
    .await;
    let receipt = recv(&mut r.ws).await;
    support::assert_envelope_conforms(&receipt);
    assert_eq!(receipt["kind"], "action.receipt");
    assert_eq!(
        receipt["payload"],
        json!({"reply_id": reply_id, "seq": 2, "result": "OK", "evidence": {}})
    );
    r.push("reply.chunk", chunk(3, "There it is.", true)).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    done.await.unwrap().unwrap();
    let mut got = Vec::new();
    while let Some(ev) = events.recv().await {
        got.push(ev);
    }
    assert_eq!(
        got,
        vec![
            BrainEvent::Speech("Opening the news.".into()),
            BrainEvent::Action(Action::new("show_news", json!({"channel": "headlines"}))),
            BrainEvent::Speech("There it is.".into()),
        ]
    );
}
