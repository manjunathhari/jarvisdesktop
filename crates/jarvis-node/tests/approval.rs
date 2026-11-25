//! Plan B Task 7: approval cards on the desktop (§9.3, P2-E11).

mod support;

use std::collections::BTreeMap;
use std::time::Duration;

use chrono::{DateTime, Duration as Span, FixedOffset};
use jarvis_node::approval::{
    pending_voice_decision, Added, ApprovalCard, CardStore, Decision, Expired, VoiceDecision,
};
use jarvis_node::node::{DecideError, UiEvent};
use jarvis_node::rules::{CoreKeys, Rejected};
use serde_json::{json, Value};
use support::authz::{card_item, card_object, signed};
use support::gateway::now;
use support::net::{recv, recv_within};
use support::rig::rig;
use uuid::Uuid;

/// Now, to the second (signed objects carry whole-second times).
fn whole_now() -> DateTime<FixedOffset> {
    use chrono::Timelike;
    now().with_nanosecond(0).unwrap()
}

fn keys() -> CoreKeys {
    BTreeMap::from([(
        "core-1".to_owned(),
        support::test_key("core-1").verifying_key().to_bytes(),
    )])
}

/// A signed card item for `me`, valid for `life` from `at`.
fn card(me: Uuid, at: DateTime<FixedOffset>, life: Span) -> (Uuid, Uuid, Value) {
    let (outbox_id, approval_id) = (Uuid::new_v4(), Uuid::new_v4());
    let authz = signed(
        "core-1",
        "core-1",
        card_object(me, approval_id, at, at + life),
    );
    (outbox_id, approval_id, card_item(outbox_id, &authz))
}

#[test]
fn card_from_signed_item() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let (outbox_id, approval_id, item) = card(me, at, Span::minutes(10));
    let c = ApprovalCard::from_item(&item, &keys(), me, at).unwrap();
    assert_eq!((c.outbox_id, c.approval_id), (outbox_id, approval_id));
    assert_eq!(c.summary, "Delete test file `fictional.txt`?");
    assert_eq!(c.valid_until, at + Span::minutes(10));
    assert_eq!(c.card_sig, item["authz"]["sig"].as_str().unwrap());
}

#[test]
fn card_for_other_node_or_untrusted_key_refused() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let (_, _, other) = card(Uuid::new_v4(), at, Span::minutes(10));
    assert_eq!(
        ApprovalCard::from_item(&other, &keys(), me, at).unwrap_err(),
        Rejected("wrong_target")
    );
    let (_, _, item) = card(me, at, Span::minutes(10));
    assert_eq!(
        ApprovalCard::from_item(&item, &CoreKeys::new(), me, at).unwrap_err(),
        Rejected("untrusted_key_id")
    );
    // Signed by the wrong key under core-1's name.
    let mut forged = item.clone();
    forged["authz"] = signed("core-2", "core-1", item["authz"]["object"].clone());
    assert_eq!(
        ApprovalCard::from_item(&forged, &keys(), me, at).unwrap_err(),
        Rejected("bad_signature")
    );
    // A node.action is not a card.
    let mut action = item.clone();
    let mut obj = item["authz"]["object"].clone();
    obj["kind"] = json!("node.action");
    action["authz"] = signed("core-1", "core-1", obj);
    assert_eq!(
        ApprovalCard::from_item(&action, &keys(), me, at).unwrap_err(),
        Rejected("wrong_kind")
    );
    // No approval_id.
    let mut bare = item.clone();
    let mut obj = item["authz"]["object"].clone();
    obj["approval_id"] = Value::Null;
    bare["authz"] = signed("core-1", "core-1", obj);
    assert_eq!(
        ApprovalCard::from_item(&bare, &keys(), me, at).unwrap_err(),
        Rejected("malformed")
    );
    // Expired beyond the 30 s clock tolerance; inside it still shown.
    assert_eq!(
        ApprovalCard::from_item(
            &item,
            &keys(),
            me,
            at + Span::minutes(10) + Span::seconds(31)
        )
        .unwrap_err(),
        Rejected("expired")
    );
    assert!(ApprovalCard::from_item(
        &item,
        &keys(),
        me,
        at + Span::minutes(10) + Span::seconds(30)
    )
    .is_ok());
}

#[test]
fn seconds_left_counts_down_to_zero() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let (_, _, item) = card(me, at, Span::minutes(10));
    let c = ApprovalCard::from_item(&item, &keys(), me, at).unwrap();
    assert_eq!(c.seconds_left(at), 600);
    assert_eq!(c.seconds_left(at + Span::seconds(599)), 1);
    assert_eq!(c.seconds_left(at + Span::seconds(600)), 0);
    assert_eq!(c.seconds_left(at + Span::seconds(700)), 0);
}

#[test]
fn decision_after_expiry_is_not_sent() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let (_, _, item) = card(me, at, Span::minutes(10));
    let c = ApprovalCard::from_item(&item, &keys(), me, at).unwrap();
    assert!(c
        .decision(Decision::Approve, at + Span::seconds(598))
        .is_ok());
    // The last second is kept clear, so a decision can't reach the server after expiry.
    assert_eq!(
        c.decision(Decision::Approve, at + Span::seconds(599)),
        Err(Expired)
    );
    assert_eq!(
        c.decision(Decision::Approve, at + Span::seconds(600)),
        Err(Expired)
    );
    assert_eq!(
        c.decision(Decision::Deny, at + Span::minutes(11)),
        Err(Expired)
    );
}

#[test]
fn decision_carries_card_sig_and_conforms() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let (_, approval_id, item) = card(me, at, Span::minutes(10));
    let c = ApprovalCard::from_item(&item, &keys(), me, at).unwrap();
    for (d, word) in [(Decision::Approve, "APPROVED"), (Decision::Deny, "DENIED")] {
        let body = c.decision(d, at).unwrap();
        assert_eq!(
            body,
            json!({"approval_id": approval_id, "decision": word, "card_sig": item["authz"]["sig"]})
        );
        support::assert_conforms("approval.decision", &body);
    }
}

#[test]
fn cards_survive_a_restart_until_cleared_or_expired() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cards.json");
    let (a, _, item_a) = card(me, at, Span::minutes(10));
    let (b, _, item_b) = card(me, at, Span::minutes(1));
    {
        let store = CardStore::open(&path);
        store
            .add(ApprovalCard::from_item(&item_a, &keys(), me, at).unwrap())
            .unwrap();
        store
            .add(ApprovalCard::from_item(&item_b, &keys(), me, at).unwrap())
            .unwrap();
    }
    // After a restart: both re-verified against the trusted keys and shown again.
    let store = CardStore::open(&path);
    let ids: Vec<Uuid> = store
        .load(&keys(), me, at)
        .iter()
        .map(|c| c.outbox_id)
        .collect();
    assert_eq!(ids.len(), 2);
    assert!(ids.contains(&a) && ids.contains(&b));
    store.remove(a).unwrap();
    // Later: a is cleared, b has expired; neither comes back.
    let store = CardStore::open(&path);
    assert!(store.load(&keys(), me, at + Span::minutes(2)).is_empty());
    // A key no longer trusted drops the card too.
    let (_, _, item_c) = card(me, at, Span::minutes(10));
    store
        .add(ApprovalCard::from_item(&item_c, &keys(), me, at).unwrap())
        .unwrap();
    assert!(CardStore::open(&path)
        .load(&CoreKeys::new(), me, at)
        .is_empty());
}

#[test]
fn voice_decision_needs_exactly_one_card() {
    use VoiceDecision::*;
    assert_eq!(pending_voice_decision("approve", 0), NotADecision);
    assert_eq!(
        pending_voice_decision("approve", 1),
        Decide(Decision::Approve)
    );
    assert_eq!(
        pending_voice_decision("Jarvis, approve it please.", 1),
        Decide(Decision::Approve)
    );
    assert_eq!(pending_voice_decision("Deny", 1), Decide(Decision::Deny));
    assert_eq!(
        pending_voice_decision("hey jarvis deny it", 1),
        Decide(Decision::Deny)
    );
    assert_eq!(pending_voice_decision("approve", 2), AskToTap);
    assert_eq!(pending_voice_decision("approve the news", 1), NotADecision);
    assert_eq!(pending_voice_decision("I approve", 1), NotADecision);
    assert_eq!(pending_voice_decision("what's the news", 1), NotADecision);
}

// ------------------------------------------------------------------ through the real node

async fn card_event(r: &mut support::rig::Rig) -> ApprovalCard {
    match r.ui_event(Duration::from_secs(2)).await {
        Some(UiEvent::ApprovalCard(c)) => c,
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn a_card_item_is_received_and_shown_as_a_card() {
    let mut r = rig().await;
    let (outbox_id, approval_id, item) = card(r.node_id, now(), Span::minutes(10));
    r.push("outbox.item", item).await;
    let received = recv(&mut r.ws).await;
    assert_eq!(
        (received["kind"].as_str(), &received["payload"]),
        (Some("received"), &json!({"outbox_id": outbox_id}))
    );
    let c = card_event(&mut r).await;
    assert_eq!((c.outbox_id, c.approval_id), (outbox_id, approval_id));
    // Not acked on display: the decision acks it.
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_millis(300)).await,
        None
    );
}

#[tokio::test]
async fn decision_then_ack_in_that_order() {
    let mut r = rig().await;
    let (outbox_id, approval_id, item) = card(r.node_id, now(), Span::minutes(10));
    let sig = item["authz"]["sig"].clone();
    r.push("outbox.item", item).await;
    recv(&mut r.ws).await;
    card_event(&mut r).await;
    r.node.decide(outbox_id, Decision::Approve).await.unwrap();
    let decision = recv(&mut r.ws).await;
    support::assert_envelope_conforms(&decision);
    assert_eq!(decision["kind"], "approval.decision");
    assert_eq!(
        decision["payload"],
        json!({"approval_id": approval_id, "decision": "APPROVED", "card_sig": sig})
    );
    let ack = recv(&mut r.ws).await;
    assert_eq!(
        (ack["kind"].as_str(), &ack["payload"]),
        (Some("ack"), &json!({"outbox_id": outbox_id}))
    );
    // The card ends here: the gateway never sends outbox.cleared to the node that acked.
    match r.ui_event(Duration::from_secs(2)).await {
        Some(UiEvent::ApprovalCleared {
            outbox_id: id,
            reason,
        }) => {
            assert_eq!((id, reason.as_str()), (outbox_id, "SENT"))
        }
        other => panic!("{other:?}"),
    }
    let stored = std::fs::read_to_string(r.dir.path().join("cards.json")).unwrap();
    assert!(
        !stored.contains(&outbox_id.to_string()),
        "not kept after it was sent"
    );
    // The ack is journalled, so it is re-sent after a reconnect if it was lost.
    let journal =
        std::fs::read_to_string(r.dir.path().join(jarvis_node::node::JOURNAL_FILE)).unwrap();
    assert!(journal
        .lines()
        .any(|l| l.contains("\"acked\"") && l.contains(&outbox_id.to_string())));
    // Sent once: a second tap doesn't send again (the server's answer is what shows next).
    assert_eq!(
        r.node.decide(outbox_id, Decision::Deny).await,
        Err(DecideError::Gone)
    );
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_millis(300)).await,
        None
    );
}

#[tokio::test]
async fn cleared_card_cannot_be_decided() {
    let mut r = rig().await;
    let (outbox_id, _, item) = card(r.node_id, now(), Span::minutes(10));
    r.push("outbox.item", item).await;
    recv(&mut r.ws).await;
    card_event(&mut r).await;
    r.push(
        "outbox.cleared",
        json!({"outbox_id": outbox_id, "reason": "EXPIRED"}),
    )
    .await;
    match r.ui_event(Duration::from_secs(2)).await {
        Some(UiEvent::ApprovalCleared {
            outbox_id: id,
            reason,
        }) => {
            assert_eq!((id, reason.as_str()), (outbox_id, "EXPIRED"))
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        r.node.decide(outbox_id, Decision::Approve).await,
        Err(DecideError::Gone)
    );
    assert_eq!(
        r.node.decide(Uuid::new_v4(), Decision::Approve).await,
        Err(DecideError::Gone)
    );
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_millis(300)).await,
        None,
        "nothing sent"
    );
}

#[tokio::test]
async fn an_expired_card_is_not_decided() {
    let mut r = rig().await;
    // Expired 10 s ago: still within the 30 s tolerance, so it is shown at 0, buttons off.
    let (outbox_id, _, item) = card(r.node_id, now() - Span::minutes(1), Span::seconds(50));
    r.push("outbox.item", item).await;
    recv(&mut r.ws).await;
    let c = card_event(&mut r).await;
    assert_eq!(c.seconds_left(now()), 0);
    assert_eq!(
        r.node.decide(outbox_id, Decision::Approve).await,
        Err(DecideError::Expired)
    );
    assert_eq!(
        recv_within(&mut r.ws, Duration::from_millis(300)).await,
        None
    );
}

#[tokio::test]
async fn two_cards_shown_separately() {
    let mut r = rig().await;
    let (a, _, item_a) = card(r.node_id, now(), Span::minutes(10));
    let (b, _, item_b) = card(r.node_id, now(), Span::minutes(10));
    r.push("outbox.item", item_a).await;
    r.push("outbox.item", item_b).await;
    recv(&mut r.ws).await;
    recv(&mut r.ws).await;
    assert_eq!(card_event(&mut r).await.outbox_id, a);
    assert_eq!(card_event(&mut r).await.outbox_id, b);
    assert_eq!(r.node.voice_target().0, 2);
    r.node.decide(b, Decision::Deny).await.unwrap();
    assert_eq!(recv(&mut r.ws).await["payload"]["decision"], "DENIED");
    assert_eq!(recv(&mut r.ws).await["payload"], json!({"outbox_id": b}));
    assert_eq!(r.node.voice_target(), (1, Some(a)), "a is still waiting");
}

#[tokio::test]
async fn a_bad_card_is_received_but_never_shown() {
    let mut r = rig().await;
    let (outbox_id, _, item) = card(Uuid::new_v4(), now(), Span::minutes(10)); // another node's
    r.push("outbox.item", item).await;
    assert_eq!(
        recv(&mut r.ws).await["payload"],
        json!({"outbox_id": outbox_id})
    );
    assert_eq!(r.ui_event(Duration::from_millis(300)).await, None);
    assert_eq!(
        r.node.decide(outbox_id, Decision::Approve).await,
        Err(DecideError::Gone)
    );
}

#[test]
fn a_card_shows_only_signed_words_and_is_never_an_execution() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let approval_id = Uuid::new_v4();
    let mut obj = card_object(me, approval_id, at, at + Span::minutes(10));
    obj["summary"] = Value::Null; // the gateway's item body is never used instead
    let item = card_item(Uuid::new_v4(), &signed("core-1", "core-1", obj));
    assert_eq!(
        ApprovalCard::from_item(&item, &keys(), me, at).unwrap_err(),
        Rejected("malformed")
    );
    let mut obj = card_object(me, approval_id, at, at + Span::minutes(10));
    obj["execution_key"] = json!(Uuid::new_v4());
    let item = card_item(Uuid::new_v4(), &signed("core-1", "core-1", obj));
    assert_eq!(
        ApprovalCard::from_item(&item, &keys(), me, at).unwrap_err(),
        Rejected("malformed")
    );
}

#[test]
fn one_card_per_approval_and_a_resend_keeps_its_state() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let dir = tempfile::tempdir().unwrap();
    let store = CardStore::open(&dir.path().join("cards.json"));
    let approval_id = Uuid::new_v4();
    let authz = signed(
        "core-1",
        "core-1",
        card_object(me, approval_id, at, at + Span::minutes(10)),
    );
    let (first, second) = (Uuid::new_v4(), Uuid::new_v4());
    let card = |id| ApprovalCard::from_item(&card_item(id, &authz), &keys(), me, at).unwrap();
    assert_eq!(store.add(card(first)).unwrap(), Added::New);
    assert!(store.begin_decide(first).is_some());
    // The same item again: kept as it was (still being decided), never reset.
    assert_eq!(store.add(card(first)).unwrap(), Added::Already);
    assert!(store.begin_decide(first).is_none());
    store.undo_decide(first);
    // The re-surfaced card (a new item for the same approval) replaces the older one.
    assert_eq!(store.add(card(second)).unwrap(), Added::Replaced(first));
    assert_eq!(store.voice_target(at), (1, Some(second)));
    assert!(!store.contains(first));
}

#[test]
fn expired_cards_are_not_voice_targets() {
    let me = Uuid::new_v4();
    let at = whole_now();
    let dir = tempfile::tempdir().unwrap();
    let store = CardStore::open(&dir.path().join("cards.json"));
    let (live, _, live_item) = card(me, at, Span::minutes(10));
    let (_, _, old_item) = card(me, at - Span::minutes(1), Span::seconds(50)); // ended 10 s ago
    store
        .add(ApprovalCard::from_item(&live_item, &keys(), me, at).unwrap())
        .unwrap();
    store
        .add(ApprovalCard::from_item(&old_item, &keys(), me, at).unwrap())
        .unwrap();
    assert_eq!(store.voice_target(at), (1, Some(live)), "only the live one");
}

#[test]
fn a_corrupt_card_store_is_kept_aside() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cards.json");
    std::fs::write(&path, b"{not json").unwrap();
    assert!(CardStore::open(&path)
        .load(&keys(), Uuid::new_v4(), now())
        .is_empty());
    assert_eq!(
        std::fs::read(dir.path().join("cards.json.corrupt")).unwrap(),
        b"{not json"
    );
}

#[test]
fn the_normalise_copy_matches_jarvis_core() {
    // approval.rs keeps a copy of the coordinator's private normalise; they must not drift.
    let body = |path: &str| {
        let src =
            std::fs::read_to_string(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(path))
                .unwrap();
        let start = src
            .find("fn normalise(text: &str) -> String {")
            .expect(path);
        let end = start + src[start..].find("\n}\n").expect(path);
        src[start..end].to_owned()
    };
    assert_eq!(
        body("src/approval.rs"),
        body("../jarvis-core/src/coordinator.rs")
    );
}

#[tokio::test]
async fn a_card_under_an_untrusted_key_asks_reenrolment_and_is_not_received() {
    use jarvis_node::client::{wait_for, LinkStatus};
    let mut r = rig().await;
    let outbox_id = Uuid::new_v4();
    let authz = signed(
        "core-2",
        "core-9",
        card_object(r.node_id, Uuid::new_v4(), now(), now() + Span::minutes(10)),
    );
    r.push("outbox.item", card_item(outbox_id, &authz)).await;
    let mut status = r.node.status();
    assert!(
        wait_for(
            &mut status,
            |s| *s == LinkStatus::NeedsReenrol,
            Duration::from_secs(3)
        )
        .await
    );
    assert_ne!(
        recv_within(&mut r.ws, Duration::from_millis(200))
            .await
            .map(|f| f["kind"].clone()),
        Some(json!("received"))
    );
}

#[tokio::test]
async fn a_decision_that_could_not_be_sent_can_be_made_again() {
    use jarvis_node::client::{wait_for, LinkStatus};
    let mut r = rig().await;
    let (outbox_id, _, item) = card(r.node_id, now(), Span::minutes(10));
    r.push("outbox.item", item).await;
    recv(&mut r.ws).await;
    card_event(&mut r).await;
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
    assert_eq!(
        r.node.decide(outbox_id, Decision::Approve).await,
        Err(DecideError::NotConnected)
    );
    assert_eq!(
        r.node.voice_target(),
        (1, Some(outbox_id)),
        "still open, still decidable"
    );
}

#[tokio::test]
async fn a_resent_card_after_the_decision_is_not_shown_again() {
    let mut r = rig().await;
    let (outbox_id, _, item) = card(r.node_id, now(), Span::minutes(10));
    r.push("outbox.item", item.clone()).await;
    recv(&mut r.ws).await;
    card_event(&mut r).await;
    r.node.decide(outbox_id, Decision::Approve).await.unwrap();
    recv(&mut r.ws).await; // decision
    recv(&mut r.ws).await; // ack
    r.ui_event(Duration::from_secs(2)).await; // SENT
    r.push("outbox.item", item).await;
    assert_eq!(recv(&mut r.ws).await["kind"], "received");
    assert_eq!(
        r.ui_event(Duration::from_millis(300)).await,
        None,
        "not shown again"
    );
    assert_eq!(
        r.node.decide(outbox_id, Decision::Deny).await,
        Err(DecideError::Gone)
    );
}

#[tokio::test]
async fn a_resurfaced_card_replaces_the_older_one() {
    let mut r = rig().await;
    let approval_id = Uuid::new_v4();
    let authz = signed(
        "core-1",
        "core-1",
        card_object(r.node_id, approval_id, now(), now() + Span::minutes(10)),
    );
    let (first, second) = (Uuid::new_v4(), Uuid::new_v4());
    r.push("outbox.item", card_item(first, &authz)).await;
    recv(&mut r.ws).await;
    assert_eq!(card_event(&mut r).await.outbox_id, first);
    r.push("outbox.item", card_item(second, &authz)).await;
    recv(&mut r.ws).await;
    match r.ui_event(Duration::from_secs(2)).await {
        Some(UiEvent::ApprovalCleared { outbox_id, reason }) => {
            assert_eq!((outbox_id, reason.as_str()), (first, "SUPERSEDED"))
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(card_event(&mut r).await.outbox_id, second);
    assert_eq!(r.node.voice_target(), (1, Some(second)));
}
