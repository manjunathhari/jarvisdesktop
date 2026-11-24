//! The node's durable journal (spec §9.4, §8.2): an append-only JSON-lines file in the app's
//! data folder.
//!
//! It holds the outbox seen-list (`seen`, `cleared`) and every irreversible execution's
//! progress (`started` → `staged` → `done` with its receipt). Each record is one line, written
//! and then `sync_data`'d before `append` returns, so a caller acts only on what is durable.
//!
//! On open the whole file is replayed:
//! - a final line without its newline is a write torn by a crash or power cut: it is dropped
//!   and the file truncated back to the last complete line (the caller never saw it succeed);
//! - any complete line that doesn't parse, or a stage that goes backwards, fails closed: the
//!   journal won't open, so the node runs no action until a person looks at it.
//!
//! After a failed write the file is cut back to its last good length and the journal refuses
//! every later append (`Poisoned`), so a half-written line can never sit in the middle.
//!
//! One process owns the journal: the app runs as a single instance. On Windows the file is
//! opened with read sharing only, so no other process can write, rename or delete it while it
//! is open; on Unix nothing enforces this.

use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

/// The approved file's identity (spec §9.3): name, size, SHA-256 and Windows file ID.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileFacts {
    pub name: String,
    pub size: u64,
    pub sha256: String,
    pub file_id: String,
}

/// One journal line, tagged by `"t"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "t", rename_all = "lowercase", deny_unknown_fields)]
pub enum Record {
    /// An outbox item was shown (or handled) on this node.
    Seen { outbox_id: Uuid },
    /// The server cleared an outbox item; it is never shown after this.
    Cleared { outbox_id: Uuid },
    /// The person saw this item: its `ack` is owed to the server, and is re-sent after
    /// reconnects until the server clears it here or the intent is a week old (§8.2: a late
    /// `ack` changes nothing else).
    Acked {
        outbox_id: Uuid,
        at: DateTime<FixedOffset>,
    },
    /// Written before step 1 of §9.4.
    Started {
        execution_key: Uuid,
        args: FileFacts,
        at: DateTime<FixedOffset>,
    },
    /// The file was moved into `.jarvis-pending\<execution_key>`.
    Staged { execution_key: Uuid },
    /// The execution ended; `receipt` is the `action.receipt` payload to (re)send.
    Done { execution_key: Uuid, receipt: Value },
}

/// Where an execution has got to.
#[derive(Debug, Clone, PartialEq)]
pub enum Stage {
    Started(FileFacts),
    Staged(FileFacts),
    Done(Value),
}

/// What the journal says, rebuilt from every line.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct JournalState {
    pub seen: HashSet<Uuid>,
    pub cleared: HashSet<Uuid>,
    /// When each ack intent was recorded.
    pub acked: HashMap<Uuid, DateTime<FixedOffset>>,
    pub executions: HashMap<Uuid, Stage>,
}

#[derive(Debug, thiserror::Error)]
pub enum JournalError {
    #[error("journal I/O: {0}")]
    Io(#[from] std::io::Error),
    /// A complete line that isn't a valid record (1-based line number).
    #[error("journal line {line} is corrupt")]
    Corrupt { line: usize },
    /// A stage that doesn't follow Started → (Staged) → Done.
    #[error("execution {execution_key} would move out of order")]
    OutOfOrder { execution_key: Uuid },
    /// A write failed earlier; nothing more is written until the journal is reopened.
    #[error("the journal refused writes after an earlier failure")]
    Poisoned,
}

impl JournalState {
    /// Apply one record, or say why it can't follow what is already there.
    ///
    /// `seen` and `cleared` may repeat. An execution moves only forward: `started` once, then
    /// optionally `staged`, then `done` exactly once (a refusal before staging goes straight
    /// from `started` to `done`).
    fn apply(&mut self, record: &Record) -> Result<(), JournalError> {
        self.check(record)?;
        self.apply_checked(record);
        Ok(())
    }

    /// Whether `record` may follow the current state.
    fn check(&self, record: &Record) -> Result<(), JournalError> {
        let ok = match record {
            Record::Seen { .. } | Record::Cleared { .. } | Record::Acked { .. } => true,
            Record::Started { execution_key, .. } => !self.executions.contains_key(execution_key),
            Record::Staged { execution_key } => {
                matches!(self.executions.get(execution_key), Some(Stage::Started(_)))
            }
            Record::Done { execution_key, .. } => matches!(
                self.executions.get(execution_key),
                Some(Stage::Started(_) | Stage::Staged(_))
            ),
        };
        match record {
            _ if ok => Ok(()),
            Record::Started { execution_key, .. }
            | Record::Staged { execution_key }
            | Record::Done { execution_key, .. } => Err(JournalError::OutOfOrder {
                execution_key: *execution_key,
            }),
            Record::Seen { .. } | Record::Cleared { .. } | Record::Acked { .. } => {
                unreachable!("always allowed")
            }
        }
    }

    /// Apply a record that `check` accepted.
    fn apply_checked(&mut self, record: &Record) {
        match record {
            Record::Seen { outbox_id } => {
                self.seen.insert(*outbox_id);
            }
            Record::Cleared { outbox_id } => {
                self.cleared.insert(*outbox_id);
            }
            Record::Acked { outbox_id, at } => {
                self.acked.entry(*outbox_id).or_insert(*at);
            }
            Record::Started {
                execution_key,
                args,
                ..
            } => {
                self.executions
                    .insert(*execution_key, Stage::Started(args.clone()));
            }
            Record::Staged { execution_key } => {
                if let Some(Stage::Started(args)) = self.executions.remove(execution_key) {
                    self.executions.insert(*execution_key, Stage::Staged(args));
                }
            }
            Record::Done {
                execution_key,
                receipt,
            } => {
                self.executions
                    .insert(*execution_key, Stage::Done(receipt.clone()));
            }
        }
    }
}

/// The open journal. Not shared between processes; one node owns its data folder.
#[derive(Debug)]
pub struct Journal {
    path: PathBuf,
    file: File,
    /// Length of the file up to its last complete, durable line.
    len: u64,
    state: JournalState,
    poisoned: bool,
    /// Test hook: the next write puts this many bytes on disk, then fails.
    #[cfg(test)]
    fail_after: Option<usize>,
}

impl Journal {
    /// Open (creating if missing) and replay the journal at `path`.
    pub fn open(path: &Path) -> Result<Journal, JournalError> {
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        // One writer only: while the node has it open, nothing else may write, rename or
        // delete the journal (std's default would allow all three).
        #[cfg(windows)]
        std::os::windows::fs::OpenOptionsExt::share_mode(
            &mut options,
            0x1, /* FILE_SHARE_READ */
        );
        let mut file = options.open(path)?;
        // Make the file's folder entry durable on every open, not only on creation: a crash
        // between creating the file and syncing its folder would otherwise never be repaired.
        file.sync_all()?;
        crate::state::sync_parent_dir(path)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;

        // Everything after the last newline is a torn write: drop it.
        let mut complete = bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        // A final line holding a NUL byte is torn too: pages of one write can reach the disk
        // out of order, and an unwritten page reads back as zeros. JSON never contains a raw
        // NUL, so no line `append` confirmed can look like this. Only the last line is ever in
        // flight, so only it is checked; a NUL anywhere earlier stays corrupt.
        if complete > 0 {
            let start = bytes[..complete - 1]
                .iter()
                .rposition(|b| *b == b'\n')
                .map_or(0, |i| i + 1);
            if bytes[start..complete].contains(&0) {
                complete = start;
            }
        }
        if complete < bytes.len() {
            file.set_len(complete as u64)?;
            file.sync_all()?;
        }

        // `complete` is 0 or ends at a newline; without that final newline, every piece
        // between newlines is one line (an empty piece is a blank line, which is corrupt).
        let mut state = JournalState::default();
        if complete > 0 {
            for (n, line) in bytes[..complete - 1].split(|b| *b == b'\n').enumerate() {
                let record: Record = serde_json::from_slice(line)
                    .map_err(|_| JournalError::Corrupt { line: n + 1 })?;
                state.apply(&record)?;
            }
        }
        file.seek(SeekFrom::Start(complete as u64))?;
        Ok(Journal {
            path: path.to_owned(),
            file,
            len: complete as u64,
            state,
            poisoned: false,
            #[cfg(test)]
            fail_after: None,
        })
    }

    /// Append one record durably: check it against the state, write the line, `sync_data`,
    /// then apply it. On any failure nothing is applied.
    pub fn append(&mut self, record: &Record) -> Result<(), JournalError> {
        if self.poisoned {
            return Err(JournalError::Poisoned);
        }
        self.state.check(record)?;
        let mut line = serde_json::to_vec(record).expect("a record always serialises");
        line.push(b'\n');
        if let Err(e) = self.write_line(&line) {
            // Cut any partial line back off, and write nothing more until reopened.
            self.poisoned = true;
            let _ = self.file.set_len(self.len);
            let _ = self.file.seek(SeekFrom::Start(self.len));
            let _ = self.file.sync_all();
            return Err(e.into());
        }
        self.len += line.len() as u64;
        self.state.apply_checked(record);
        Ok(())
    }

    fn write_line(&mut self, line: &[u8]) -> std::io::Result<()> {
        #[cfg(test)]
        if let Some(n) = self.fail_after.take() {
            self.file.write_all(&line[..n.min(line.len())])?;
            return Err(std::io::Error::other("injected write failure"));
        }
        self.file.write_all(line)?;
        self.file.sync_data()
    }

    pub fn state(&self) -> &JournalState {
        &self.state
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn facts() -> FileFacts {
        FileFacts {
            name: "old-notes.txt".into(),
            size: 12,
            sha256: "0".repeat(64),
            file_id: format!("{}:{}", "1".repeat(16), "2".repeat(32)),
        }
    }

    fn at() -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339("2026-11-24T10:00:00+05:30").unwrap()
    }

    fn journal() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal.jsonl");
        (dir, path)
    }

    #[test]
    fn appends_survive_reopen() {
        let (_dir, path) = journal();
        let (item, key) = (Uuid::new_v4(), Uuid::new_v4());
        {
            let mut j = Journal::open(&path).unwrap();
            j.append(&Record::Seen { outbox_id: item }).unwrap();
            j.append(&Record::Started {
                execution_key: key,
                args: facts(),
                at: at(),
            })
            .unwrap();
            j.append(&Record::Staged { execution_key: key }).unwrap();
        }
        let j = Journal::open(&path).unwrap();
        assert!(j.state().seen.contains(&item));
        assert_eq!(j.state().executions[&key], Stage::Staged(facts()));
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 3);
        assert!(text.ends_with('\n'));
        assert!(text.lines().next().unwrap().contains(r#""t":"seen""#));
    }

    #[test]
    fn torn_final_line_is_dropped() {
        let (_dir, path) = journal();
        let (a, b, c) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        {
            let mut j = Journal::open(&path).unwrap();
            j.append(&Record::Seen { outbox_id: a }).unwrap();
            j.append(&Record::Seen { outbox_id: b }).unwrap();
        }
        let good_len = std::fs::metadata(&path).unwrap().len();
        // A third record cut off mid-write (no newline).
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        f.write_all(br#"{"t":"seen","outbox_id":"#).unwrap();
        drop(f);
        let mut j = Journal::open(&path).unwrap();
        assert_eq!(j.state().seen, HashSet::from([a, b]));
        assert_eq!(std::fs::metadata(&path).unwrap().len(), good_len);
        j.append(&Record::Seen { outbox_id: c }).unwrap();
        drop(j);
        let j = Journal::open(&path).unwrap();
        assert_eq!(j.state().seen, HashSet::from([a, b, c]));
    }

    #[test]
    fn a_complete_record_without_its_newline_is_also_dropped() {
        // The write was never confirmed to the caller, so it never happened.
        let (_dir, path) = journal();
        let a = Uuid::new_v4();
        std::fs::write(&path, format!(r#"{{"t":"seen","outbox_id":"{a}"}}"#)).unwrap();
        let j = Journal::open(&path).unwrap();
        assert!(j.state().seen.is_empty());
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 0);
    }

    #[test]
    fn a_final_line_with_nul_bytes_is_torn_not_corrupt() {
        // Out-of-order page writes: the newline reached the disk, the start of the line didn't.
        let (_dir, path) = journal();
        let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
        let good = format!("{{\"t\":\"seen\",\"outbox_id\":\"{a}\"}}\n");
        let torn = format!("\0\0\0\0{{\"t\":\"seen\",\"outbox_id\":\"{b}\"}}\n");
        std::fs::write(&path, format!("{good}{torn}")).unwrap();
        let j = Journal::open(&path).unwrap();
        assert_eq!(j.state().seen, HashSet::from([a]));
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            good,
            "cut back to the good line"
        );
        // A NUL in an earlier line is not a torn write: that is corruption.
        std::fs::write(&path, format!("{torn}{good}")).unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::Corrupt { line: 1 })
        ));
    }

    #[test]
    fn corrupt_middle_line_fails_closed() {
        let (_dir, path) = journal();
        let a = Uuid::new_v4();
        let line = format!(r#"{{"t":"seen","outbox_id":"{a}"}}"#);
        std::fs::write(&path, format!("{line}\nnot json\n{line}\n")).unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::Corrupt { line: 2 })
        ));
        // A record of an unknown kind, or with an extra field, is corrupt too.
        std::fs::write(&path, format!("{line}\n{{\"t\":\"erase\"}}\n")).unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::Corrupt { line: 2 })
        ));
        std::fs::write(
            &path,
            format!("{{\"t\":\"seen\",\"outbox_id\":\"{a}\",\"x\":1}}\n"),
        )
        .unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::Corrupt { line: 1 })
        ));
        // A blank line in the middle is not a record.
        std::fs::write(&path, format!("{line}\n\n{line}\n")).unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::Corrupt { line: 2 })
        ));
    }

    #[test]
    fn stages_only_move_forward() {
        let (_dir, path) = journal();
        let key = Uuid::new_v4();
        let mut j = Journal::open(&path).unwrap();
        let started = Record::Started {
            execution_key: key,
            args: facts(),
            at: at(),
        };
        let done = Record::Done {
            execution_key: key,
            receipt: json!({"result": "RECYCLED"}),
        };
        let out_of_order = |r: Result<(), JournalError>| matches!(r, Err(JournalError::OutOfOrder { execution_key }) if execution_key == key);
        assert!(out_of_order(
            j.append(&Record::Staged { execution_key: key })
        ));
        assert!(out_of_order(j.append(&done)));
        j.append(&started).unwrap();
        assert!(out_of_order(j.append(&started)), "started twice");
        j.append(&Record::Staged { execution_key: key }).unwrap();
        assert!(out_of_order(
            j.append(&Record::Staged { execution_key: key })
        ));
        j.append(&done).unwrap();
        assert!(out_of_order(j.append(&done)), "done twice");
        assert!(out_of_order(j.append(&started)), "started after done");
        assert_eq!(
            j.state().executions[&key],
            Stage::Done(json!({"result": "RECYCLED"}))
        );
        // A refused write leaves nothing on disk.
        drop(j);
        assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 3);

        // Started → Done directly (refused before staging) is allowed.
        let other = Uuid::new_v4();
        let mut j = Journal::open(&path).unwrap();
        j.append(&Record::Started {
            execution_key: other,
            args: facts(),
            at: at(),
        })
        .unwrap();
        j.append(&Record::Done {
            execution_key: other,
            receipt: json!({"result": "MISMATCH"}),
        })
        .unwrap();
    }

    #[test]
    fn a_failed_write_is_cut_back_and_poisons_the_journal() {
        let (_dir, path) = journal();
        let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
        let mut j = Journal::open(&path).unwrap();
        j.append(&Record::Seen { outbox_id: a }).unwrap();
        let good_len = std::fs::metadata(&path).unwrap().len();
        j.fail_after = Some(10); // half a line reaches the disk, then the write fails
        assert!(matches!(
            j.append(&Record::Seen { outbox_id: b }),
            Err(JournalError::Io(_))
        ));
        assert!(
            !j.state().seen.contains(&b),
            "a failed append is never applied"
        );
        assert_eq!(
            std::fs::metadata(&path).unwrap().len(),
            good_len,
            "partial line cut off"
        );
        assert!(matches!(
            j.append(&Record::Seen { outbox_id: b }),
            Err(JournalError::Poisoned)
        ));
        drop(j);
        // Reopening is the way back: the journal is intact and writable again.
        let mut j = Journal::open(&path).unwrap();
        assert_eq!(j.state().seen, HashSet::from([a]));
        j.append(&Record::Seen { outbox_id: b }).unwrap();
    }

    #[test]
    fn replayed_stages_out_of_order_fail_closed() {
        let (_dir, path) = journal();
        let key = Uuid::new_v4();
        std::fs::write(
            &path,
            format!("{{\"t\":\"staged\",\"execution_key\":\"{key}\"}}\n"),
        )
        .unwrap();
        assert!(matches!(
            Journal::open(&path),
            Err(JournalError::OutOfOrder { .. })
        ));
    }

    #[test]
    fn seen_and_cleared_may_repeat() {
        let (_dir, path) = journal();
        let a = Uuid::new_v4();
        let mut j = Journal::open(&path).unwrap();
        j.append(&Record::Seen { outbox_id: a }).unwrap();
        j.append(&Record::Seen { outbox_id: a }).unwrap();
        j.append(&Record::Cleared { outbox_id: a }).unwrap();
        j.append(&Record::Cleared { outbox_id: a }).unwrap();
        assert!(j.state().cleared.contains(&a));
    }

    #[test]
    fn an_empty_or_new_journal_opens_empty() {
        let (_dir, path) = journal();
        let j = Journal::open(&path).unwrap();
        assert_eq!(j.state(), &JournalState::default());
        assert!(path.exists());
    }
}
