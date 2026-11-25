//! Plan B Task 8: the test-folder listing and the Recycle Bin delete (§9.4), with the memory
//! folder. The Windows behaviour itself is proven in `folder_windows.rs`.

mod support;

use std::sync::{Arc, Mutex};

use chrono::{Duration as Span, Timelike};
use jarvis_node::folder::memory::{MemoryFolder, Tamper};
use jarvis_node::folder::{valid_name, Executor, FolderOps};
use jarvis_node::journal::{FileFacts, Journal, Record, Stage};
use jarvis_node::wire::{parse_authz_object, AuthzObject};
use serde_json::{json, Value};
use support::assert_conforms;
use support::gateway::now;
use uuid::Uuid;

struct Rig {
    folder: Arc<MemoryFolder>,
    journal: Arc<Mutex<Journal>>,
    exec: Executor,
    dir: tempfile::TempDir,
}

fn rig_with(folder: MemoryFolder) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let journal = Arc::new(Mutex::new(
        Journal::open(&dir.path().join("journal.jsonl")).unwrap(),
    ));
    let folder = Arc::new(folder);
    let exec = Executor::new(folder.clone() as Arc<dyn FolderOps>, journal.clone());
    Rig {
        folder,
        journal,
        exec,
        dir,
    }
}

fn rig() -> Rig {
    rig_with(MemoryFolder::new())
}

impl Rig {
    /// The same journal and folder after a restart.
    fn restart(self) -> Rig {
        let journal = Arc::new(Mutex::new(
            Journal::open(&self.dir.path().join("journal.jsonl")).unwrap(),
        ));
        let exec = Executor::new(self.folder.clone() as Arc<dyn FolderOps>, journal.clone());
        Rig {
            folder: self.folder,
            journal,
            exec,
            dir: self.dir,
        }
    }

    fn stage_of(&self, key: Uuid) -> Option<Stage> {
        self.journal
            .lock()
            .unwrap()
            .state()
            .executions
            .get(&key)
            .cloned()
    }

    fn journal_kinds(&self, key: Uuid) -> Vec<String> {
        std::fs::read_to_string(self.dir.path().join("journal.jsonl"))
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap())
            .filter(|v| v["execution_key"] == json!(key))
            .map(|v| v["t"].as_str().unwrap().to_owned())
            .collect()
    }

    fn append(&self, r: Record) {
        self.journal.lock().unwrap().append(&r).unwrap();
    }

    fn started(&self, key: Uuid, facts: &FileFacts) {
        self.append(Record::Started {
            execution_key: key,
            args: facts.clone(),
            at: now(),
            outbox_id: None,
        });
    }
}

/// An approved delete of `facts` (a verified object, as `check_node_action` returns it).
fn approved(facts: &FileFacts, key: Uuid) -> AuthzObject {
    let at = now().with_nanosecond(0).unwrap();
    let obj = json!({
        "v": 1, "kind": "node.action", "action_kind": "pc.delete_test_file",
        "args": {"name": facts.name, "size": facts.size, "sha256": facts.sha256, "file_id": facts.file_id},
        "target_node_id": Uuid::new_v4(), "person_id": Uuid::new_v4(),
        "approval_id": Uuid::new_v4(), "execution_key": key, "reply_id": null, "summary": null,
        "issued_at": at.to_rfc3339(), "valid_until": (at + Span::minutes(10)).to_rfc3339(),
    });
    parse_authz_object(&obj).unwrap()
}

fn delete(r: &Rig, facts: &FileFacts, key: Uuid) -> Value {
    let receipt = r
        .exec
        .delete(&approved(facts, key), None)
        .expect("a receipt");
    assert_conforms("action.receipt", &receipt);
    assert_conforms("pc.delete_test_file.receipt", &receipt);
    receipt
}

fn result(receipt: &Value) -> (&str, Option<&str>) {
    (
        receipt["result"].as_str().unwrap(),
        receipt["evidence"]["check"].as_str(),
    )
}

// ------------------------------------------------------------------ the name rule and listing

#[test]
fn name_rule() {
    for ok in ["a.txt", "A1-b_c.d", "9", &"a".repeat(100)] {
        assert!(valid_name(ok), "{ok}");
    }
    let long = "a".repeat(101);
    for bad in [
        "",
        ".hidden",
        "..",
        "a/b",
        "a\\b",
        "a:b",
        "C:x",
        "con",
        "CON.txt",
        "nul",
        "com1.log",
        "lpt9",
        "Aux.tar.gz",
        &long,
        "é.txt",
        "a b.txt",
        "-x",
        "_x",
    ] {
        assert!(!valid_name(bad), "{bad}");
    }
    // Not device names: a letter or digit more, or COM0.
    for ok in ["cone.txt", "com10", "com0", "lpt", "nullify"] {
        assert!(valid_name(ok), "{ok}");
    }
}

#[test]
fn listing_omits_invalid_names_and_non_regular_files() {
    let r = rig();
    let a = r.folder.put("a.txt", b"fictional one");
    let b = r.folder.put("B-2.log", b"fictional two");
    r.folder.put("a b.txt", b"space");
    r.folder.put(".hidden", b"hidden");
    r.folder.put("é.txt", b"accent");
    r.folder.put_dir("sub");
    r.folder.put_link("link.txt");
    let receipt = r.exec.list_receipt(Uuid::new_v4(), 3);
    assert_eq!(receipt["result"], "OK");
    let files = receipt["evidence"]["files"].as_array().unwrap();
    let names: Vec<&str> = files.iter().map(|f| f["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["B-2.log", "a.txt"], "sorted, odd files left out");
    assert_eq!(
        files[1],
        json!({"name": a.name, "size": a.size, "sha256": a.sha256, "file_id": a.file_id})
    );
    assert_eq!(files[0]["file_id"], json!(b.file_id));
}

#[test]
fn listing_receipt_conforms() {
    let r = rig();
    r.folder.put("a.txt", b"fictional");
    let reply_id = Uuid::new_v4();
    let receipt = r.exec.list_receipt(reply_id, 2);
    assert_eq!(
        (&receipt["reply_id"], &receipt["seq"]),
        (&json!(reply_id), &json!(2))
    );
    assert_conforms("action.receipt", &receipt);
    assert_conforms("pc.list_test_folder.receipt", &receipt);
}

#[test]
fn listing_is_capped_at_1000() {
    let r = rig();
    for i in 0..1005 {
        r.folder
            .put(&format!("f{i:04}.txt"), format!("fictional {i}").as_bytes());
    }
    let receipt = r.exec.list_receipt(Uuid::new_v4(), 1);
    assert_eq!(receipt["evidence"]["files"].as_array().unwrap().len(), 1000);
    assert_conforms("pc.list_test_folder.receipt", &receipt);
}

#[test]
fn no_folder_configured_refuses() {
    let r = rig_with(MemoryFolder::unconfigured());
    let listing = r.exec.list_receipt(Uuid::new_v4(), 1);
    assert_eq!(
        (listing["result"].as_str(), &listing["evidence"]),
        (Some("REFUSED"), &json!({"files": []}))
    );
    assert_conforms("pc.list_test_folder.receipt", &listing);
    let facts = FileFacts {
        name: "a.txt".into(),
        size: 1,
        sha256: "0".repeat(64),
        file_id: format!("{}:{}", "0".repeat(16), "1".repeat(32)),
    };
    let key = Uuid::new_v4();
    assert_eq!(
        result(&delete(&r, &facts, key)),
        ("REFUSED", Some("no_test_folder"))
    );
    // A folder that is itself a link is refused the same way, with its own check.
    let r = rig();
    r.folder.set_folder_is_link(true);
    assert_eq!(r.exec.list_receipt(Uuid::new_v4(), 1)["result"], "REFUSED");
    let f = r.folder.put("a.txt", b"x");
    assert_eq!(
        result(&delete(&r, &f, Uuid::new_v4())),
        ("REFUSED", Some("folder_reparse_point"))
    );
    assert!(r.folder.in_folder("a.txt"));
}

// ------------------------------------------------------------------ the delete

#[test]
fn delete_journals_started_staged_done_in_order() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    let receipt = delete(&r, &f, key);
    assert_eq!(result(&receipt), ("RECYCLED", None));
    assert_eq!(receipt["execution_key"], json!(key));
    assert_eq!(receipt["evidence"]["file_id"], json!(f.file_id));
    assert_eq!(receipt["evidence"]["before_sha256"], json!(f.sha256));
    assert!(receipt["evidence"]["recycled_at"].is_string());
    assert_eq!(r.journal_kinds(key), ["started", "staged", "done"]);
    assert!(!r.folder.in_folder("a.txt") && r.folder.in_bin(&f.file_id));
    assert_eq!(r.stage_of(key), Some(Stage::Done(receipt)));
}

#[test]
fn mismatch_leaves_file_in_place() {
    // A different SHA-256 (same ID, the content changed since the listing).
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    r.folder.replace_content("a.txt", b"changed");
    let key = Uuid::new_v4();
    let receipt = delete(&r, &f, key);
    assert_eq!(receipt["result"], "MISMATCH");
    assert_eq!(receipt["evidence"]["recycled_at"], Value::Null);
    assert!(r.folder.in_folder("a.txt"));
    assert_eq!(r.folder.recycle_calls(), 0);
    // Same content, different file ID (swapped for a copy).
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    r.folder.swap_file("a.txt");
    assert_eq!(delete(&r, &f, Uuid::new_v4())["result"], "MISMATCH");
    assert!(r.folder.in_folder("a.txt"));
    // Gone: NOT_FOUND, with no file evidence.
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    r.folder.remove("a.txt");
    let receipt = delete(&r, &f, Uuid::new_v4());
    assert_eq!(receipt["result"], "NOT_FOUND");
    assert_eq!(receipt["evidence"]["file_id"], Value::Null);
}

#[test]
fn rule5_needs_approval_and_unused_key() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    let mut no_approval = approved(&f, key);
    no_approval.approval_id = None;
    let receipt = r.exec.delete(&no_approval, None).unwrap();
    assert_eq!(result(&receipt), ("REFUSED", Some("no_approval")));
    assert_conforms("pc.delete_test_file.receipt", &receipt);
    assert!(r.folder.in_folder("a.txt"));
    // No execution_key: nothing can be receipted, so nothing is done.
    let mut no_key = approved(&f, key);
    no_key.execution_key = None;
    assert_eq!(r.exec.delete(&no_key, None), None);
    // A key in progress (STARTED, not done) is never run twice and gets no second receipt.
    let busy = Uuid::new_v4();
    r.started(busy, &f);
    assert_eq!(r.exec.delete(&approved(&f, busy), None), None);
    assert!(r.folder.in_folder("a.txt"));
    // Args that aren't one valid file object, or a bad name: refused, file untouched.
    let mut bad = approved(&f, Uuid::new_v4());
    bad.args.insert("extra".into(), json!(1));
    assert_eq!(
        result(&r.exec.delete(&bad, None).unwrap()),
        ("REFUSED", Some("bad_args"))
    );
    let mut bad_name = approved(&f, Uuid::new_v4());
    bad_name.args.insert("name".into(), json!("..\\a.txt"));
    assert_eq!(
        result(&r.exec.delete(&bad_name, None).unwrap()),
        ("REFUSED", Some("bad_name"))
    );
    let mut wrong_kind = approved(&f, Uuid::new_v4());
    wrong_kind.action_kind = "pc.format_disk".into();
    assert_eq!(
        result(&r.exec.delete(&wrong_kind, None).unwrap()),
        ("REFUSED", Some("unknown_action_kind"))
    );
    assert!(r.folder.in_folder("a.txt"));
    assert_eq!(r.folder.recycle_calls(), 0);
}

#[test]
fn duplicate_action_resends_stored_receipt() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    let first = delete(&r, &f, key);
    let again = delete(&r, &f, key);
    assert_eq!(again, first);
    assert_eq!(r.folder.recycle_calls(), 1);
    // Also after a restart.
    let r = r.restart();
    assert_eq!(delete(&r, &f, key), first);
    assert_eq!(r.folder.recycle_calls(), 1);
}

#[test]
fn step_4_refuses_a_tampered_staging_and_keeps_the_file_there() {
    for (tamper, check) in [
        (Tamper::Junction, "staging_reparse_point"),
        (Tamper::Dacl, "staging_dacl"),
        (Tamper::SwapFile, "file_id"),
        (Tamper::ChangeContent, "sha256"),
    ] {
        let r = rig();
        let f = r.folder.put("a.txt", b"fictional");
        let key = Uuid::new_v4();
        r.folder.tamper_after_stage(key, tamper);
        let receipt = delete(&r, &f, key);
        assert_eq!(result(&receipt), ("REFUSED", Some(check)), "{tamper:?}");
        assert!(r.folder.staged(key), "{tamper:?}: still in staging");
        assert_eq!(r.folder.recycle_calls(), 0);
    }
}

#[test]
fn delete_receipt_conforms() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    for receipt in [
        delete(&r, &f, Uuid::new_v4()),
        delete(&r, &f, Uuid::new_v4()),
    ] {
        // RECYCLED, then NOT_FOUND (it has gone): both conform (checked in `delete`).
        assert!(receipt["evidence"].is_object());
    }
}

// ------------------------------------------------------------------ recovery (the table)

/// Run `recover()` twice: the receipts must be the same both times, and touch nothing more.
fn recover_twice(r: &Rig) -> Vec<Value> {
    let first: Vec<Value> = r.exec.recover().into_iter().map(|x| x.receipt).collect();
    let calls = r.folder.recycle_calls();
    let second: Vec<Value> = r.exec.recover().into_iter().map(|x| x.receipt).collect();
    assert_eq!(first, second, "recover twice: the same receipts");
    assert_eq!(
        r.folder.recycle_calls(),
        calls,
        "the second recover touches nothing"
    );
    for receipt in &first {
        assert_conforms("pc.delete_test_file.receipt", receipt);
    }
    first
}

#[test]
fn recover_done_resends() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    let receipt = delete(&r, &f, key);
    let r = r.restart();
    assert_eq!(recover_twice(&r), vec![receipt]);
    assert_eq!(r.folder.recycle_calls(), 1);
}

#[test]
fn recover_skips_done_items_the_server_cleared() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let (key, item) = (Uuid::new_v4(), Uuid::new_v4());
    r.exec.delete(&approved(&f, key), Some(item)).unwrap();
    r.append(Record::Cleared { outbox_id: item });
    assert!(r.restart().exec.recover().is_empty());
}

#[test]
fn recover_started_unmoved_refuses() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    let r = r.restart();
    let got = recover_twice(&r);
    assert_eq!(got.len(), 1);
    assert_eq!(
        result(&got[0]),
        ("REFUSED", Some("interrupted_before_staging"))
    );
    assert!(r.folder.in_folder("a.txt"), "D-1: left alone");
    assert_eq!(r.journal_kinds(key), ["started", "done"]);
}

#[test]
fn recover_started_but_staged_journals_staged_then_recycles() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.folder.move_to_staging("a.txt", key); // the crash came after the move
    let r = r.restart();
    let got = recover_twice(&r);
    assert_eq!(result(&got[0]), ("RECYCLED", None));
    assert_eq!(r.journal_kinds(key), ["started", "staged", "done"]);
    assert!(r.folder.in_bin(&f.file_id));
}

#[test]
fn recover_started_but_staged_with_wrong_identity_refuses() {
    for tamper in [Tamper::ChangeContent, Tamper::SwapFile] {
        let r = rig();
        let f = r.folder.put("a.txt", b"fictional");
        let key = Uuid::new_v4();
        r.started(key, &f);
        r.folder.move_to_staging("a.txt", key);
        r.folder.tamper_staged_now(key, tamper);
        let r = r.restart();
        let got = recover_twice(&r);
        assert_eq!(got[0]["result"], "REFUSED", "{tamper:?}");
        assert!(r.folder.staged(key), "{tamper:?}: stays in staging");
        assert_eq!(r.folder.recycle_calls(), 0, "{tamper:?}: never recycled");
    }
}

#[test]
fn recover_staged_redoes_step_4() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.folder.move_to_staging("a.txt", key);
    r.append(Record::Staged { execution_key: key });
    let r = r.restart();
    assert_eq!(result(&recover_twice(&r)[0]), ("RECYCLED", None));
    assert!(r.folder.in_bin(&f.file_id));
}

#[test]
fn recover_staged_in_recycle_bin_reports_recycled() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.folder.move_to_staging("a.txt", key);
    r.append(Record::Staged { execution_key: key });
    r.folder.recycle_now(key); // the crash came after the recycle, before Done
    let r = r.restart();
    let got = recover_twice(&r);
    assert_eq!(result(&got[0]), ("RECYCLED", None));
    assert_eq!(got[0]["evidence"]["file_id"], json!(f.file_id));
    assert_eq!(r.folder.recycle_calls(), 1, "only the original recycle");
}

#[test]
fn recover_started_in_recycle_bin_is_error() {
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.folder.move_to_staging("a.txt", key);
    r.folder.recycle_now(key); // something else recycled it before Staged was journalled
    let r = r.restart();
    assert_eq!(
        result(&recover_twice(&r)[0]),
        ("ERROR", Some("outcome_unknown"))
    );
}

#[test]
fn recover_unknown_is_error() {
    // Started: the file is nowhere.
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.folder.remove("a.txt");
    let r = r.restart();
    assert_eq!(
        result(&recover_twice(&r)[0]),
        ("ERROR", Some("outcome_unknown"))
    );
    // Staged, but the file is back in the folder.
    let r = rig();
    let f = r.folder.put("a.txt", b"fictional");
    let key = Uuid::new_v4();
    r.started(key, &f);
    r.append(Record::Staged { execution_key: key });
    let r = r.restart();
    assert_eq!(
        result(&recover_twice(&r)[0]),
        ("ERROR", Some("outcome_unknown"))
    );
    assert!(r.folder.in_folder("a.txt"));
}

// ------------------------------------------------------------------ through the real node

mod wire {
    use super::*;
    use std::time::Duration;
    use support::authz::{live_action, signed};
    use support::net::{recv, recv_within};
    use support::rig::rig;

    fn approved_item(
        node: Uuid,
        facts: &FileFacts,
        key: Uuid,
        outbox_id: Uuid,
        signer: &str,
    ) -> Value {
        let at = now().with_nanosecond(0).unwrap();
        let obj = json!({
            "v": 1, "kind": "node.action", "action_kind": "pc.delete_test_file",
            "args": {"name": facts.name, "size": facts.size, "sha256": facts.sha256, "file_id": facts.file_id},
            "target_node_id": node, "person_id": Uuid::new_v4(),
            "approval_id": Uuid::new_v4(), "execution_key": key, "reply_id": null, "summary": null,
            "issued_at": at.to_rfc3339(), "valid_until": (at + Span::minutes(10)).to_rfc3339(),
        });
        json!({
            "outbox_id": outbox_id, "priority": "URGENT", "kind": "action.approved",
            "body": "Deleting a.txt", "expires_at": obj["valid_until"],
            "authz": signed(signer, "core-1", obj),
        })
    }

    #[tokio::test]
    async fn an_approved_delete_runs_once_then_received_receipt_ack() {
        let mut r = rig().await;
        let f = r.folder.put("a.txt", b"fictional");
        let (key, item) = (Uuid::new_v4(), Uuid::new_v4());
        r.push(
            "outbox.item",
            approved_item(r.node_id, &f, key, item, "core-1"),
        )
        .await;
        let received = recv(&mut r.ws).await;
        assert_eq!(
            (received["kind"].as_str(), &received["payload"]),
            (Some("received"), &json!({"outbox_id": item}))
        );
        let receipt = recv(&mut r.ws).await;
        support::assert_envelope_conforms(&receipt);
        assert_eq!(receipt["kind"], "action.receipt");
        assert_eq!(receipt["payload"]["result"], "RECYCLED");
        assert_conforms("pc.delete_test_file.receipt", &receipt["payload"]);
        let ack = recv(&mut r.ws).await;
        assert_eq!(
            (ack["kind"].as_str(), &ack["payload"]),
            (Some("ack"), &json!({"outbox_id": item}))
        );
        assert!(r.folder.in_bin(&f.file_id));
        // A re-send of the item: the stored receipt again, nothing run twice.
        r.push(
            "outbox.item",
            approved_item(r.node_id, &f, key, item, "core-1"),
        )
        .await;
        assert_eq!(recv(&mut r.ws).await["kind"], "received");
        assert_eq!(recv(&mut r.ws).await["payload"], receipt["payload"]);
        assert_eq!(r.folder.recycle_calls(), 1);
    }

    #[tokio::test]
    async fn a_forged_or_other_nodes_action_is_received_but_never_run() {
        let mut r = rig().await;
        let f = r.folder.put("a.txt", b"fictional");
        // Signed with the wrong key under core-1's name.
        let item = Uuid::new_v4();
        r.push(
            "outbox.item",
            approved_item(r.node_id, &f, Uuid::new_v4(), item, "core-2"),
        )
        .await;
        assert_eq!(recv(&mut r.ws).await["payload"], json!({"outbox_id": item}));
        // For another node.
        let other = Uuid::new_v4();
        r.push(
            "outbox.item",
            approved_item(Uuid::new_v4(), &f, Uuid::new_v4(), other, "core-1"),
        )
        .await;
        assert_eq!(
            recv(&mut r.ws).await["payload"],
            json!({"outbox_id": other})
        );
        assert_eq!(
            recv_within(&mut r.ws, Duration::from_millis(400)).await,
            None,
            "no receipt"
        );
        assert!(r.folder.in_folder("a.txt"));
    }

    #[tokio::test]
    async fn a_live_listing_is_answered_with_its_receipt() {
        let mut r = rig().await;
        r.folder.put("a.txt", b"fictional");
        let reply_id = Uuid::new_v4();
        let mut obj = live_action(r.node_id, reply_id, now());
        obj["action_kind"] = json!("pc.list_test_folder");
        obj["args"] = json!({});
        r.push(
            "node.action",
            json!({"authz": signed("core-1", "core-1", obj), "seq": 1, "in_reply_to": null}),
        )
        .await;
        let receipt = recv(&mut r.ws).await;
        assert_eq!(receipt["kind"], "action.receipt");
        assert_eq!(
            (&receipt["payload"]["reply_id"], &receipt["payload"]["seq"]),
            (&json!(reply_id), &json!(1))
        );
        assert_eq!(receipt["payload"]["evidence"]["files"][0]["name"], "a.txt");
        assert_conforms("pc.list_test_folder.receipt", &receipt["payload"]);
    }
}
