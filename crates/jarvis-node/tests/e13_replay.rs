//! P2-E13 (Rust half): replay the frames each node received in the server's 200 randomised
//! multi-node runs (`server/tests/fakes/traces/e13.jsonl`) through the seen-list.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use jarvis_node::delivery::{Inbox, ItemFate};
use jarvis_node::journal::Journal;
use serde_json::Value;
use uuid::Uuid;

fn trace() -> Vec<Value> {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../server/tests/fakes/traces/e13.jsonl");
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

#[test]
fn e13_trace_replays_clean() {
    let runs = trace();
    assert_eq!(runs.len(), 200, "the committed trace has 200 runs");
    let mut items = 0;
    for run in &runs {
        let n = run["run"].as_u64().unwrap();
        for (node, frames) in run["frames"].as_object().unwrap() {
            // The trace names items by role ("this", "earlier"); give each a fixed id.
            let mut ids: HashMap<String, Uuid> = HashMap::new();
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("journal.jsonl");
            let mut shows: HashMap<Uuid, usize> = HashMap::new();
            let mut cleared: HashSet<Uuid> = HashSet::new();
            let frames = frames.as_array().unwrap();
            // Every frame twice (re-sends), restarting the node (reopening the journal)
            // before each one.
            for frame in frames.iter().chain(frames.iter()) {
                let inbox = Inbox::new(Arc::new(Mutex::new(Journal::open(&path).unwrap())));
                let name = frame["outbox_id"].as_str().unwrap().to_owned();
                let id = *ids.entry(name).or_insert_with(Uuid::new_v4);
                match frame["kind"].as_str().unwrap() {
                    "outbox.item" => {
                        if inbox.on_item(id).unwrap() == ItemFate::Show {
                            items += 1;
                            assert!(
                                !cleared.contains(&id),
                                "run {n} node {node}: shown after cleared"
                            );
                            *shows.entry(id).or_default() += 1;
                        }
                    }
                    "outbox.cleared" => {
                        inbox.on_cleared(id).unwrap();
                        cleared.insert(id);
                    }
                    other => panic!("run {n}: unexpected frame {other}"),
                }
            }
            for (id, count) in shows {
                assert_eq!(count, 1, "run {n} node {node}: {id} shown {count} times");
            }
        }
    }
    assert!(items > 100, "the replay showed items ({items})");
}
