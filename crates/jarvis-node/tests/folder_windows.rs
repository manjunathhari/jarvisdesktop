//! The Windows test folder, on a real NTFS volume (plan B Task 8: P2-A7, P2-A8, P2-A10).
//! Each test works in its own temporary folder; the files are fictional and go to the
//! Recycle Bin, never a permanent delete. Symlink tests need the symlink privilege (Developer
//! Mode or an elevated shell): without it they say so and return, except under CI.
#![cfg(windows)]

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use chrono::{Duration as Span, Timelike};
use jarvis_node::folder::windows::{WindowsFolder, STAGING};
use jarvis_node::folder::{Executor, FolderOps, Located, Outcome};
use jarvis_node::journal::{FileFacts, Journal};
use jarvis_node::wire::{parse_authz_object, AuthzObject};
use serde_json::json;
use uuid::Uuid;

struct Place {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    folder: PathBuf,
}

fn place() -> Place {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_owned();
    let folder = root.join("test-folder");
    std::fs::create_dir(&folder).unwrap();
    Place {
        _tmp: tmp,
        root,
        folder,
    }
}

fn put(folder: &Path, name: &str, content: &[u8]) {
    std::fs::write(folder.join(name), content).unwrap();
}

fn facts(ops: &WindowsFolder, name: &str) -> FileFacts {
    ops.list()
        .unwrap()
        .into_iter()
        .find(|f| f.name == name)
        .unwrap_or_else(|| panic!("{name} not listed"))
}

fn approved(facts: &FileFacts, key: Uuid) -> AuthzObject {
    let at = support::gateway::now().with_nanosecond(0).unwrap();
    parse_authz_object(&json!({
        "v": 1, "kind": "node.action", "action_kind": "pc.delete_test_file",
        "args": {"name": facts.name, "size": facts.size, "sha256": facts.sha256, "file_id": facts.file_id},
        "target_node_id": Uuid::new_v4(), "person_id": Uuid::new_v4(),
        "approval_id": Uuid::new_v4(), "execution_key": key, "reply_id": null, "summary": null,
        "issued_at": at.to_rfc3339(), "valid_until": (at + Span::minutes(10)).to_rfc3339(),
    }))
    .unwrap()
}

fn executor(folder: &Path, journal: &Path) -> Executor {
    let journal = Arc::new(Mutex::new(Journal::open(journal).unwrap()));
    Executor::new(Arc::new(WindowsFolder::new(folder.to_owned())), journal)
}

fn junction(link: &Path, target: &Path) {
    let ok = Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(link)
        .arg(target)
        .status()
        .unwrap()
        .success();
    assert!(ok, "mklink /J failed");
}

/// A symlink, or None (with the reason printed) where the machine lacks the privilege.
fn symlink(make: impl FnOnce() -> std::io::Result<()>) -> Option<()> {
    match make() {
        Ok(()) => Some(()),
        Err(e) if std::env::var_os("CI").is_none() => {
            eprintln!("skipping: can't create a symlink here ({e}); enable Developer Mode");
            None
        }
        Err(e) => panic!("CI must be able to create symlinks: {e}"),
    }
}

// ------------------------------------------------------------------ listing and the folder

#[test]
fn lists_files_with_file_ids() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional one");
    put(&p.folder, "b.log", b"fictional two");
    let ops = WindowsFolder::new(p.folder.clone());
    ops.check_folder().unwrap();
    let files = ops.list().unwrap();
    assert_eq!(files.len(), 2);
    for f in &files {
        let (vol, id) = f.file_id.split_once(':').unwrap();
        assert_eq!((vol.len(), id.len()), (16, 32), "{}", f.file_id);
        assert_eq!(f.sha256.len(), 64);
    }
    assert_ne!(files[0].file_id, files[1].file_id);
}

#[test]
fn listing_skips_links_and_folders() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    std::fs::create_dir(p.folder.join("sub")).unwrap();
    put(&p.root, "outside.txt", b"outside");
    junction(&p.folder.join("jdir"), &p.root);
    put(&p.folder, "a b.txt", b"odd name");
    let linked = symlink(|| {
        std::os::windows::fs::symlink_file(p.root.join("outside.txt"), p.folder.join("link.txt"))
    });
    let ops = WindowsFolder::new(p.folder.clone());
    let names: Vec<String> = ops.list().unwrap().into_iter().map(|f| f.name).collect();
    assert_eq!(names, ["a.txt"], "links {linked:?}");
}

#[test]
fn refuses_folder_that_is_a_junction() {
    let p = place();
    let link = p.root.join("folder-junction");
    junction(&link, &p.folder);
    let ops = WindowsFolder::new(link);
    assert_eq!(
        ops.check_folder(),
        Err(Outcome::Refused("folder_reparse_point"))
    );
    assert!(ops.list().is_err());
}

#[test]
fn refuses_folder_that_is_a_symlink() {
    let p = place();
    let link = p.root.join("folder-link");
    if symlink(|| std::os::windows::fs::symlink_dir(&p.folder, &link)).is_none() {
        return;
    }
    assert_eq!(
        WindowsFolder::new(link).check_folder(),
        Err(Outcome::Refused("folder_reparse_point"))
    );
}

#[test]
fn refuses_file_symlink() {
    let p = place();
    put(&p.root, "outside.txt", b"fictional");
    if symlink(|| {
        std::os::windows::fs::symlink_file(p.root.join("outside.txt"), p.folder.join("a.txt"))
    })
    .is_none()
    {
        return;
    }
    let ops = WindowsFolder::new(p.folder.clone());
    // Approve the target's facts under the link's name: the link is opened as itself.
    let target = WindowsFolder::new(p.root.clone());
    let mut f = facts(&target, "outside.txt");
    f.name = "a.txt".into();
    assert_eq!(
        ops.stage(&f, Uuid::new_v4()),
        Err(Outcome::Refused("not_regular_file"))
    );
    assert!(p.root.join("outside.txt").exists());
}

#[test]
fn refuses_file_junction() {
    // A junction (to a folder) standing where the approved file was: refused, not an error.
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    std::fs::remove_file(p.folder.join("a.txt")).unwrap();
    junction(&p.folder.join("a.txt"), &p.root);
    assert_eq!(
        ops.stage(&f, Uuid::new_v4()),
        Err(Outcome::Refused("not_regular_file"))
    );
    assert!(p.root.exists());
}

#[test]
fn hard_links_are_left_out_of_the_listing() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    std::fs::hard_link(p.folder.join("a.txt"), p.folder.join("b.txt")).unwrap();
    put(&p.root, "outside.txt", b"linked from outside");
    std::fs::hard_link(p.root.join("outside.txt"), p.folder.join("d.txt")).unwrap();
    put(&p.folder, "c.txt", b"other");
    let ops = WindowsFolder::new(p.folder.clone());
    let names: Vec<String> = ops.list().unwrap().into_iter().map(|f| f.name).collect();
    assert_eq!(names, ["c.txt"]);
}

#[test]
fn refuses_non_ntfs_or_missing_folder() {
    let p = place();
    let missing = WindowsFolder::new(p.root.join("nope"));
    assert_eq!(
        missing.check_folder(),
        Err(Outcome::Refused("folder_missing"))
    );
    let relative = WindowsFolder::new(PathBuf::from("relative\\folder"));
    assert_eq!(
        relative.check_folder(),
        Err(Outcome::Refused("folder_not_absolute"))
    );
    let network = WindowsFolder::new(PathBuf::from("\\\\server\\share\\folder"));
    assert_eq!(
        network.check_folder(),
        Err(Outcome::Refused("network_path"))
    );
}

// ------------------------------------------------------------------ the delete

#[test]
fn delete_goes_to_recycle_bin() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional, for the Recycle Bin");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    let key = Uuid::new_v4();
    let exec = executor(&p.folder, &p.root.join("journal.jsonl"));
    let receipt = exec.delete(&approved(&f, key), None).unwrap();
    assert_eq!(receipt["result"], "RECYCLED", "{receipt}");
    assert!(!p.folder.join("a.txt").exists());
    assert_eq!(ops.locate(&f, key), Located::InRecycleBin);
    support::assert_conforms("pc.delete_test_file.receipt", &receipt);
}

#[test]
fn swapped_file_is_mismatch() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    std::fs::remove_file(p.folder.join("a.txt")).unwrap();
    put(&p.folder, "a.txt", b"fictional"); // same content, new file ID
    assert_eq!(
        ops.stage(&f, Uuid::new_v4()),
        Err(Outcome::Mismatch("file_id"))
    );
    assert!(p.folder.join("a.txt").exists());
}

// ------------------------------------------------------------------ P2-A10: tampered staging

/// Stage `a.txt`, tamper, then step 4: refused, and the file is not recycled.
fn staged_then(tamper: impl FnOnce(&Path, &Path), check: &'static str) {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    let key = Uuid::new_v4();
    ops.stage(&f, key).unwrap();
    let sub = p.folder.join(STAGING).join(key.to_string());
    assert!(sub.join("a.txt").exists());
    tamper(&sub, &p.root);
    assert_eq!(ops.recycle_staged(&f, key), Err(Outcome::Refused(check)));
    assert_ne!(ops.locate(&f, key), Located::InRecycleBin);
}

#[test]
fn staging_replaced_by_junction_refused() {
    staged_then(
        |sub, root| {
            let moved = root.join("moved-staging");
            std::fs::rename(sub, &moved).unwrap();
            junction(sub, &moved);
        },
        "staging_reparse_point",
    );
}

#[test]
fn staging_dacl_changed_refused() {
    staged_then(
        |sub, _| {
            let ok = Command::new("icacls")
                .arg(sub)
                .args(["/grant", "*S-1-1-0:R"])
                .status()
                .unwrap()
                .success();
            assert!(ok, "icacls failed");
        },
        "staging_dacl",
    );
}

#[test]
fn staged_file_swapped_refused() {
    staged_then(
        |sub, _| {
            std::fs::remove_file(sub.join("a.txt")).unwrap();
            std::fs::write(sub.join("a.txt"), b"fictional").unwrap(); // new file ID
        },
        "file_id",
    );
}

// ------------------------------------------------------------------ P2-A8: kill and recover

/// Run `child_crash_delete` in a child process that aborts at `at`.
fn crash_at(p: &Place, f: &FileFacts, key: Uuid, at: &str) {
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "child_crash_delete", "--ignored", "--nocapture"])
        .env("JARVIS_NODE_CRASH_AT", at)
        .env("JARVIS_TEST_ROOT", &p.root)
        .env("JARVIS_TEST_KEY", key.to_string())
        .env("JARVIS_TEST_FACTS", serde_json::to_string(f).unwrap())
        .status()
        .unwrap();
    assert!(!status.success(), "the child must have aborted at {at}");
}

#[test]
#[ignore = "run by the P2-A8 tests in a child process"]
fn child_crash_delete() {
    let root = PathBuf::from(std::env::var("JARVIS_TEST_ROOT").unwrap());
    let key: Uuid = std::env::var("JARVIS_TEST_KEY").unwrap().parse().unwrap();
    let f: FileFacts = serde_json::from_str(&std::env::var("JARVIS_TEST_FACTS").unwrap()).unwrap();
    let exec = executor(&root.join("test-folder"), &root.join("journal.jsonl"));
    exec.delete(&approved(&f, key), None);
    panic!("the crash point was not reached");
}

fn kill_and_recover(at: &str) {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    let key = Uuid::new_v4();
    crash_at(&p, &f, key, at);
    assert_eq!(
        ops.locate(&f, key),
        Located::Staged,
        "the crash came after the move"
    );
    let exec = executor(&p.folder, &p.root.join("journal.jsonl"));
    let first = exec.recover();
    assert_eq!(first.len(), 1);
    assert_eq!(
        first[0].receipt["result"], "RECYCLED",
        "{}",
        first[0].receipt
    );
    assert_eq!(ops.locate(&f, key), Located::InRecycleBin);
    let again = exec.recover();
    assert_eq!(again, first, "the stored receipt, nothing more");
}

#[test]
fn p2_a8_kill_between_staged_and_done() {
    kill_and_recover("staged");
}

#[test]
fn p2_a8_kill_between_move_and_staged() {
    kill_and_recover("moved");
}

#[test]
fn p2_a8_swapped_after_the_crash_is_refused() {
    let p = place();
    put(&p.folder, "a.txt", b"fictional");
    let ops = WindowsFolder::new(p.folder.clone());
    let f = facts(&ops, "a.txt");
    let key = Uuid::new_v4();
    crash_at(&p, &f, key, "moved");
    let staged = p.folder.join(STAGING).join(key.to_string()).join("a.txt");
    std::fs::remove_file(&staged).unwrap();
    std::fs::write(&staged, b"fictional").unwrap();
    let exec = executor(&p.folder, &p.root.join("journal.jsonl"));
    let got = exec.recover();
    assert_eq!(got[0].receipt["result"], "REFUSED", "{}", got[0].receipt);
    assert!(staged.exists(), "nothing recycled");
}
