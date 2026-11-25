//! An in-memory test folder (test double for `FolderOps`), with the tampering the tests need.
//! It models §9.4's outcomes, not Windows: the real checks are in `windows.rs` and its tests.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;

use chrono::{DateTime, FixedOffset};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::{valid_name, FolderOps, Located, Outcome};
use crate::journal::FileFacts;

/// Something done to an execution's staging between step 3 and step 4.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tamper {
    /// The staging subfolder became a junction.
    Junction,
    /// Its DACL changed.
    Dacl,
    /// The staged file was replaced by a copy (same content, another file ID).
    SwapFile,
    /// The staged file's content changed (same file ID).
    ChangeContent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Regular,
    Folder,
    Link,
}

#[derive(Debug, Clone)]
struct File {
    content: Vec<u8>,
    file_id: String,
    kind: Kind,
}

#[derive(Default)]
struct State {
    unconfigured: bool,
    folder_is_link: bool,
    folder: BTreeMap<String, File>,
    staging: HashMap<Uuid, (String, File)>,
    bin: Vec<File>,
    staging_link: HashSet<Uuid>,
    dacl_changed: HashSet<Uuid>,
    after_stage: HashMap<Uuid, Tamper>,
    recycle_calls: usize,
    next_id: u128,
}

pub struct MemoryFolder {
    state: Mutex<State>,
}

fn sha256(b: &[u8]) -> String {
    data_encoding::HEXLOWER.encode(&Sha256::digest(b))
}

fn facts(name: &str, f: &File) -> FileFacts {
    FileFacts {
        name: name.to_owned(),
        size: f.content.len() as u64,
        sha256: sha256(&f.content),
        file_id: f.file_id.clone(),
    }
}

impl Default for MemoryFolder {
    fn default() -> Self {
        Self::new()
    }
}

impl MemoryFolder {
    /// A configured, empty folder.
    pub fn new() -> Self {
        Self {
            state: Mutex::default(),
        }
    }

    /// No test folder set.
    pub fn unconfigured() -> Self {
        let f = Self::new();
        f.lock().unconfigured = true;
        f
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().expect("memory folder lock")
    }

    fn new_id(s: &mut State) -> String {
        s.next_id += 1;
        format!("{:016x}:{:032x}", 0xfeed_u64, s.next_id)
    }

    fn put_kind(&self, name: &str, content: &[u8], kind: Kind) -> FileFacts {
        let mut s = self.lock();
        let file = File {
            content: content.to_vec(),
            file_id: Self::new_id(&mut s),
            kind,
        };
        let f = facts(name, &file);
        s.folder.insert(name.to_owned(), file);
        f
    }

    /// A regular file; returns what a listing reports for it.
    pub fn put(&self, name: &str, content: &[u8]) -> FileFacts {
        self.put_kind(name, content, Kind::Regular)
    }
    pub fn put_dir(&self, name: &str) {
        self.put_kind(name, b"", Kind::Folder);
    }
    pub fn put_link(&self, name: &str) {
        self.put_kind(name, b"", Kind::Link);
    }
    pub fn set_folder_is_link(&self, link: bool) {
        self.lock().folder_is_link = link;
    }
    /// New content, same file ID.
    pub fn replace_content(&self, name: &str, content: &[u8]) {
        if let Some(f) = self.lock().folder.get_mut(name) {
            f.content = content.to_vec();
        }
    }
    /// The same content under a new file ID (a copy put in its place).
    pub fn swap_file(&self, name: &str) {
        let mut s = self.lock();
        let id = Self::new_id(&mut s);
        if let Some(f) = s.folder.get_mut(name) {
            f.file_id = id;
        }
    }
    pub fn remove(&self, name: &str) {
        self.lock().folder.remove(name);
    }
    pub fn in_folder(&self, name: &str) -> bool {
        self.lock().folder.contains_key(name)
    }
    pub fn staged(&self, key: Uuid) -> bool {
        self.lock().staging.contains_key(&key)
    }
    pub fn in_bin(&self, file_id: &str) -> bool {
        self.lock().bin.iter().any(|f| f.file_id == file_id)
    }
    pub fn recycle_calls(&self) -> usize {
        self.lock().recycle_calls
    }
    /// Step 3 done outside the executor (as if the process died right after it).
    pub fn move_to_staging(&self, name: &str, key: Uuid) {
        let mut s = self.lock();
        if let Some(f) = s.folder.remove(name) {
            s.staging.insert(key, (name.to_owned(), f));
        }
    }
    /// Step 4 done outside the executor (as if the process died right after the recycle).
    pub fn recycle_now(&self, key: Uuid) {
        let mut s = self.lock();
        if let Some((_, f)) = s.staging.remove(&key) {
            s.bin.push(f);
            s.recycle_calls += 1;
        }
    }
    /// Tamper with the staging once step 3 has put the file there.
    pub fn tamper_after_stage(&self, key: Uuid, t: Tamper) {
        self.lock().after_stage.insert(key, t);
    }
    /// Tamper with what is in staging now.
    pub fn tamper_staged_now(&self, key: Uuid, t: Tamper) {
        let mut s = self.lock();
        Self::apply(&mut s, key, t);
    }

    fn apply(s: &mut State, key: Uuid, t: Tamper) {
        match t {
            Tamper::Junction => {
                s.staging_link.insert(key);
            }
            Tamper::Dacl => {
                s.dacl_changed.insert(key);
            }
            Tamper::SwapFile => {
                let id = Self::new_id(s);
                if let Some((_, f)) = s.staging.get_mut(&key) {
                    f.file_id = id;
                }
            }
            Tamper::ChangeContent => {
                if let Some((_, f)) = s.staging.get_mut(&key) {
                    f.content.extend_from_slice(b" changed");
                }
            }
        }
    }

    fn folder_ok(s: &State) -> Result<(), Outcome> {
        if s.unconfigured {
            return Err(Outcome::Refused("no_test_folder"));
        }
        if s.folder_is_link {
            return Err(Outcome::Refused("folder_reparse_point"));
        }
        Ok(())
    }
}

impl FolderOps for MemoryFolder {
    fn check_folder(&self) -> Result<(), Outcome> {
        Self::folder_ok(&self.lock())
    }

    fn list(&self) -> Result<Vec<FileFacts>, Outcome> {
        let s = self.lock();
        Self::folder_ok(&s)?;
        Ok(s.folder
            .iter()
            .filter(|(name, f)| f.kind == Kind::Regular && valid_name(name))
            .map(|(name, f)| facts(name, f))
            .collect())
    }

    fn stage(&self, want: &FileFacts, key: Uuid) -> Result<(), Outcome> {
        let mut s = self.lock();
        Self::folder_ok(&s)?;
        let file = s.folder.get(&want.name).ok_or(Outcome::NotFound)?;
        if file.kind != Kind::Regular {
            return Err(Outcome::Refused("not_regular_file"));
        }
        let have = facts(&want.name, file);
        if have.file_id != want.file_id {
            return Err(Outcome::Mismatch("file_id"));
        }
        if have.size != want.size {
            return Err(Outcome::Mismatch("size"));
        }
        if have.sha256 != want.sha256 {
            return Err(Outcome::Mismatch("sha256"));
        }
        if s.staging.contains_key(&key) {
            return Err(Outcome::Refused("staging_exists"));
        }
        let file = s.folder.remove(&want.name).expect("checked above");
        s.staging.insert(key, (want.name.clone(), file));
        if let Some(t) = s.after_stage.remove(&key) {
            Self::apply(&mut s, key, t);
        }
        Ok(())
    }

    fn recycle_staged(
        &self,
        want: &FileFacts,
        key: Uuid,
    ) -> Result<DateTime<FixedOffset>, Outcome> {
        let mut s = self.lock();
        if s.staging_link.contains(&key) {
            return Err(Outcome::Refused("staging_reparse_point"));
        }
        if s.dacl_changed.contains(&key) {
            return Err(Outcome::Refused("staging_dacl"));
        }
        let (name, file) = s.staging.get(&key).ok_or(Outcome::NotFound)?;
        let have = facts(name, file);
        if have.name != want.name {
            return Err(Outcome::Refused("staged_path"));
        }
        if have.file_id != want.file_id {
            return Err(Outcome::Refused("file_id"));
        }
        if have.size != want.size || have.sha256 != want.sha256 {
            return Err(Outcome::Refused("sha256"));
        }
        let (_, file) = s.staging.remove(&key).expect("checked above");
        s.bin.push(file);
        s.recycle_calls += 1;
        Ok(chrono::Utc::now().fixed_offset())
    }

    fn locate(&self, want: &FileFacts, key: Uuid) -> Located {
        let s = self.lock();
        if s.staging
            .get(&key)
            .is_some_and(|(name, _)| *name == want.name)
        {
            return Located::Staged;
        }
        if s.folder
            .get(&want.name)
            .is_some_and(|f| f.file_id == want.file_id)
        {
            return Located::InFolder;
        }
        if s.bin.iter().any(|f| f.file_id == want.file_id) {
            return Located::InRecycleBin;
        }
        Located::Unknown
    }
}
