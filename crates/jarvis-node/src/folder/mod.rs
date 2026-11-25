//! The test folder: its listing and the approved Recycle Bin delete (spec §9.4, plan B
//! Task 8).
//!
//! `Executor` holds the platform-neutral rules: the name rule, rule 5 (an approval and an
//! unused `execution_key`), the journal order STARTED → (move) → STAGED → (recycle) → DONE,
//! the receipts, and recovery after a crash (the Recovery table in the plan's Decisions). The
//! file system work is behind `FolderOps`: `windows.rs` does it as §9.4 says, step for step;
//! `memory.rs` is the test double.
//!
//! Every receipt for an `execution_key` is journalled as DONE before it is returned, so it is
//! sent once and re-sent, never recomputed. The exceptions are refusals decided from the signed
//! object alone (no approval, bad args or name, an unknown kind): those need no journal, since
//! the same object always gets the same receipt, and nothing on disk is touched.

use std::sync::{Arc, Mutex};

use chrono::{DateTime, FixedOffset};
use serde_json::{json, Map, Value};
use uuid::Uuid;

use crate::journal::{FileFacts, Journal, Record, Stage};
use crate::wire::AuthzObject;

#[cfg(any(test, feature = "test-support"))]
pub mod memory;
#[cfg(windows)]
pub mod windows;

/// The approved, irreversible action this executor runs.
pub const DELETE: &str = "pc.delete_test_file";
/// The live, read-only listing.
pub const LISTING: &str = "pc.list_test_folder";
/// The most files a listing carries (the receipt schema's cap).
pub const MAX_LISTED: usize = 1000;

/// A plain file name (§9.4): `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`, and no Windows device name
/// (CON, PRN, AUX, NUL, COM1–9, LPT1–9), with or without an extension.
pub fn valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    let plain = |b: &u8| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-');
    if bytes.is_empty() || bytes.len() > 100 || !bytes[0].is_ascii_alphanumeric() {
        return false;
    }
    if !bytes.iter().all(plain) {
        return false;
    }
    let stem = name.split('.').next().unwrap_or("").to_ascii_lowercase();
    let device = matches!(stem.as_str(), "con" | "prn" | "aux" | "nul")
        || (stem.len() == 4
            && (stem.starts_with("com") || stem.starts_with("lpt"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'));
    !device
}

/// Why a folder step didn't happen. The `&'static str` is the receipt's `check`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The file isn't the approved one (steps 1 and 2): MISMATCH.
    Mismatch(&'static str),
    /// No file of that name (steps 1 and 2): NOT_FOUND.
    NotFound,
    /// A precondition couldn't be established; nothing was changed: REFUSED.
    Refused(&'static str),
    /// Something failed part-way or isn't as expected afterwards: ERROR.
    Error(&'static str),
}

/// Where an execution's file is now (for recovery).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Located {
    /// Directly in the test folder, with the approved file ID.
    InFolder,
    /// At `<staging>\<execution_key>\<name>` (whatever its identity: step 4 checks that).
    Staged,
    /// In the Recycle Bin, with the approved file ID.
    InRecycleBin,
    Unknown,
}

/// The file system side of §9.4.
pub trait FolderOps: Send + Sync {
    /// The folder is configured, exists, is not a reparse point, and is on a local fixed
    /// NTFS volume.
    fn check_folder(&self) -> Result<(), Outcome>;
    /// The folder's regular files with valid names (links, folders and odd names left out).
    fn list(&self) -> Result<Vec<FileFacts>, Outcome>;
    /// Steps 1 to 3: open exclusively, check identity through the handle, move into
    /// `<staging>\<key>\`.
    fn stage(&self, facts: &FileFacts, key: Uuid) -> Result<(), Outcome>;
    /// Step 4: re-check the staging and the file (ID, size, SHA-256, path) through a handle,
    /// recycle, and confirm through the handle that it is in the Recycle Bin.
    fn recycle_staged(
        &self,
        facts: &FileFacts,
        key: Uuid,
    ) -> Result<DateTime<FixedOffset>, Outcome>;
    fn locate(&self, facts: &FileFacts, key: Uuid) -> Located;
}

/// No test folder configured: every action is refused.
pub struct NoFolder;

impl FolderOps for NoFolder {
    fn check_folder(&self) -> Result<(), Outcome> {
        Err(Outcome::Refused("no_test_folder"))
    }
    fn list(&self) -> Result<Vec<FileFacts>, Outcome> {
        Err(Outcome::Refused("no_test_folder"))
    }
    fn stage(&self, _: &FileFacts, _: Uuid) -> Result<(), Outcome> {
        Err(Outcome::Refused("no_test_folder"))
    }
    fn recycle_staged(&self, _: &FileFacts, _: Uuid) -> Result<DateTime<FixedOffset>, Outcome> {
        Err(Outcome::Refused("no_test_folder"))
    }
    fn locate(&self, _: &FileFacts, _: Uuid) -> Located {
        Located::Unknown
    }
}

/// Whether a staging folder's DACL, as SDDL read back from its handle (§9.4 step 4), is
/// protected (`P`: nothing inherited from above) and every entry is an allow for SYSTEM or the
/// signed-in user, none of them inherited, with at least the user's.
pub fn sddl_is_protected_for(sddl: &str, user_sid: &str) -> bool {
    let Some(rest) = sddl.strip_prefix("D:") else {
        return false;
    };
    let (flags, aces) = rest.split_at(rest.find('(').unwrap_or(rest.len()));
    if !flags.contains('P') {
        return false;
    }
    let mut has_user = false;
    for ace in aces.split(')').filter(|a| !a.is_empty()) {
        let parts: Vec<&str> = ace.trim_start_matches('(').split(';').collect();
        let [kind, ace_flags, _rights, _, _, sid] = parts[..] else {
            return false;
        };
        if kind != "A" || ace_flags.contains("ID") {
            return false;
        }
        match sid {
            "SY" | "S-1-5-18" => {}
            s if s == user_sid => has_user = true,
            _ => return false,
        }
    }
    has_user
}

/// A receipt to (re)send after a restart, and the item to ack once it is sent.
#[derive(Debug, Clone, PartialEq)]
pub struct Recovered {
    pub outbox_id: Option<Uuid>,
    pub receipt: Value,
}

pub struct Executor {
    ops: Arc<dyn FolderOps>,
    journal: Arc<Mutex<Journal>>,
}

impl Executor {
    pub fn new(ops: Arc<dyn FolderOps>, journal: Arc<Mutex<Journal>>) -> Self {
        Self { ops, journal }
    }

    /// The `pc.list_test_folder` receipt for a live action: OK with up to 1000 files sorted by
    /// name, or REFUSED / ERROR with none.
    pub fn list_receipt(&self, reply_id: Uuid, seq: i64) -> Value {
        let listed = self.ops.check_folder().and_then(|()| self.ops.list());
        let (result, files) = match listed {
            Ok(mut files) => {
                files.retain(|f| valid_name(&f.name));
                files.sort_by(|a, b| a.name.cmp(&b.name));
                files.truncate(MAX_LISTED);
                ("OK", files)
            }
            Err(o) => {
                tracing::warn!(?o, "test folder listing refused");
                match o {
                    Outcome::Error(_) => ("ERROR", vec![]),
                    _ => ("REFUSED", vec![]),
                }
            }
        };
        let files: Vec<Value> = files.iter().map(file_json).collect();
        json!({"reply_id": reply_id, "seq": seq, "result": result, "evidence": {"files": files}})
    }

    /// Run an approved delete (a `node.action` that passed §7.1 rules 2 to 4). The receipt to
    /// send, or None: no `execution_key` (nothing can be receipted), the key is already being
    /// executed, or the journal failed after the file moved (recovery finishes it).
    pub fn delete(&self, obj: &AuthzObject, outbox_id: Option<Uuid>) -> Option<Value> {
        let key = obj.execution_key?;
        // From the signed object alone: same object, same receipt; nothing touched.
        if obj.action_kind != DELETE {
            return Some(receipt(
                key,
                "REFUSED",
                None,
                None,
                Some("unknown_action_kind"),
            ));
        }
        if obj.approval_id.is_none() {
            return Some(receipt(key, "REFUSED", None, None, Some("no_approval")));
        }
        let Some(facts) = file_facts(&obj.args) else {
            return Some(receipt(key, "REFUSED", None, None, Some("bad_args")));
        };
        if !valid_name(&facts.name) {
            return Some(receipt(key, "REFUSED", None, None, Some("bad_name")));
        }
        // Rule 5, and STARTED fsync'd before step 1, under one lock.
        {
            let mut journal = self.journal.lock().expect("journal lock");
            match journal.state().executions.get(&key) {
                Some(Stage::Done(stored)) => return Some(stored.clone()),
                Some(_) => {
                    tracing::warn!(%key, "execution already in progress; not run twice");
                    return None;
                }
                None => {}
            }
            let started = Record::Started {
                execution_key: key,
                args: facts.clone(),
                at: chrono::Utc::now().fixed_offset(),
                outbox_id,
            };
            if let Err(e) = journal.append(&started) {
                tracing::error!(error = %e, %key, "couldn't journal STARTED; nothing done");
                return Some(receipt(
                    key,
                    "REFUSED",
                    None,
                    None,
                    Some("journal_unavailable"),
                ));
            }
        }
        if let Err(o) = self.ops.check_folder() {
            return self.finish(key, step_1_to_3(key, o));
        }
        if let Err(o) = self.ops.stage(&facts, key) {
            return self.finish(key, step_1_to_3(key, o));
        }
        crash_point("moved");
        if let Err(e) = self.append(Record::Staged { execution_key: key }) {
            tracing::error!(error = %e, %key, "couldn't journal STAGED; recovery will finish it");
            return None;
        }
        crash_point("staged");
        self.step_4(key, &facts)
    }

    /// After a restart: every execution's receipt, following the Recovery table exactly.
    /// Each is journalled as DONE first, so a second call returns the same receipts and does
    /// nothing more. DONE receipts whose item the server has cleared are not re-sent.
    pub fn recover(&self) -> Vec<Recovered> {
        let (mut work, cleared, outbox_of) = {
            let journal = self.journal.lock().expect("journal lock");
            let state = journal.state();
            let work: Vec<(Uuid, Stage)> = state
                .executions
                .iter()
                .map(|(k, s)| (*k, s.clone()))
                .collect();
            (work, state.cleared.clone(), state.outbox_of.clone())
        };
        work.sort_by_key(|(k, _)| *k);
        let mut out = Vec::new();
        for (key, stage) in work {
            let outbox_id = outbox_of.get(&key).copied();
            let receipt = match stage {
                Stage::Done(stored) => {
                    if outbox_id.is_some_and(|o| cleared.contains(&o)) {
                        continue; // the server has it: answered, never re-sent
                    }
                    Some(stored)
                }
                Stage::Started(facts) => match self.ops.locate(&facts, key) {
                    Located::InFolder => self.finish(
                        key,
                        receipt(
                            key,
                            "REFUSED",
                            None,
                            None,
                            Some("interrupted_before_staging"),
                        ),
                    ),
                    Located::Staged => {
                        // Moved, then the crash: this continues the same approved execution.
                        match self.append(Record::Staged { execution_key: key }) {
                            Ok(()) => self.step_4(key, &facts),
                            Err(e) => {
                                tracing::error!(error = %e, %key, "couldn't journal STAGED");
                                None
                            }
                        }
                    }
                    Located::InRecycleBin | Located::Unknown => self.finish(key, unknown(key)),
                },
                Stage::Staged(facts) => match self.ops.locate(&facts, key) {
                    Located::Staged => self.step_4(key, &facts),
                    Located::InRecycleBin => {
                        // The crash came after the recycle, before DONE. The time is now's:
                        // the moment itself wasn't recorded.
                        let at = chrono::Utc::now().fixed_offset();
                        self.finish(key, receipt(key, "RECYCLED", Some(&facts), Some(at), None))
                    }
                    Located::InFolder | Located::Unknown => self.finish(key, unknown(key)),
                },
            };
            if let Some(receipt) = receipt {
                out.push(Recovered { outbox_id, receipt });
            }
        }
        out
    }

    /// DONE receipts not yet known to have arrived: their item is neither cleared by the
    /// server nor acked here (the ack follows a sent receipt). Re-sent after each reconnect.
    pub fn owed_receipts(&self) -> Vec<Recovered> {
        let journal = self.journal.lock().expect("journal lock");
        let state = journal.state();
        let mut owed: Vec<Recovered> = state
            .executions
            .iter()
            .filter_map(|(key, stage)| {
                let Stage::Done(receipt) = stage else {
                    return None;
                };
                let outbox_id = *state.outbox_of.get(key)?;
                (!state.cleared.contains(&outbox_id) && !state.acked.contains_key(&outbox_id)).then(
                    || Recovered {
                        outbox_id: Some(outbox_id),
                        receipt: receipt.clone(),
                    },
                )
            })
            .collect();
        owed.sort_by_key(|r| r.outbox_id);
        owed
    }

    fn step_4(&self, key: Uuid, facts: &FileFacts) -> Option<Value> {
        let r = match self.ops.recycle_staged(facts, key) {
            Ok(at) => receipt(key, "RECYCLED", Some(facts), Some(at), None),
            // Any failed precondition: the file stays in staging (still recoverable).
            Err(Outcome::Mismatch(check) | Outcome::Refused(check)) => {
                receipt(key, "REFUSED", None, None, Some(check))
            }
            Err(Outcome::NotFound) => receipt(key, "REFUSED", None, None, Some("staged_missing")),
            Err(Outcome::Error(check)) => receipt(key, "ERROR", None, None, Some(check)),
        };
        self.finish(key, r)
    }

    /// Journal DONE with the receipt, then return it. If DONE can't be written the receipt is
    /// still right, so it is returned; a restart reaches the same outcome from the file.
    fn finish(&self, key: Uuid, receipt: Value) -> Option<Value> {
        let done = Record::Done {
            execution_key: key,
            receipt: receipt.clone(),
        };
        if let Err(e) = self.append(done) {
            tracing::error!(error = %e, %key, "couldn't journal DONE");
        }
        tracing::info!(%key, result = %receipt["result"], "delete finished");
        Some(receipt)
    }

    fn append(&self, r: Record) -> Result<(), crate::journal::JournalError> {
        self.journal.lock().expect("journal lock").append(&r)
    }
}

fn step_1_to_3(key: Uuid, o: Outcome) -> Value {
    match o {
        Outcome::Mismatch(check) => receipt(key, "MISMATCH", None, None, Some(check)),
        Outcome::NotFound => receipt(key, "NOT_FOUND", None, None, None),
        Outcome::Refused(check) => receipt(key, "REFUSED", None, None, Some(check)),
        Outcome::Error(check) => receipt(key, "ERROR", None, None, Some(check)),
    }
}

fn unknown(key: Uuid) -> Value {
    receipt(key, "ERROR", None, None, Some("outcome_unknown"))
}

/// The delete receipt. `file` is set only for RECYCLED: then the approved ID and hash are the
/// ones read through the handle before recycling (step 4 checked them equal).
fn receipt(
    key: Uuid,
    result: &str,
    file: Option<&FileFacts>,
    recycled_at: Option<DateTime<FixedOffset>>,
    check: Option<&str>,
) -> Value {
    let mut evidence = json!({
        "file_id": file.map(|f| f.file_id.clone()),
        "before_sha256": file.map(|f| f.sha256.clone()),
        "recycled_at": recycled_at.map(|t| t.to_rfc3339()),
    });
    if let Some(check) = check {
        evidence["check"] = json!(check);
    }
    json!({"execution_key": key, "result": result, "evidence": evidence})
}

fn file_json(f: &FileFacts) -> Value {
    json!({"name": f.name, "size": f.size, "sha256": f.sha256, "file_id": f.file_id})
}

/// The approved file object, exactly `pc.delete_test_file.args`.
fn file_facts(args: &Map<String, Value>) -> Option<FileFacts> {
    let facts: FileFacts = serde_json::from_value(Value::Object(args.clone())).ok()?;
    let hex = |s: &str, n: usize| {
        s.len() == n && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    };
    let id_ok = facts
        .file_id
        .split_once(':')
        .is_some_and(|(vol, id)| hex(vol, 16) && hex(id, 32));
    (hex(&facts.sha256, 64) && id_ok).then_some(facts)
}

/// Test hook: `JARVIS_NODE_CRASH_AT=moved` or `staged` aborts the process there (P2-A8).
#[cfg(any(test, feature = "test-support"))]
fn crash_point(at: &str) {
    if std::env::var("JARVIS_NODE_CRASH_AT").as_deref() == Ok(at) {
        std::process::abort();
    }
}

#[cfg(not(any(test, feature = "test-support")))]
fn crash_point(_: &str) {}

#[cfg(test)]
mod tests {
    use super::sddl_is_protected_for;

    const ME: &str = "S-1-5-21-1-2-3-1001";

    #[test]
    fn the_staging_dacl_is_protected_and_only_for_the_user_and_system() {
        for ok in [
            "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            "D:P(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
        ] {
            assert!(sddl_is_protected_for(ok, ME), "{ok}");
        }
        for bad in [
            // Not protected: inheritance from above is on.
            "D:AI(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            // Another principal (Everyone, Administrators, another user).
            "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)(A;;FR;;;WD)",
            "D:P(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            "D:P(A;OICI;FA;;;S-1-5-21-1-2-3-1002)",
            // An inherited entry, or a deny, or no entry for the user.
            "D:P(A;OICIID;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            "D:P(D;;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
            "D:P(A;OICI;FA;;;SY)",
            "D:P",
            "",
            "O:SYD:P(A;OICI;FA;;;S-1-5-21-1-2-3-1001)",
        ] {
            assert!(!sddl_is_protected_for(bad, ME), "{bad}");
        }
    }
}
