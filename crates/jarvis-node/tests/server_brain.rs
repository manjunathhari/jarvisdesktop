//! ServerBrain (Plan B Task 6): the existing `Brain` trait over the node link.

mod support;

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use jarvis_core::action::Action;
use jarvis_core::brain::{Brain, BrainError, BrainEvent, BrainRequest, Role, Turn};
use jarvis_node::client::NotConnected;
use jarvis_node::node::ReplyEvent;
use jarvis_node::server_brain::{Link, ServerBrain, SERVER_TOOLS, SILENCE};
use jarvis_node::wire::parse_authz_object;
use serde_json::{json, Value};
use support::assert_conforms;
use support::authz::live_action;
use tokio::sync::{broadcast, mpsc};
use uuid::Uuid;

/// A fake link: records what is sent; the test plays the server through `events`.
struct FakeLink {
    connected: bool,
    events: broadcast::Sender<ReplyEvent>,
    sent: Mutex<Vec<(String, Value)>>,
    on_send: mpsc::UnboundedSender<(String, Value)>,
    /// The `message_id` of the last utterance sent.
    asked: Mutex<Option<Uuid>>,
}

#[async_trait]
impl Link for FakeLink {
    fn connected(&self) -> bool {
        self.connected
    }
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent> {
        self.events.subscribe()
    }
    async fn send(&self, kind: &'static str, payload: Value) -> Result<Uuid, NotConnected> {
        if !self.connected {
            return Err(NotConnected);
        }
        let message_id = Uuid::new_v4();
        if kind == "utterance" {
            *self.asked.lock().unwrap() = Some(message_id);
        }
        self.sent
            .lock()
            .unwrap()
            .push((kind.to_owned(), payload.clone()));
        let _ = self.on_send.send((kind.to_owned(), payload));
        Ok(message_id)
    }
}

struct Rig {
    link: Arc<FakeLink>,
    sends: mpsc::UnboundedReceiver<(String, Value)>,
    brain: Arc<ServerBrain>,
}

fn rig(connected: bool) -> Rig {
    let (on_send, sends) = mpsc::unbounded_channel();
    let link = Arc::new(FakeLink {
        connected,
        events: broadcast::channel(64).0,
        sent: Mutex::default(),
        on_send,
        asked: Mutex::default(),
    });
    let brain = Arc::new(ServerBrain::new(link.clone()));
    Rig { link, sends, brain }
}

fn req(text: &str) -> BrainRequest {
    BrainRequest {
        utterance: text.into(),
        screen_summary: "news panel open".into(),
        history: vec![Turn {
            role: Role::User,
            text: "an earlier turn".into(),
        }],
        tools: vec![],
    }
}

/// Stands for "the utterance this turn sent": `emit` puts in its real `message_id`.
const ASKED: Uuid = Uuid::from_u128(0xa5);

fn speech(reply_id: Uuid, seq: i64, text: &str, final_: bool) -> ReplyEvent {
    ReplyEvent::Speech {
        reply_id,
        seq,
        text: text.into(),
        final_,
        in_reply_to: Some(ASKED),
    }
}

/// The same event, answering `to` (another utterance, or None: a reply to no utterance).
fn answering(mut ev: ReplyEvent, to: Option<Uuid>) -> ReplyEvent {
    match &mut ev {
        ReplyEvent::Speech { in_reply_to, .. } | ReplyEvent::Action { in_reply_to, .. } => {
            *in_reply_to = to
        }
    }
    ev
}

fn action(reply_id: Uuid, seq: i64, action_kind: &str, tool: &str, input: Value) -> ReplyEvent {
    let mut obj = live_action(Uuid::new_v4(), reply_id, support::gateway::now());
    obj["action_kind"] = json!(action_kind);
    obj["args"] = json!({"tool": tool, "input": input});
    ReplyEvent::Action {
        seq,
        in_reply_to: Some(ASKED),
        authz: parse_authz_object(&obj).unwrap(),
    }
}

/// Start `think` in the background; returns its events and its result.
fn think(
    r: &Rig,
    request: BrainRequest,
) -> (
    mpsc::Receiver<BrainEvent>,
    tokio::task::JoinHandle<Result<(), BrainError>>,
) {
    let (tx, rx) = mpsc::channel(32);
    let brain = r.brain.clone();
    (
        rx,
        tokio::spawn(async move { brain.think(request, tx).await }),
    )
}

/// Wait until the utterance has been sent (so events after it are "after the send").
async fn utterance(r: &mut Rig) -> Value {
    let (kind, payload) = tokio::time::timeout(Duration::from_secs(2), r.sends.recv())
        .await
        .expect("an utterance")
        .unwrap();
    assert_eq!(kind, "utterance");
    payload
}

async fn next_send(r: &mut Rig) -> (String, Value) {
    tokio::time::timeout(Duration::from_secs(2), r.sends.recv())
        .await
        .expect("a send")
        .unwrap()
}

fn emit(r: &Rig, mut ev: ReplyEvent) {
    let asked = r.link.asked.lock().unwrap().expect("an utterance was sent");
    match &mut ev {
        ReplyEvent::Speech { in_reply_to, .. } | ReplyEvent::Action { in_reply_to, .. } => {
            if *in_reply_to == Some(ASKED) {
                *in_reply_to = Some(asked);
            }
        }
    }
    r.link.events.send(ev).unwrap();
}

async fn drain(mut rx: mpsc::Receiver<BrainEvent>) -> Vec<BrainEvent> {
    let mut out = Vec::new();
    while let Some(ev) = rx.recv().await {
        out.push(ev);
    }
    out
}

fn say(t: &str) -> BrainEvent {
    BrainEvent::Speech(t.into())
}

#[tokio::test]
async fn speech_streams_in_order_until_final() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("what's the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(&r, speech(id, 1, "Here are the headlines.", false));
    emit(&r, speech(id, 2, " Rain is expected.", false));
    emit(&r, speech(id, 3, " That's all.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![
            say("Here are the headlines."),
            say("Rain is expected."),
            say("That's all.")
        ]
    );
}

#[tokio::test]
async fn half_word_pieces_are_spoken_as_whole_sentences() {
    // Live check, 25 Nov: the server streams Claude's text in pieces, often parts of words;
    // each spoken on its own sounded like gibberish.
    let mut r = rig(true);
    let (rx, done) = think(&r, req("tell me about the moon"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    for (seq, piece) in [
        "The mo",
        "on is about 3.",
        "5 times smal",
        "ler than Earth. It ",
        "has no air.",
    ]
    .iter()
    .enumerate()
    {
        emit(&r, speech(id, seq as i64 + 1, piece, seq == 4));
    }
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![
            say("The moon is about 3.5 times smaller than Earth."),
            say("It has no air.")
        ]
    );
}

#[tokio::test]
async fn words_before_an_action_are_said_before_it() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(&r, speech(id, 1, "Here you", false));
    emit(&r, speech(id, 2, " go", false));
    emit(&r, action(id, 3, "module.news", "show_news", json!({})));
    assert_eq!(next_send(&mut r).await.0, "action.receipt");
    emit(&r, speech(id, 4, "Okay.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![
            say("Here you go"),
            BrainEvent::Action(Action::new("show_news", json!({}))),
            say("Okay.")
        ]
    );
}

#[tokio::test]
async fn first_speech_forwarded_before_final() {
    let mut r = rig(true);
    let (mut rx, done) = think(&r, req("hello"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(&r, speech(id, 1, "Hello. ", false));
    let first = tokio::time::timeout(Duration::from_secs(1), rx.recv())
        .await
        .unwrap();
    assert_eq!(first, Some(say("Hello.")));
    assert!(!done.is_finished(), "still waiting for final");
    emit(&r, speech(id, 2, "", true));
    done.await.unwrap().unwrap();
    assert_eq!(drain(rx).await, vec![], "an empty final chunk says nothing");
}

#[tokio::test]
async fn module_action_maps_tool_and_sends_ok_receipt() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(
        &r,
        action(
            id,
            1,
            "module.news",
            "show_news",
            json!({"channel": "headlines"}),
        ),
    );
    let (kind, receipt) = next_send(&mut r).await;
    assert_eq!(kind, "action.receipt");
    assert_eq!(
        receipt,
        json!({"reply_id": id, "seq": 1, "result": "OK", "evidence": {}})
    );
    assert_conforms("action.receipt", &receipt);
    emit(&r, speech(id, 2, "Here's the news.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![
            BrainEvent::Action(Action::new("show_news", json!({"channel": "headlines"}))),
            say("Here's the news.")
        ]
    );
}

#[tokio::test]
async fn screen_action_without_input_gets_empty_args() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("clear the screen"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    let mut ev = action(id, 1, "screen.clear", "clear_screen", json!({}));
    if let ReplyEvent::Action { authz, .. } = &mut ev {
        let mut obj = authz.raw.clone();
        obj["args"] = json!({"tool": "clear_screen"});
        *authz = parse_authz_object(&obj).unwrap();
    }
    emit(&r, ev);
    assert_eq!(next_send(&mut r).await.1["result"], "OK");
    emit(&r, speech(id, 2, "Done.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await[0],
        BrainEvent::Action(Action::new("clear_screen", json!({})))
    );
}

#[tokio::test]
async fn tool_kind_mismatch_refused() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    for (seq, kind, tool) in [
        (1, "module.live_tv", "show_news"),
        (2, "module.news", "format_disk"),
        (3, "screen.clear", "close_panel"),
    ] {
        emit(&r, action(id, seq, kind, tool, json!({})));
        let (k, receipt) = next_send(&mut r).await;
        assert_eq!(k, "action.receipt");
        assert_eq!(
            receipt,
            json!({"reply_id": id, "seq": seq, "result": "REFUSED", "evidence": {"check": "unknown_tool"}})
        );
        assert_conforms("action.receipt", &receipt);
    }
    emit(&r, speech(id, 4, "Sorry.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![say("Sorry.")],
        "no action reaches the coordinator"
    );
}

#[tokio::test]
async fn malformed_args_and_unknown_kinds_refused_pc_left_alone() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("do things"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    let mut bad = action(id, 1, "module.news", "show_news", json!({}));
    if let ReplyEvent::Action { authz, .. } = &mut bad {
        let mut obj = authz.raw.clone();
        obj["args"] = json!({"tool": 7, "input": {}});
        *authz = parse_authz_object(&obj).unwrap();
    }
    emit(&r, bad);
    assert_eq!(
        next_send(&mut r).await.1["evidence"],
        json!({"check": "malformed_args"})
    );
    emit(&r, action(id, 2, "memory.write", "memory_write", json!({})));
    assert_eq!(
        next_send(&mut r).await.1["evidence"],
        json!({"check": "unknown_action_kind"})
    );
    // pc.* is Task 8's executor's to answer: no receipt from ServerBrain.
    emit(
        &r,
        action(
            id,
            3,
            "pc.list_test_folder",
            "pc_list_test_folder",
            json!({}),
        ),
    );
    emit(&r, speech(id, 4, "Done.", true));
    done.await.unwrap().unwrap();
    assert_eq!(drain(rx).await, vec![say("Done.")]);
    let receipts = r
        .link
        .sent
        .lock()
        .unwrap()
        .iter()
        .filter(|(k, _)| k == "action.receipt")
        .count();
    assert_eq!(receipts, 2);
}

#[tokio::test]
async fn other_replies_are_ignored() {
    // R12-37: only the reply answering this utterance is followed.
    let mut r = rig(true);
    let (rx, done) = think(&r, req("hello"));
    utterance(&mut r).await;
    let (mine, phone, own, twin) = (
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
    );
    // Another device's question, answered at the same moment, starting at seq 1.
    emit(
        &r,
        answering(
            speech(phone, 1, "someone else's", false),
            Some(Uuid::new_v4()),
        ),
    );
    emit(
        &r,
        answering(
            action(phone, 2, "module.news", "show_news", json!({})),
            Some(Uuid::new_v4()),
        ),
    );
    // Core's own one-chunk result ("Done: ... is in the Recycle Bin."): answers no utterance.
    emit(
        &r,
        answering(speech(own, 1, "A fictional result.", true), None),
    );
    emit(&r, speech(mine, 1, "Hi.", false));
    // A second reply naming the same utterance once one is followed: ignored.
    emit(&r, speech(twin, 1, "a duplicate turn", false));
    emit(&r, speech(mine, 2, " Bye.", true));
    done.await.unwrap().unwrap();
    assert_eq!(drain(rx).await, vec![say("Hi."), say("Bye.")]);
    assert!(r
        .link
        .sent
        .lock()
        .unwrap()
        .iter()
        .all(|(k, _)| k != "action.receipt"));
}

#[tokio::test]
async fn the_answer_is_followed_when_its_first_event_never_arrives() {
    // The ACTION chunk usually takes seq 1 and may be dropped (expired, never re-sent).
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(&r, speech(id, 2, "Here's the news.", true));
    done.await.unwrap().unwrap();
    assert_eq!(drain(rx).await, vec![say("Here's the news.")]);
}

#[tokio::test]
async fn an_earlier_turns_reply_re_sent_is_ignored() {
    // A late or re-sent chunk of the previous reply names the previous utterance.
    let mut r = rig(true);
    let (_rx, done) = think(&r, req("first"));
    utterance(&mut r).await;
    let first_utterance = r.link.asked.lock().unwrap().unwrap();
    let first = Uuid::new_v4();
    emit(&r, speech(first, 1, "Part one.", false));
    emit(&r, speech(first, 2, "Done.", true));
    done.await.unwrap().unwrap();
    let (rx, done) = think(&r, req("second"));
    utterance(&mut r).await;
    emit(
        &r,
        answering(speech(first, 1, "Part one.", false), Some(first_utterance)),
    );
    let second = Uuid::new_v4();
    emit(&r, speech(second, 1, "Answer.", true));
    done.await.unwrap().unwrap();
    assert_eq!(drain(rx).await, vec![say("Answer.")]);
}

#[tokio::test]
async fn not_connected_fails_fast() {
    let r = rig(false);
    let (tx, _rx) = mpsc::channel(4);
    let t = std::time::Instant::now();
    let err = r.brain.think(req("hello"), tx).await.unwrap_err();
    assert!(t.elapsed() < Duration::from_millis(100));
    assert!(
        matches!(&err, BrainError::Unavailable(m) if m == "I can't reach the Jarvis server"),
        "{err}"
    );
    assert!(r.link.sent.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn silence_times_out() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("hello"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    emit(&r, speech(id, 1, "Thinking. ", false));
    // Ignored events don't count as an answer.
    tokio::time::sleep(SILENCE - Duration::from_secs(1)).await;
    emit(
        &r,
        answering(speech(Uuid::new_v4(), 1, "someone else's", false), None),
    );
    tokio::time::sleep(Duration::from_secs(2)).await;
    let err = done.await.unwrap().unwrap_err();
    assert!(
        matches!(&err, BrainError::Unavailable(m) if m == "the Jarvis server didn't answer"),
        "{err}"
    );
    assert_eq!(drain(rx).await, vec![say("Thinking.")]);
}

#[tokio::test(start_paused = true)]
async fn a_chunk_of_the_reply_restarts_the_silence_timer() {
    let mut r = rig(true);
    let (_rx, done) = think(&r, req("hello"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    for seq in 1..=3 {
        tokio::time::sleep(SILENCE - Duration::from_secs(1)).await;
        emit(&r, speech(id, seq, "more", false));
    }
    tokio::time::sleep(SILENCE - Duration::from_secs(1)).await;
    emit(&r, speech(id, 4, "end", true));
    done.await.unwrap().unwrap();
}

#[tokio::test]
async fn a_lagging_subscriber_fails_the_reply() {
    // Task 5 minor 4: chunks lost to a lagging broadcast can't be recovered.
    let mut r = rig(true);
    let (tx, mut rx) = mpsc::channel(1); // the caller stops reading: think blocks on tx
    let brain = r.brain.clone();
    let done = tokio::spawn(async move { brain.think(req("hello"), tx).await });
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    for seq in 1..=200 {
        emit(&r, speech(id, seq, "word", false));
    }
    while rx.recv().await.is_some() {}
    let err = done.await.unwrap().unwrap_err();
    assert!(
        matches!(&err, BrainError::Unavailable(m) if m.contains("lost part")),
        "{err}"
    );
}

#[tokio::test]
async fn utterance_conforms_and_omits_history() {
    let mut r = rig(true);
    let (_rx, _done) = think(&r, req("what's the news"));
    let payload = utterance(&mut r).await;
    assert_eq!(
        payload,
        json!({"text": "what's the news", "screen_summary": "news panel open"})
    );
    assert_conforms("utterance", &payload);
}

#[tokio::test]
async fn long_text_is_clipped_to_the_contract_and_blank_screen_is_null() {
    let mut r = rig(true);
    let mut request = req(&"é".repeat(5000));
    request.screen_summary = "  ".into();
    let (_rx, _done) = think(&r, request);
    let payload = utterance(&mut r).await;
    assert_eq!(payload["text"].as_str().unwrap().chars().count(), 4000);
    assert_eq!(payload["screen_summary"], Value::Null);
    assert_conforms("utterance", &payload);
    let mut request = req("hi");
    request.screen_summary = "x".repeat(3000);
    let (_rx, _done) = think(&r, request);
    let payload = utterance(&mut r).await;
    assert_eq!(payload["screen_summary"].as_str().unwrap().len(), 2000);
}

#[tokio::test]
async fn a_blank_utterance_is_not_sent() {
    let r = rig(true);
    let (tx, _rx) = mpsc::channel(4);
    assert!(matches!(
        r.brain.think(req("   "), tx).await,
        Err(BrainError::Protocol(_))
    ));
    assert!(r.link.sent.lock().unwrap().is_empty());
}

#[test]
fn tool_table_matches_server() {
    let path = support::contracts().join("../server/src/jarvis_server/core/prompt.py");
    let src = std::fs::read_to_string(path).unwrap();
    let block = src
        .split("TOOL_ACTIONS: dict[str, str] = {")
        .nth(1)
        .unwrap();
    let block = block.split('}').next().unwrap();
    let server: BTreeSet<(String, String)> = block
        .lines()
        .filter_map(|l| {
            let parts: Vec<&str> = l.split('"').collect();
            (parts.len() >= 4).then(|| (parts[1].to_owned(), parts[3].to_owned()))
        })
        .filter(|(_, kind)| kind.starts_with("module.") || kind.starts_with("screen."))
        .collect();
    let ours: BTreeSet<(String, String)> = SERVER_TOOLS
        .iter()
        .map(|(t, k)| (t.to_string(), k.to_string()))
        .collect();
    assert_eq!(ours, server);
    assert_eq!(ours.len(), 7);
}

#[tokio::test]
async fn re_sent_events_of_the_reply_are_dropped() {
    // A lost `received` or a gateway resync re-sends chunks: never spoken or run twice.
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    let id = Uuid::new_v4();
    let news = || action(id, 1, "module.news", "show_news", json!({}));
    emit(&r, news());
    assert_eq!(next_send(&mut r).await.0, "action.receipt");
    emit(&r, news());
    emit(&r, speech(id, 2, "A.", false));
    emit(&r, speech(id, 2, "A.", false));
    emit(&r, speech(id, 3, " B.", true));
    done.await.unwrap().unwrap();
    assert_eq!(
        drain(rx).await,
        vec![
            BrainEvent::Action(Action::new("show_news", json!({}))),
            say("A."),
            say("B.")
        ]
    );
    let receipts = r
        .link
        .sent
        .lock()
        .unwrap()
        .iter()
        .filter(|(k, _)| k == "action.receipt")
        .count();
    assert_eq!(receipts, 1, "one receipt per action");
}

#[tokio::test]
async fn an_action_after_the_caller_left_is_refused_not_delivered() {
    let mut r = rig(true);
    let (rx, done) = think(&r, req("show me the news"));
    utterance(&mut r).await;
    drop(rx);
    let id = Uuid::new_v4();
    emit(&r, action(id, 1, "module.news", "show_news", json!({})));
    let (kind, receipt) = next_send(&mut r).await;
    assert_eq!(kind, "action.receipt");
    assert_eq!(
        receipt,
        json!({"reply_id": id, "seq": 1, "result": "REFUSED", "evidence": {"check": "not_delivered"}})
    );
    assert_conforms("action.receipt", &receipt);
    emit(&r, speech(id, 2, "Done.", true));
    done.await.unwrap().unwrap();
}

/// A link whose reply broadcast has closed (the node has stopped).
struct ClosedLink;

#[async_trait]
impl Link for ClosedLink {
    fn connected(&self) -> bool {
        true
    }
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent> {
        broadcast::channel(1).1
    }
    async fn send(&self, _: &'static str, _: Value) -> Result<Uuid, NotConnected> {
        Ok(Uuid::new_v4())
    }
}

#[tokio::test]
async fn a_closed_reply_stream_fails_the_turn() {
    let brain = ServerBrain::new(Arc::new(ClosedLink));
    let (tx, _rx) = mpsc::channel(4);
    let err = brain.think(req("hello"), tx).await.unwrap_err();
    assert!(
        matches!(&err, BrainError::Unavailable(m) if m == "I can't reach the Jarvis server"),
        "{err}"
    );
}
