//! Board slice 1, Task 5: Board actions on the desktop node. Parsing and the contract's limits,
//! the local panel store and conversation, the receipts (`RENDERED` only after the Board
//! reports the panel drawn), and panel state the person changes by hand.
//! People, panels and requests are fictional.

mod support;

use std::time::Duration;

use chrono::Duration as Span;
use jarvis_node::board::{
    self, content_sha256, BoardEvent, BoardStore, ConversationEntry, How, Panel, PanelKind,
    PanelState, Who,
};
use jarvis_node::node::UiEvent;
use serde_json::{json, Map, Value};
use support::authz::signed;
use support::gateway::now;
use support::net::{recv, recv_within};
use support::rig::{rig, rig_with, Rig};
use support::{assert_conforms, example};
use uuid::Uuid;

const TEXT: &str = "| Option | Nights |\n|---|---|\n| Coorg (fictional) | 3 |";

fn args(panel_id: Uuid, revision: u32, kind: &str, title: &str, content: &str) -> Value {
    json!({
        "panel_id": panel_id, "revision": revision, "kind": kind, "title": title,
        "content": content, "content_sha256": content_sha256(content), "cost_paise": 1240,
        "created_at": "2026-11-26T19:15:29.743239+05:30",
    })
}

fn map(v: &Value) -> &Map<String, Value> {
    v.as_object().unwrap()
}

fn shown(v: &Value) -> Panel {
    match board::parse("board.show", map(v)).unwrap() {
        BoardEvent::Show(p) => p,
        other => panic!("not a show: {other:?}"),
    }
}

// --- parsing: the contract's limits, each with its own check ---------------------------------

#[test]
fn the_contracts_example_parses() {
    let v = example("board.show.args.valid.json");
    assert_conforms("board.show.args", &v);
    let p = shown(&v);
    assert_eq!(
        (p.kind, p.revision, p.cost_paise, p.state),
        (PanelKind::Text, 1, 1240, PanelState::Open)
    );
    assert_eq!(p.title, "December trip: three options");
    assert_eq!(p.content_sha256, v["content_sha256"]);
    // The server's timestamps: an offset, and a fraction of a second when there is one.
    let v = args(Uuid::new_v4(), 2, "canvas", "Chart", "<p>fictional</p>");
    assert_conforms("board.update.args", &v);
    match board::parse("board.update", map(&v)).unwrap() {
        BoardEvent::Update(p) => {
            assert_eq!(
                p.created_at.to_rfc3339(),
                "2026-11-26T19:15:29.743239+05:30"
            )
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn bad_show_args_each_have_their_own_check() {
    let id = Uuid::new_v4();
    let good = args(id, 1, "text", "Trip", TEXT);
    let with = |k: &str, v: Value| {
        let mut m = good.clone();
        m[k] = v;
        m
    };
    let check = |v: &Value| board::parse("board.show", map(v)).unwrap_err();
    // Over the limit: 30,001 bytes, counted as UTF-8 (15,001 two-byte characters are 30,002).
    assert_eq!(
        check(&args(id, 1, "text", "T", &"x".repeat(30_001))),
        "too_large"
    );
    assert_eq!(
        check(&args(id, 1, "text", "T", &"é".repeat(15_001))),
        "too_large"
    );
    assert!(board::parse(
        "board.show",
        map(&args(id, 1, "text", "T", &"é".repeat(15_000)))
    )
    .is_ok());
    assert_eq!(
        check(&with("content_sha256", json!("0".repeat(64)))),
        "hash_mismatch"
    );
    assert_eq!(check(&with("kind", json!("html"))), "unknown_kind");
    for bad in [
        with("revision", json!(0)),
        with("revision", json!("1")),
        with("revision", json!(2_147_483_648u64)),
        with("panel_id", json!("p1")),
        with("title", json!("")),
        with("title", json!("   ")),
        with("title", json!("two\nlines")),
        with("content", json!("")),
        with("content", json!("a\u{0}b")),
        with("content", json!(7)),
        with("cost_paise", json!(-1)),
        with("created_at", json!("yesterday")),
        with("extra", json!(1)),
        json!({"panel_id": id}),
    ] {
        assert_eq!(check(&bad), "malformed", "{bad}");
    }
    assert_eq!(
        board::parse("board.make", map(&good)).unwrap_err(),
        "unknown_action_kind"
    );
}

#[test]
fn title_length_is_counted_in_code_points() {
    let id = Uuid::new_v4();
    // 120 characters of two bytes each, and one of four: 120 code points, far more bytes.
    let title = format!("{}{}", "é".repeat(119), "😀");
    assert!(board::parse("board.show", map(&args(id, 1, "text", &title, TEXT))).is_ok());
    let longer = format!("{title}x");
    assert_eq!(
        board::parse("board.show", map(&args(id, 1, "text", &longer, TEXT))).unwrap_err(),
        "malformed"
    );
}

#[test]
fn state_and_focus_args_parse() {
    let id = Uuid::new_v4();
    for (word, state) in [
        ("open", PanelState::Open),
        ("minimised", PanelState::Minimised),
        ("maximised", PanelState::Maximised),
        ("closed", PanelState::Closed),
    ] {
        let v = json!({"panel_id": id, "state": word});
        assert_conforms("board.set_state.args", &v);
        assert_eq!(
            board::parse("board.set_state", map(&v)).unwrap(),
            BoardEvent::State {
                panel_id: id,
                state
            }
        );
    }
    let v = json!({"panel_id": id});
    assert_conforms("board.focus.args", &v);
    assert_eq!(
        board::parse("board.focus", map(&v)).unwrap(),
        BoardEvent::Focus { panel_id: id }
    );
    for bad in [
        json!({"panel_id": id, "state": "hidden"}),
        json!({"state": "open"}),
        json!({"panel_id": id, "state": "open", "x": 1}),
    ] {
        assert_eq!(
            board::parse("board.set_state", map(&bad)).unwrap_err(),
            "malformed"
        );
    }
    assert_eq!(
        board::parse(
            "board.focus",
            map(&json!({"panel_id": id, "state": "open"}))
        )
        .unwrap_err(),
        "malformed"
    );
}

// --- the local store --------------------------------------------------------------------------

fn panel(id: Uuid, revision: u32, content: &str) -> Panel {
    shown(&args(id, revision, "text", "Trip", content))
}

#[test]
fn update_with_an_older_revision_is_stale() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    let id = Uuid::new_v4();
    store.apply(&BoardEvent::Show(panel(id, 2, "two"))).unwrap();
    assert_eq!(
        store.apply(&BoardEvent::Update(panel(id, 1, "one"))),
        Err("stale_revision")
    );
    assert_eq!(
        store.apply(&BoardEvent::Show(panel(id, 1, "one"))),
        Err("stale_revision")
    );
    // The same revision again with other content is not the panel the store holds.
    assert_eq!(
        store.apply(&BoardEvent::Update(panel(id, 2, "other"))),
        Err("stale_revision")
    );
    // The same revision and content again changes nothing and is fine (a re-send).
    store.apply(&BoardEvent::Show(panel(id, 2, "two"))).unwrap();
    store
        .apply(&BoardEvent::Update(panel(id, 3, "three")))
        .unwrap();
    let [only] = &store.panels()[..] else {
        panic!()
    };
    assert_eq!((only.revision, only.content.as_str()), (3, "three"));
}

#[test]
fn an_update_keeps_the_panels_state_and_a_lost_panel_is_restored() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    let id = Uuid::new_v4();
    store.apply(&BoardEvent::Show(panel(id, 1, "one"))).unwrap();
    store
        .apply(&BoardEvent::State {
            panel_id: id,
            state: PanelState::Maximised,
        })
        .unwrap();
    store
        .apply(&BoardEvent::Update(panel(id, 2, "two")))
        .unwrap();
    assert_eq!(store.panels()[0].state, PanelState::Maximised);
    // An update carries the whole panel, so one the store has lost comes back, open.
    let lost = Uuid::new_v4();
    store
        .apply(&BoardEvent::Update(panel(lost, 4, "four")))
        .unwrap();
    let back = store
        .panels()
        .into_iter()
        .find(|p| p.panel_id == lost)
        .unwrap();
    assert_eq!((back.revision, back.state), (4, PanelState::Open));
}

#[test]
fn state_and_focus_of_an_unknown_panel_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    let id = Uuid::new_v4();
    let state = BoardEvent::State {
        panel_id: id,
        state: PanelState::Closed,
    };
    assert_eq!(store.apply(&state), Err("unknown_panel"));
    assert_eq!(
        store.apply(&BoardEvent::Focus { panel_id: id }),
        Err("unknown_panel")
    );
    assert!(store.panels().is_empty());
}

#[test]
fn focus_brings_a_minimised_or_closed_panel_back() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    let id = Uuid::new_v4();
    store.apply(&BoardEvent::Show(panel(id, 1, "one"))).unwrap();
    for hidden in [PanelState::Minimised, PanelState::Closed] {
        store
            .apply(&BoardEvent::State {
                panel_id: id,
                state: hidden,
            })
            .unwrap();
        store.apply(&BoardEvent::Focus { panel_id: id }).unwrap();
        assert_eq!(store.panels()[0].state, PanelState::Open);
    }
    store
        .apply(&BoardEvent::State {
            panel_id: id,
            state: PanelState::Maximised,
        })
        .unwrap();
    store.apply(&BoardEvent::Focus { panel_id: id }).unwrap();
    assert_eq!(
        store.panels()[0].state,
        PanelState::Maximised,
        "already in view"
    );
}

#[test]
fn store_survives_reopen_and_a_torn_write() {
    let dir = tempfile::tempdir().unwrap();
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    {
        let mut store = BoardStore::open(dir.path());
        store.apply(&BoardEvent::Show(panel(a, 1, "one"))).unwrap();
        store.apply(&BoardEvent::Show(panel(b, 1, "two"))).unwrap();
        store
            .apply(&BoardEvent::State {
                panel_id: a,
                state: PanelState::Closed,
            })
            .unwrap();
    }
    // A crash during the next save left a half-written temporary file beside the real one.
    std::fs::write(dir.path().join("board.json.tmp"), b"{\"v\":1,\"pan").unwrap();
    let mut store = BoardStore::open(dir.path());
    let panels = store.panels();
    assert_eq!(
        panels.iter().map(|p| p.panel_id).collect::<Vec<_>>(),
        vec![a, b]
    );
    assert_eq!(panels[0].state, PanelState::Closed);
    assert_eq!(panels[1].content, "two");
    // Clearing closed panels is the one local removal.
    assert_eq!(store.clear_closed().unwrap(), 1);
    assert_eq!(store.clear_closed().unwrap(), 0);
    drop(store);
    assert_eq!(BoardStore::open(dir.path()).panels().len(), 1);
}

#[test]
fn an_unreadable_store_starts_empty_and_keeps_the_bad_file() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("board.json"), b"not json").unwrap();
    let mut store = BoardStore::open(dir.path());
    assert!(store.panels().is_empty());
    store
        .apply(&BoardEvent::Show(panel(Uuid::new_v4(), 1, "one")))
        .unwrap();
    assert_eq!(
        std::fs::read(dir.path().join("board.json.unreadable")).unwrap(),
        b"not json"
    );
    assert_eq!(BoardStore::open(dir.path()).panels().len(), 1);
}

#[test]
fn conversation_keeps_order_and_panel_links() {
    let dir = tempfile::tempdir().unwrap();
    let id = Uuid::new_v4();
    let entry = |who, text: &str, how, panels: Vec<Uuid>| ConversationEntry {
        at: now(),
        who,
        text: text.into(),
        how,
        panels,
    };
    let said = [
        entry(
            Who::Person,
            "Compare three hill stations",
            How::Voice,
            vec![],
        ),
        entry(Who::Jarvis, "On it.", How::Spoken, vec![]),
        entry(Who::Person, "And a chart", How::Typed, vec![]),
        entry(
            Who::Jarvis,
            "Three options are on the board.",
            How::Spoken,
            vec![id],
        ),
    ];
    {
        let mut store = BoardStore::open(dir.path());
        for e in &said {
            store.append_turn(e.clone()).unwrap();
        }
        assert_eq!(store.conversation(), said);
    }
    // A crash tore the last line: the rest is still read.
    let path = dir.path().join("conversation.jsonl");
    let mut text = std::fs::read_to_string(&path).unwrap();
    text.push_str("{\"at\":\"2026-11-26T19");
    std::fs::write(&path, text).unwrap();
    let mut store = BoardStore::open(dir.path());
    assert_eq!(store.conversation(), said);
    assert_eq!(store.conversation()[3].panels, vec![id]);
    store.append_turn(said[0].clone()).unwrap();
    assert_eq!(BoardStore::open(dir.path()).conversation().len(), 5);
}

#[test]
fn only_the_last_2000_entries_are_loaded() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    for i in 0..2005 {
        store
            .append_turn(ConversationEntry {
                at: now(),
                who: Who::Person,
                text: format!("line {i}"),
                how: How::Typed,
                panels: vec![],
            })
            .unwrap();
    }
    drop(store);
    let loaded = BoardStore::open(dir.path()).conversation();
    assert_eq!(loaded.len(), 2000);
    assert_eq!(
        (loaded[0].text.as_str(), loaded[1999].text.as_str()),
        ("line 5", "line 2004")
    );
}

// --- the node: routing, receipts, panel state ---------------------------------------------------

/// A core-signed live `kind` action for the rig's node.
fn action(r: &Rig, reply_id: Uuid, kind: &str, args: &Value) -> Value {
    let at = now();
    let ts = |t: chrono::DateTime<chrono::FixedOffset>| {
        t.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    };
    signed(
        "core-1",
        "core-1",
        json!({
            "v": 1, "kind": "node.action", "action_kind": kind, "args": args,
            "target_node_id": r.node_id, "person_id": Uuid::new_v4(),
            "approval_id": null, "execution_key": null, "reply_id": reply_id, "summary": null,
            "issued_at": ts(at), "valid_until": ts(at + Span::minutes(2)),
        }),
    )
}

async fn push(r: &mut Rig, seq: i64, kind: &str, args: &Value) -> Uuid {
    let reply_id = Uuid::new_v4();
    let authz = action(r, reply_id, kind, args);
    r.push(
        "node.action",
        json!({"authz": authz, "seq": seq, "in_reply_to": null}),
    )
    .await;
    reply_id
}

/// The next frame of `kind` the node sends, as its payload.
async fn sent(r: &mut Rig, kind: &str) -> Value {
    loop {
        let frame = recv(&mut r.ws).await;
        if frame["kind"] == kind {
            return frame["payload"].clone();
        }
    }
}

async fn board_event(r: &mut Rig) -> BoardEvent {
    match r.ui_event(Duration::from_secs(5)).await {
        Some(UiEvent::Board(ev)) => ev,
        other => panic!("expected a Board event, got {other:?}"),
    }
}

fn evidence(receipt: &Value) -> (&str, Option<&str>) {
    (
        receipt["result"].as_str().unwrap(),
        receipt["evidence"]["check"].as_str(),
    )
}

#[tokio::test]
async fn show_is_stored_and_emitted_then_rendered_receipt_conforms() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    let a = args(id, 1, "text", "Trip", TEXT);
    let reply_id = push(&mut r, 3, "board.show", &a).await;
    let BoardEvent::Show(p) = board_event(&mut r).await else {
        panic!()
    };
    assert_eq!((p.panel_id, p.revision, p.content.as_str()), (id, 1, TEXT));
    assert_eq!(r.node.board().lock().unwrap().panels(), vec![p.clone()]);
    // Nothing is claimed until the Board says the panel is drawn.
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
    r.node.board_rendered(id, 1, true).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("action.receipt", &receipt);
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(
        receipt,
        json!({"reply_id": reply_id, "seq": 3, "result": "RENDERED",
               "evidence": {"panel_id": id, "revision": 1, "content_sha256": content_sha256(TEXT)}})
    );
    // One receipt for one action: a second report, or the timeout, sends nothing more.
    r.node.board_rendered(id, 1, true).await;
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
}

#[tokio::test]
async fn an_update_is_stored_and_its_receipt_names_the_new_revision() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", "one")).await;
    board_event(&mut r).await;
    r.node.board_rendered(id, 1, true).await;
    sent(&mut r, "action.receipt").await;
    push(
        &mut r,
        1,
        "board.update",
        &args(id, 2, "text", "Trip, wider", "two"),
    )
    .await;
    let BoardEvent::Update(p) = board_event(&mut r).await else {
        panic!()
    };
    assert_eq!((p.revision, p.title.as_str()), (2, "Trip, wider"));
    r.node.board_rendered(id, 2, true).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(receipt["evidence"]["revision"], 2);
    assert_eq!(receipt["evidence"]["content_sha256"], content_sha256("two"));
}

#[tokio::test]
async fn bad_show_is_refused_and_not_stored() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    let mut wrong_hash = args(id, 1, "text", "Trip", TEXT);
    wrong_hash["content_sha256"] = json!("0".repeat(64));
    for (bad, check) in [
        (
            args(id, 1, "text", "Trip", &"x".repeat(30_001)),
            "too_large",
        ),
        (wrong_hash, "hash_mismatch"),
        (args(id, 1, "html", "Trip", TEXT), "unknown_kind"),
    ] {
        let reply_id = push(&mut r, 2, "board.show", &bad).await;
        let receipt = sent(&mut r, "action.receipt").await;
        assert_conforms("board.show.receipt", &receipt);
        assert_eq!(evidence(&receipt), ("REFUSED", Some(check)));
        assert_eq!(receipt["reply_id"], json!(reply_id));
        // What the node could read is given back: the ID, the revision, the hash it computed.
        assert_eq!(receipt["evidence"]["panel_id"], json!(id));
        assert_eq!(receipt["evidence"]["revision"], 1);
        assert_eq!(
            receipt["evidence"]["content_sha256"],
            content_sha256(bad["content"].as_str().unwrap())
        );
    }
    assert!(r.node.board().lock().unwrap().panels().is_empty());
    assert!(
        r.ui_event(Duration::from_millis(200)).await.is_none(),
        "nothing reached the Board"
    );
}

#[tokio::test]
async fn refusal_of_malformed_args_has_null_evidence_and_conforms() {
    let mut r = rig().await;
    push(
        &mut r,
        1,
        "board.show",
        &json!({"panel_id": "p1", "content": 7}),
    )
    .await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(
        receipt["evidence"],
        json!({"panel_id": null, "revision": null, "content_sha256": null, "check": "malformed"})
    );
    push(&mut r, 1, "board.set_state", &json!({"state": "open"})).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.set_state.receipt", &receipt);
    assert_eq!(
        receipt["evidence"],
        json!({"panel_id": null, "check": "malformed"})
    );
    assert_eq!(receipt["result"], "REFUSED");
}

#[tokio::test]
async fn a_stale_update_is_refused_and_the_panel_is_unchanged() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 2, "text", "Trip", "two")).await;
    board_event(&mut r).await;
    push(
        &mut r,
        1,
        "board.update",
        &args(id, 1, "text", "Trip", "one"),
    )
    .await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(evidence(&receipt), ("REFUSED", Some("stale_revision")));
    assert_eq!(r.node.board().lock().unwrap().panels()[0].content, "two");
}

#[tokio::test]
async fn no_render_report_gives_render_timeout() {
    let mut r = rig_with(|cfg| cfg.board_render_timeout = Duration::from_millis(200)).await;
    let id = Uuid::new_v4();
    let reply_id = push(&mut r, 4, "board.show", &args(id, 1, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(evidence(&receipt), ("ERROR", Some("render_timeout")));
    assert_eq!(
        (&receipt["reply_id"], &receipt["seq"]),
        (&json!(reply_id), &json!(4))
    );
    assert_eq!(receipt["evidence"]["panel_id"], json!(id));
    // The panel is kept (it may yet be drawn); a late report sends no second receipt.
    assert_eq!(r.node.board().lock().unwrap().panels().len(), 1);
    r.node.board_rendered(id, 1, true).await;
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
}

#[tokio::test]
async fn a_failed_render_is_an_error_receipt() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(
        &mut r,
        1,
        "board.show",
        &args(id, 1, "canvas", "Chart", "<p>x</p>"),
    )
    .await;
    board_event(&mut r).await;
    r.node.board_rendered(id, 1, false).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.show.receipt", &receipt);
    assert_eq!(evidence(&receipt), ("ERROR", Some("render_failed")));
}

#[tokio::test]
async fn set_state_and_focus_are_answered_at_once() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    r.node.board_rendered(id, 1, true).await;
    sent(&mut r, "action.receipt").await;

    let reply_id = push(
        &mut r,
        2,
        "board.set_state",
        &json!({"panel_id": id, "state": "minimised"}),
    )
    .await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.set_state.receipt", &receipt);
    assert_eq!(
        receipt,
        json!({"reply_id": reply_id, "seq": 2, "result": "OK", "evidence": {"panel_id": id}})
    );
    assert_eq!(
        board_event(&mut r).await,
        BoardEvent::State {
            panel_id: id,
            state: PanelState::Minimised
        }
    );
    assert_eq!(
        r.node.board().lock().unwrap().panels()[0].state,
        PanelState::Minimised
    );

    // Focus brings it back into view, and the server is told its state changed.
    push(&mut r, 1, "board.focus", &json!({"panel_id": id})).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.set_state.receipt", &receipt);
    assert_eq!(evidence(&receipt), ("OK", None));
    let report = sent(&mut r, "board.state").await;
    assert_conforms("board.state", &report);
    assert_eq!(
        (&report["panel_id"], &report["state"]),
        (&json!(id), &json!("open"))
    );
    assert_eq!(
        board_event(&mut r).await,
        BoardEvent::Focus { panel_id: id }
    );
}

#[tokio::test]
async fn set_state_for_an_unknown_panel_is_refused() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    for (kind, a) in [
        (
            "board.set_state",
            json!({"panel_id": id, "state": "closed"}),
        ),
        ("board.focus", json!({"panel_id": id})),
    ] {
        push(&mut r, 1, kind, &a).await;
        let receipt = sent(&mut r, "action.receipt").await;
        assert_conforms("board.set_state.receipt", &receipt);
        assert_eq!(evidence(&receipt), ("REFUSED", Some("unknown_panel")));
        assert_eq!(receipt["evidence"]["panel_id"], json!(id));
    }
    assert!(r.ui_event(Duration::from_millis(200)).await.is_none());
}

#[tokio::test]
async fn work_started_is_passed_to_the_app_and_answered_at_once() {
    let mut r = rig().await;
    let args = json!({"state": "started"});
    assert_conforms("board.working.args", &args);
    push(&mut r, 1, "board.working", &args).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_conforms("board.working.receipt", &receipt);
    assert_eq!(receipt["result"], "OK");
    assert_eq!(receipt["evidence"], json!({}));
    assert!(matches!(
        r.ui_event(Duration::from_secs(2)).await,
        Some(UiEvent::BoardWorking)
    ));
    // Nothing is stored: it is not a panel.
    assert!(r.node.board().lock().unwrap().panels().is_empty());
}

#[tokio::test]
async fn a_damaged_work_started_is_refused_and_not_passed_on() {
    let mut r = rig().await;
    for bad in [
        json!({"state": "ended"}),
        json!({}),
        json!({"state": "started", "extra": 1}),
    ] {
        push(&mut r, 1, "board.working", &bad).await;
        let receipt = sent(&mut r, "action.receipt").await;
        assert_conforms("board.working.receipt", &receipt);
        assert_eq!(evidence(&receipt), ("REFUSED", Some("malformed")));
    }
    assert!(r.ui_event(Duration::from_millis(200)).await.is_none());
}

#[tokio::test]
async fn an_unknown_board_kind_is_refused_once() {
    let mut r = rig().await;
    push(
        &mut r,
        1,
        "board.erase",
        &json!({"panel_id": Uuid::new_v4()}),
    )
    .await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_eq!(evidence(&receipt), ("REFUSED", Some("unknown_action_kind")));
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
}

#[tokio::test]
async fn click_state_change_sends_board_state_that_conforms() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 3, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    r.node.board_state(id, PanelState::Closed).await.unwrap();
    let report = sent(&mut r, "board.state").await;
    assert_conforms("board.state", &report);
    assert_eq!(
        (&report["panel_id"], &report["state"], &report["revision"]),
        (&json!(id), &json!("closed"), &json!(3))
    );
    assert_eq!(
        r.node.board().lock().unwrap().panels()[0].state,
        PanelState::Closed
    );
    // The Board made the change itself: it is not echoed back as an event.
    assert!(r.ui_event(Duration::from_millis(200)).await.is_none());
    assert_eq!(
        r.node.board_state(Uuid::new_v4(), PanelState::Closed).await,
        Err("unknown_panel")
    );
}

#[tokio::test]
async fn board_actions_never_reach_the_reply_stream() {
    // `ServerBrain` answers the live actions of the reply it follows; a Board action is the
    // node's own and must get exactly one receipt.
    let mut r = rig().await;
    let mut replies = r.node.subscribe_replies();
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    assert!(replies.try_recv().is_err());
}

#[tokio::test]
async fn an_action_changed_after_signing_is_not_applied() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    let mut authz = action(
        &r,
        Uuid::new_v4(),
        "board.show",
        &args(id, 1, "text", "Trip", TEXT),
    );
    authz["object"]["args"]["title"] = json!("Changed after signing");
    r.push(
        "node.action",
        json!({"authz": authz, "seq": 1, "in_reply_to": null}),
    )
    .await;
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
    assert!(r.node.board().lock().unwrap().panels().is_empty());
}

#[tokio::test]
async fn panels_are_back_after_a_restart() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    r.node.board_state(id, PanelState::Minimised).await.unwrap();
    let reopened = BoardStore::open(r.dir.path());
    let [p] = &reopened.panels()[..] else {
        panic!()
    };
    assert_eq!(
        (p.panel_id, p.state, p.content.as_str()),
        (id, PanelState::Minimised, TEXT)
    );
}

#[tokio::test]
async fn two_waiting_panels_each_get_their_own_receipt() {
    let mut r = rig().await;
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    let reply_a = push(&mut r, 1, "board.show", &args(a, 1, "text", "First", "one")).await;
    let reply_b = push(
        &mut r,
        2,
        "board.show",
        &args(b, 1, "canvas", "Second", "<p>2</p>"),
    )
    .await;
    board_event(&mut r).await;
    board_event(&mut r).await;
    // Drawn out of order: reporting one leaves the other waiting.
    r.node.board_rendered(b, 1, true).await;
    let second = sent(&mut r, "action.receipt").await;
    assert_eq!(
        (&second["reply_id"], &second["seq"]),
        (&json!(reply_b), &json!(2))
    );
    r.node.board_rendered(a, 1, true).await;
    let first = sent(&mut r, "action.receipt").await;
    assert_eq!(
        (&first["reply_id"], &first["result"]),
        (&json!(reply_a), &json!("RENDERED"))
    );
    // A report for a revision nothing waits for sends nothing.
    r.node.board_rendered(a, 2, true).await;
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
}

// --- after the independent review of Task 5 ---------------------------------------------------

#[test]
fn an_update_brings_a_hidden_panel_back_so_it_can_be_drawn() {
    // A minimised or closed panel is not on screen, so its update could never be reported
    // drawn. The person asked for the change: it comes back, open.
    let dir = tempfile::tempdir().unwrap();
    let mut store = BoardStore::open(dir.path());
    let id = Uuid::new_v4();
    store.apply(&BoardEvent::Show(panel(id, 1, "one"))).unwrap();
    for (hidden, revision) in [(PanelState::Minimised, 2), (PanelState::Closed, 3)] {
        store
            .apply(&BoardEvent::State {
                panel_id: id,
                state: hidden,
            })
            .unwrap();
        store
            .apply(&BoardEvent::Update(panel(id, revision, "newer")))
            .unwrap();
        assert_eq!(store.panels()[0].state, PanelState::Open);
    }
    // One in view keeps its state.
    store
        .apply(&BoardEvent::State {
            panel_id: id,
            state: PanelState::Maximised,
        })
        .unwrap();
    store
        .apply(&BoardEvent::Update(panel(id, 4, "newest")))
        .unwrap();
    assert_eq!(store.panels()[0].state, PanelState::Maximised);
}

#[test]
fn files_from_a_newer_build_are_not_thrown_away() {
    let dir = tempfile::tempdir().unwrap();
    let id = Uuid::new_v4();
    {
        let mut store = BoardStore::open(dir.path());
        store.apply(&BoardEvent::Show(panel(id, 1, "one"))).unwrap();
        store
            .append_turn(ConversationEntry {
                at: now(),
                who: Who::Person,
                text: "hello".into(),
                how: How::Typed,
                panels: vec![],
            })
            .unwrap();
    }
    // A later build added a field to each; this build reads what it knows.
    let path = dir.path().join("board.json");
    let mut file: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    file["panels"][0]["pinned"] = json!(true);
    file["layout"] = json!("grid");
    std::fs::write(&path, serde_json::to_vec(&file).unwrap()).unwrap();
    let talk = dir.path().join("conversation.jsonl");
    let line = std::fs::read_to_string(&talk).unwrap();
    std::fs::write(&talk, line.replacen('{', "{\"mood\":\"calm\",", 1)).unwrap();
    let store = BoardStore::open(dir.path());
    assert_eq!(store.panels().len(), 1);
    assert_eq!(store.conversation().len(), 1);
    assert!(!dir.path().join("board.json.unreadable").exists());
}

#[test]
fn a_store_from_a_newer_format_is_left_alone_and_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("board.json");
    let newer = br#"{"v":2,"panels":[],"something":"new"}"#;
    std::fs::write(&path, newer).unwrap();
    let mut store = BoardStore::open(dir.path());
    assert_eq!(
        store.apply(&BoardEvent::Show(panel(Uuid::new_v4(), 1, "one"))),
        Err("store_failed")
    );
    assert!(store.clear_closed().is_err());
    assert_eq!(std::fs::read(&path).unwrap(), newer, "untouched");
}

#[test]
fn every_unreadable_store_is_kept() {
    let dir = tempfile::tempdir().unwrap();
    for bad in [&b"first bad file"[..], &b"second bad file"[..]] {
        std::fs::write(dir.path().join("board.json"), bad).unwrap();
        let mut store = BoardStore::open(dir.path());
        store
            .apply(&BoardEvent::Show(panel(Uuid::new_v4(), 1, "one")))
            .unwrap();
    }
    let kept: Vec<Vec<u8>> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.to_string_lossy().contains("unreadable"))
        .map(|p| std::fs::read(p).unwrap())
        .collect();
    assert_eq!(kept.len(), 2, "neither bad file was overwritten");
}

#[test]
fn a_conversation_torn_inside_a_character_keeps_its_good_lines() {
    let dir = tempfile::tempdir().unwrap();
    let entry = |text: &str| ConversationEntry {
        at: now(),
        who: Who::Jarvis,
        text: text.into(),
        how: How::Spoken,
        panels: vec![],
    };
    {
        let mut store = BoardStore::open(dir.path());
        store.append_turn(entry("one")).unwrap();
        store.append_turn(entry("two")).unwrap();
    }
    let path = dir.path().join("conversation.jsonl");
    let mut bytes = std::fs::read(&path).unwrap();
    bytes.extend_from_slice(b"{\"text\":\"caf\xc3"); // cut in the middle of a two-byte letter
    std::fs::write(&path, bytes).unwrap();
    let mut store = BoardStore::open(dir.path());
    assert_eq!(store.conversation().len(), 2);
    store.append_turn(entry("three")).unwrap();
    let texts: Vec<String> = BoardStore::open(dir.path())
        .conversation()
        .into_iter()
        .map(|e| e.text)
        .collect();
    assert_eq!(texts, ["one", "two", "three"]);
}

#[test]
fn a_board_event_serialises_for_the_window() {
    let id = Uuid::new_v4();
    let show = serde_json::to_value(BoardEvent::Show(panel(id, 1, "one"))).unwrap();
    assert_eq!(
        (&show["type"], &show["panel_id"], &show["state"]),
        (&json!("show"), &json!(id), &json!("open"))
    );
    assert_eq!(
        serde_json::to_value(BoardEvent::State {
            panel_id: id,
            state: PanelState::Closed
        })
        .unwrap(),
        json!({"type": "state", "panel_id": id, "state": "closed"})
    );
    assert_eq!(
        serde_json::to_value(BoardEvent::Focus { panel_id: id }).unwrap(),
        json!({"type": "focus", "panel_id": id})
    );
}

#[tokio::test]
async fn the_event_for_an_update_carries_the_panels_real_state() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", "one")).await;
    board_event(&mut r).await;
    r.node.board_rendered(id, 1, true).await;
    sent(&mut r, "action.receipt").await;
    r.node.board_state(id, PanelState::Maximised).await.unwrap();
    sent(&mut r, "board.state").await;
    push(
        &mut r,
        1,
        "board.update",
        &args(id, 2, "text", "Trip", "two"),
    )
    .await;
    let BoardEvent::Update(p) = board_event(&mut r).await else {
        panic!()
    };
    assert_eq!(
        p.state,
        PanelState::Maximised,
        "what the store holds, not a default"
    );

    // Minimised: the update brings it back, the window is told so, and so is the server.
    r.node.board_rendered(id, 2, true).await;
    sent(&mut r, "action.receipt").await;
    r.node.board_state(id, PanelState::Minimised).await.unwrap();
    sent(&mut r, "board.state").await;
    push(
        &mut r,
        1,
        "board.update",
        &args(id, 3, "text", "Trip", "three"),
    )
    .await;
    let report = sent(&mut r, "board.state").await;
    assert_conforms("board.state", &report);
    assert_eq!(
        (&report["state"], &report["revision"]),
        (&json!("open"), &json!(3))
    );
    let BoardEvent::Update(p) = board_event(&mut r).await else {
        panic!()
    };
    assert_eq!(p.state, PanelState::Open);
}

#[tokio::test]
async fn a_newer_revision_drawn_ends_the_wait_for_an_older_one() {
    // Two changes to one panel close together; the window draws only the newer. The older
    // gets no receipt at all: it was neither drawn nor did it fail.
    let mut r = rig_with(|cfg| cfg.board_render_timeout = Duration::from_millis(400)).await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", "one")).await;
    let newer = push(
        &mut r,
        1,
        "board.update",
        &args(id, 2, "text", "Trip", "two"),
    )
    .await;
    board_event(&mut r).await;
    board_event(&mut r).await;
    r.node.board_rendered(id, 2, true).await;
    let receipt = sent(&mut r, "action.receipt").await;
    assert_eq!(
        (&receipt["reply_id"], &receipt["result"]),
        (&json!(newer), &json!("RENDERED"))
    );
    assert!(
        recv_within(&mut r.ws, Duration::from_millis(900))
            .await
            .is_none(),
        "no timeout for the older"
    );
}

#[tokio::test]
async fn the_same_action_twice_waits_once() {
    let mut r = rig_with(|cfg| cfg.board_render_timeout = Duration::from_millis(300)).await;
    let id = Uuid::new_v4();
    let reply_id = Uuid::new_v4();
    let authz = action(
        &r,
        reply_id,
        "board.show",
        &args(id, 1, "text", "Trip", TEXT),
    );
    for _ in 0..2 {
        r.push(
            "node.action",
            json!({"authz": authz, "seq": 1, "in_reply_to": null}),
        )
        .await;
        board_event(&mut r).await;
    }
    let first = sent(&mut r, "action.receipt").await;
    assert_eq!(evidence(&first), ("ERROR", Some("render_timeout")));
    assert!(
        recv_within(&mut r.ws, Duration::from_millis(600))
            .await
            .is_none(),
        "one receipt"
    );
}

#[tokio::test]
async fn focus_on_a_panel_in_view_reports_no_state() {
    let mut r = rig().await;
    let id = Uuid::new_v4();
    push(&mut r, 1, "board.show", &args(id, 1, "text", "Trip", TEXT)).await;
    board_event(&mut r).await;
    push(&mut r, 1, "board.focus", &json!({"panel_id": id})).await;
    // The frames that follow: the focus receipt, and nothing else.
    let mut kinds = Vec::new();
    while let Some(frame) = recv_within(&mut r.ws, Duration::from_millis(400)).await {
        kinds.push(frame["kind"].as_str().unwrap().to_owned());
    }
    assert_eq!(kinds, ["action.receipt"]);
}

#[tokio::test]
async fn an_action_for_another_node_is_not_applied() {
    let mut r = rig().await;
    let at = now();
    let ts = |t: chrono::DateTime<chrono::FixedOffset>| {
        t.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    };
    let other = signed(
        "core-1",
        "core-1",
        json!({
            "v": 1, "kind": "node.action", "action_kind": "board.show",
            "args": args(Uuid::new_v4(), 1, "text", "Trip", TEXT),
            "target_node_id": Uuid::new_v4(), "person_id": Uuid::new_v4(),
            "approval_id": null, "execution_key": null, "reply_id": Uuid::new_v4(),
            "summary": null, "issued_at": ts(at), "valid_until": ts(at + Span::minutes(2)),
        }),
    );
    r.push(
        "node.action",
        json!({"authz": other, "seq": 1, "in_reply_to": null}),
    )
    .await;
    assert!(recv_within(&mut r.ws, Duration::from_millis(300))
        .await
        .is_none());
    assert!(r.node.board().lock().unwrap().panels().is_empty());
}

// --- what the Board window needs (Task 6) -----------------------------------------------------

mod window {
    use super::*;
    use jarvis_node::board::view::{
        pick_placement, snapshot, spend_today_paise, MonitorInfo, Placement,
    };

    fn monitor(x: i32, width: u32) -> MonitorInfo {
        MonitorInfo {
            x,
            y: 0,
            width,
            height: 1080,
        }
    }

    #[test]
    fn placement_prefers_the_other_monitor() {
        let two = [monitor(0, 1920), monitor(1920, 2560)];
        assert_eq!(
            pick_placement(&two, Some(0)),
            Placement::Borderless { monitor: 1 }
        );
        assert_eq!(
            pick_placement(&two, Some(1)),
            Placement::Borderless { monitor: 0 }
        );
        // With three, the largest of the others.
        let three = [monitor(0, 1920), monitor(1920, 1280), monitor(3200, 3840)];
        assert_eq!(
            pick_placement(&three, Some(0)),
            Placement::Borderless { monitor: 2 }
        );
        // The main window's monitor is not known: never cover the first one.
        assert_eq!(
            pick_placement(&two, None),
            Placement::Borderless { monitor: 1 }
        );
        assert_eq!(
            pick_placement(&two, Some(7)),
            Placement::Borderless { monitor: 1 }
        );
    }

    #[test]
    fn placement_falls_back_to_one_monitor() {
        assert_eq!(
            pick_placement(&[monitor(0, 1920)], Some(0)),
            Placement::Normal
        );
        assert_eq!(pick_placement(&[], None), Placement::Normal);
    }

    fn costing(created_at: &str, cost: u64) -> Panel {
        let mut v = args(Uuid::new_v4(), 1, "text", "Trip", TEXT);
        v["created_at"] = json!(created_at);
        v["cost_paise"] = json!(cost);
        shown(&v)
    }

    #[test]
    fn spend_today_sums_todays_panels_only() {
        // "Today" is the day in India, whatever offset a timestamp carries.
        let now = chrono::DateTime::parse_from_rfc3339("2026-11-26T22:30:00+05:30").unwrap();
        let panels = [
            costing("2026-11-26T00:00:00+05:30", 100), // the first minute of today
            costing("2026-11-26T18:29:59Z", 20),       // 23:59:59 in India: today
            costing("2026-11-25T23:59:59+05:30", 4000), // yesterday
            costing("2026-11-26T18:30:00Z", 8000),     // midnight in India: tomorrow
            costing("2026-11-26T11:00:00-08:00", 16000), // tomorrow in India
        ];
        assert_eq!(spend_today_paise(&panels, now), 120);
        assert_eq!(spend_today_paise(&[], now), 0);
    }

    #[test]
    fn snapshot_is_what_the_window_draws() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = BoardStore::open(dir.path());
        let id = Uuid::new_v4();
        let mut v = args(id, 1, "text", "Trip", TEXT);
        v["created_at"] = json!("2026-11-26T10:00:00+05:30");
        store.apply(&BoardEvent::Show(shown(&v))).unwrap();
        let now = chrono::DateTime::parse_from_rfc3339("2026-11-26T22:30:00+05:30").unwrap();
        let snap = serde_json::to_value(snapshot(&store, now)).unwrap();
        assert_eq!(snap["spend_today_paise"], 1240);
        assert_eq!(snap["conversation"], json!([]));
        assert_eq!(
            snap["panels"][0],
            json!({
                "panel_id": id, "kind": "text", "title": "Trip", "content": TEXT,
                "content_sha256": content_sha256(TEXT), "revision": 1, "state": "open",
                "cost_paise": 1240, "created_at": "2026-11-26T10:00:00+05:30",
            })
        );
    }
}

mod snapshot_size {
    use jarvis_node::board::view::{snapshot, SNAPSHOT_LINES};
    use jarvis_node::board::{BoardStore, ConversationEntry, How, Who};

    #[test]
    fn a_snapshot_carries_only_the_newest_lines_of_a_long_conversation() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = BoardStore::open(dir.path());
        let now = chrono::Utc::now().fixed_offset();
        for i in 0..SNAPSHOT_LINES + 25 {
            store
                .append_turn(ConversationEntry {
                    at: now,
                    who: Who::Person,
                    text: format!("line {i}"),
                    how: How::Voice,
                    panels: vec![],
                })
                .unwrap();
        }
        let got = snapshot(&store, now).conversation;
        assert_eq!(got.len(), SNAPSHOT_LINES);
        assert_eq!(got[0].text, "line 25");
        assert_eq!(
            got.last().unwrap().text,
            format!("line {}", SNAPSHOT_LINES + 24)
        );
    }
}

mod links {
    use jarvis_node::board::view::openable_link;

    /// The list the page's own check (`text.ts` `linkTarget`) is tested against too.
    const VECTORS: &str = include_str!("../../../app/src/board/link-vectors.json");

    #[test]
    fn only_a_plain_https_address_may_be_opened() {
        let vectors: serde_json::Value = serde_json::from_str(VECTORS).unwrap();
        let list = |key: &str| -> Vec<String> {
            vectors[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect()
        };
        assert!(list("open").len() >= 5 && list("refuse").len() >= 30);
        for good in list("open") {
            assert_eq!(openable_link(&good).as_deref(), Some(good.as_str()));
        }
        for bad in list("refuse") {
            assert_eq!(openable_link(&bad), None, "{bad:?}");
        }
        let long = format!("https://example.org/{}", "a".repeat(2100));
        assert_eq!(openable_link(&long), None);
    }
}
