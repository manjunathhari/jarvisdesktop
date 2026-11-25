//! The node's durable state (`node-state.json` in the app's data folder): its node ID and its
//! pinned keys. Only public keys are here; the private key is in the key store.
//!
//! Saves are atomic: the new content goes to `node-state.json.tmp`, is `sync_all`'d, and is then
//! renamed over the old file, so a crash leaves either the old state or the new one, never a
//! mix. On Unix the folder is then synced so the rename itself is durable. On Windows the
//! rename is `MoveFileExW` (`MOVEFILE_REPLACE_EXISTING`); a same-volume rename is atomic through
//! the NTFS log, but `MOVEFILE_WRITE_THROUGH` alone doesn't promise that log entry is on disk
//! when the call returns, so the new file is then opened and flushed (`FlushFileBuffers`), which
//! commits the NTFS log, rename included.
//!
//! One process owns these files (the app runs as a single instance); nothing here locks them.
//!
//! A state file that can't be read is an error, never "no state": the node then refuses to
//! connect rather than silently re-enrolling.

use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::trust::Trust;

/// What the node remembers between runs.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NodeState {
    /// Set once enrolment has succeeded (§7.2 step 5).
    pub node_id: Option<Uuid>,
    /// The fingerprint of the key this node enrolled with. The driver connects only when the
    /// stored key has this fingerprint, so a crash between saving the key and saving the state
    /// can never pair a new key with an old node ID.
    pub node_key_id: Option<String>,
    pub trust: Trust,
}

const VERSION: u32 = 1;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StateFile {
    v: u32,
    /// Required (null when not enrolled): a missing key is a corrupt file, not "not enrolled".
    #[serde(deserialize_with = "Option::deserialize")]
    node_id: Option<Uuid>,
    #[serde(deserialize_with = "Option::deserialize")]
    node_key_id: Option<String>,
    trust: Trust,
}

#[derive(Debug, thiserror::Error)]
pub enum StateError {
    #[error("node state I/O: {0}")]
    Io(#[from] io::Error),
    #[error("the node state file is unreadable: {0}")]
    Corrupt(String),
}

/// The saved state, `None` if there is none yet.
pub fn load(path: &Path) -> Result<Option<NodeState>, StateError> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let file: StateFile =
        serde_json::from_slice(&bytes).map_err(|e| StateError::Corrupt(e.to_string()))?;
    if file.v != VERSION {
        return Err(StateError::Corrupt(format!("unknown version {}", file.v)));
    }
    Ok(Some(NodeState {
        node_id: file.node_id,
        node_key_id: file.node_key_id,
        trust: file.trust,
    }))
}

/// Save atomically and durably.
pub fn save(path: &Path, state: &NodeState) -> Result<(), StateError> {
    let file = StateFile {
        v: VERSION,
        node_id: state.node_id,
        node_key_id: state.node_key_id.clone(),
        trust: state.trust.clone(),
    };
    let bytes = serde_json::to_vec_pretty(&file).expect("node state always serialises");
    write_atomic(path, &bytes)?;
    Ok(())
}

/// `<path>.tmp` in the same folder (so the rename stays on one volume).
fn tmp_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_owned();
    name.push(".tmp");
    path.with_file_name(name)
}

/// Replace `path` with `bytes` atomically: write a temporary file, `sync_all`, rename over,
/// then make the rename durable. Shared with the key store.
pub(crate) fn write_atomic(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let tmp = tmp_path(path);
    {
        let mut f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    if let Err(e) = rename_durable(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    sync_parent_dir(path)
}

#[cfg(windows)]
fn rename_durable(from: &Path, to: &Path) -> io::Result<()> {
    use windows::core::HSTRING;
    use windows::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    // SAFETY: both arguments are valid, NUL-terminated wide strings for the call's duration.
    unsafe {
        MoveFileExW(
            &HSTRING::from(from),
            &HSTRING::from(to),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    }
    .map_err(io::Error::other)?;
    // FlushFileBuffers on the renamed file (it needs a writable handle) commits the NTFS log.
    OpenOptions::new().write(true).open(to)?.sync_all()
}

#[cfg(not(windows))]
fn rename_durable(from: &Path, to: &Path) -> io::Result<()> {
    std::fs::rename(from, to)
}

/// Make a new or renamed entry in `path`'s folder durable. On Unix, fsync the folder. On
/// Windows this is a no-op: a folder can't be flushed through std, and flushing the file
/// itself (`sync_all`, which callers do) commits the NTFS log that holds the entry.
pub(crate) fn sync_parent_dir(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        let parent = match path.parent() {
            Some(p) if !p.as_os_str().is_empty() => p,
            _ => Path::new("."),
        };
        std::fs::File::open(parent)?.sync_all()?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trust::PinnedKey;

    fn state() -> NodeState {
        let mut trust = Trust::default();
        trust.core.insert("core-1".into(), PinnedKey::new([7; 32]));
        trust.gateway.insert("gw-1".into(), PinnedKey::new([8; 32]));
        NodeState {
            node_id: Some(Uuid::new_v4()),
            node_key_id: Some("abcdefghijklmnop".into()),
            trust,
        }
    }

    #[test]
    fn save_is_atomic_and_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("node-state.json");
        let first = state();
        save(&path, &first).unwrap();
        assert_eq!(load(&path).unwrap(), Some(first));
        let second = state();
        save(&path, &second).unwrap();
        assert_eq!(load(&path).unwrap(), Some(second));
        let names: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(
            names,
            vec![std::ffi::OsString::from("node-state.json")],
            "no .tmp left"
        );
    }

    #[test]
    fn a_stale_tmp_file_from_a_crash_does_not_matter() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("node-state.json");
        let saved = state();
        save(&path, &saved).unwrap();
        // A crash during the next save left a half-written temporary file.
        std::fs::write(tmp_path(&path), b"{\"v\":1,\"node_").unwrap();
        assert_eq!(
            load(&path).unwrap(),
            Some(saved.clone()),
            "the old state is intact"
        );
        let next = state();
        save(&path, &next).unwrap();
        assert_eq!(load(&path).unwrap(), Some(next));
        assert!(!tmp_path(&path).exists());
    }

    #[test]
    fn missing_state_is_none() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load(&dir.path().join("node-state.json")).unwrap(), None);
    }

    #[test]
    fn unreadable_state_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("node-state.json");
        for bad in [
            &b"not json"[..],
            br#"{"v":2,"node_id":null,"node_key_id":null,"trust":{"core":{},"gateway":{}}}"#,
            br#"{"v":1,"node_id":null,"node_key_id":null,"trust":{"core":{},"gateway":{}},"extra":1}"#,
            br#"{"v":1,"node_id":null,"node_key_id":null}"#,
            br#"{"v":1,"node_key_id":null,"trust":{"core":{},"gateway":{}}}"#,
            br#"{"v":1,"node_id":null,"trust":{"core":{},"gateway":{}}}"#,
            b"",
        ] {
            std::fs::write(&path, bad).unwrap();
            assert!(
                matches!(load(&path), Err(StateError::Corrupt(_))),
                "{}",
                String::from_utf8_lossy(bad)
            );
        }
    }
}
