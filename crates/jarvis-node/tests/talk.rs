//! The conversation recorder: what the person said and what Jarvis said, kept in order with
//! the panels each Jarvis line brought.

use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use jarvis_node::board::{BoardStore, ConversationEntry, How, Who};
use jarvis_node::talk::{Recorder, TEXT_MAX};
use uuid::Uuid;

struct Rig {
    _dir: tempfile::TempDir,
    store: Arc<Mutex<BoardStore>>,
    recorder: Recorder,
    seen: mpsc::Receiver<ConversationEntry>,
}

fn rig_in(dir: tempfile::TempDir) -> Rig {
    let store = Arc::new(Mutex::new(BoardStore::open(dir.path())));
    let (tx, seen) = mpsc::channel();
    let recorder = Recorder::start(store.clone(), move |entry| {
        let _ = tx.send(entry.clone());
    });
    Rig {
        _dir: dir,
        store,
        recorder,
        seen,
    }
}

fn rig() -> Rig {
    rig_in(tempfile::tempdir().unwrap())
}

fn next(rig: &Rig) -> ConversationEntry {
    rig.seen
        .recv_timeout(Duration::from_secs(5))
        .expect("an entry")
}

#[test]
fn lines_are_kept_in_order_and_marked_voice_typed_or_spoken() {
    let rig = rig();
    rig.recorder.heard("  show me the fictional rainfall  ");
    rig.recorder.spoke("On it. I'll put it on the board.");
    rig.recorder.typed("add a column");
    rig.recorder.heard("add a column");
    let got = [next(&rig), next(&rig), next(&rig)];
    assert_eq!(
        got.iter()
            .map(|e| (e.who, e.how, e.text.as_str()))
            .collect::<Vec<_>>(),
        vec![
            (Who::Person, How::Voice, "show me the fictional rainfall"),
            (Who::Jarvis, How::Spoken, "On it. I'll put it on the board."),
            (Who::Person, How::Typed, "add a column"),
        ]
    );
    // What the window was told is what the store holds.
    let stored = rig.store.lock().unwrap().conversation();
    assert_eq!(stored, got.to_vec());
    assert!(got[0].at <= got[1].at && got[1].at <= got[2].at);
}

#[test]
fn a_typed_line_that_was_never_heard_does_not_mark_the_next_spoken_one() {
    let rig = rig();
    rig.recorder.typed("this one was dropped");
    rig.recorder.heard("something said aloud");
    assert_eq!(next(&rig).how, How::Voice);
    // A mark is used once.
    rig.recorder.typed("typed twice");
    rig.recorder.heard("typed twice");
    rig.recorder.heard("typed twice");
    assert_eq!(next(&rig).how, How::Typed);
    assert_eq!(next(&rig).how, How::Voice);
}

#[test]
fn a_voice_line_before_the_typed_one_arrives_does_not_use_up_its_mark() {
    let rig = rig();
    rig.recorder.typed("add a column");
    rig.recorder.heard("something else, said aloud");
    rig.recorder.heard("add a column");
    assert_eq!(next(&rig).how, How::Voice);
    assert_eq!(next(&rig).how, How::Typed);
}

#[test]
fn a_typed_mark_does_not_outlive_its_moment() {
    let rig = rig();
    rig.recorder.typed("say it again");
    std::thread::sleep(jarvis_node::talk::TYPED_MARK_LASTS + Duration::from_millis(150));
    rig.recorder.heard("say it again"); // the same words, spoken later
    assert_eq!(next(&rig).how, How::Voice);
}

#[test]
fn a_jarvis_line_carries_the_panels_shown_since_the_last_one() {
    let rig = rig();
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    rig.recorder.heard("compare the three");
    rig.recorder.spoke("On it.");
    rig.recorder.panel_shown(a);
    rig.recorder.panel_shown(b);
    rig.recorder.panel_shown(a); // an update of the same panel
    rig.recorder.spoke("Two panels are up.");
    rig.recorder.spoke("Anything else?");
    let lines = [next(&rig), next(&rig), next(&rig), next(&rig)];
    assert!(lines[0].panels.is_empty()); // the person's line never carries panels
    assert!(lines[1].panels.is_empty());
    assert_eq!(lines[2].panels, vec![a, b]);
    assert!(lines[3].panels.is_empty());
}

#[test]
fn empty_lines_are_left_out_and_long_ones_are_cut() {
    let rig = rig();
    rig.recorder.heard("   ");
    rig.recorder.spoke("");
    rig.recorder.spoke(&"é".repeat(TEXT_MAX + 50));
    let line = next(&rig);
    assert_eq!(line.text.chars().count(), TEXT_MAX);
    assert_eq!(rig.store.lock().unwrap().conversation().len(), 1);
}

#[test]
fn the_conversation_is_back_after_a_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().to_owned();
    let first = rig_in(dir);
    first.recorder.heard("remember this");
    first.recorder.spoke("I will.");
    let (_, _) = (next(&first), next(&first));
    let kept = first._dir; // keep the folder, drop the rest
    drop((first.recorder, first.store));

    let store = BoardStore::open(&path);
    let texts: Vec<String> = store.conversation().into_iter().map(|e| e.text).collect();
    assert_eq!(texts, ["remember this", "I will."]);
    drop(kept);
}

#[test]
fn many_lines_from_many_threads_all_arrive_and_none_is_torn() {
    let rig = rig();
    let recorder = Arc::new(rig.recorder);
    let handles: Vec<_> = (0..4)
        .map(|t| {
            let recorder = recorder.clone();
            std::thread::spawn(move || {
                for i in 0..25 {
                    recorder.spoke(&format!("line {t}-{i}"));
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    let mut got = Vec::new();
    for _ in 0..100 {
        got.push(
            rig.seen
                .recv_timeout(Duration::from_secs(5))
                .expect("an entry")
                .text,
        );
    }
    got.sort();
    got.dedup();
    assert_eq!(got.len(), 100);
    assert_eq!(rig.store.lock().unwrap().conversation().len(), 100);
}
