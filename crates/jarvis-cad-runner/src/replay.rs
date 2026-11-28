//! One replay (or catalogue) in a fresh engine, from nothing, inside the time limit.
//!
//! Ported from the slice 0 test (`spikes/cad-engine/draw_test.py`, `Engine.replay`,
//! `Engine.catalogue`, `summarise`, `find_extents`). The engine's replies are untrusted data:
//! they are parsed, checked and cut to the protocol's sizes here.
//!
//! The order of one replay: a new directory under the work root; the engine started in it, in
//! its own process group; every tool call, `save` last; the engine's group killed and the
//! engine reaped (and, in container mode, every other process killed and reaped); only then the
//! saved SVG read, through the directory opened at the start, without following links and
//! without blocking, inside the same deadline; then the directory removed.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use data_encoding::BASE64;
use serde::de::{DeserializeSeed, Deserializer, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use serde_json::json;
use serde_json::value::RawValue;
use tokio::time::Instant;

use crate::engine::{Engine, EngineError, Lenient, MAX_TOOL_TEXT};
use crate::protocol::{
    Answer, CatalogueAnswer, Command, CommandResult, ErrorCode, ReplayAnswer, Request, Step,
    VERSION,
};
pub use crate::scan::{find_extents, summarise, Summary};

/// Request line limit, in bytes, not counting its `\n`.
pub const MAX_REQUEST_BYTES: usize = 1_048_576;
/// Commands per recipe.
pub const MAX_COMMANDS: usize = 2_000;
/// The SVG limit, in bytes.
pub const MAX_SVG_BYTES: u64 = 2_000_000;
/// The longest line read from the engine.
pub const MAX_ENGINE_LINE: usize = 4 * 1024 * 1024;
/// The longest PNG passed on, as base64 (an 800x600 line drawing is far smaller).
pub const MAX_PNG_B64: usize = 2 * 1024 * 1024;
/// A command result's text: one line of at most this many characters.
pub const RESULT_TEXT_CHARS: usize = 300;
/// The drawing's texts: at most this many, each at most this many characters.
pub const MAX_TEXTS: usize = 80;
pub const TEXT_CHARS: usize = 80;
/// The most items a drawing may have (Task 1's limit); more is `too_large`.
pub const MAX_ITEMS: u64 = 100_000;

const ENGINE_NAME: &str = "cadcraft";
const SVG_NAME: &str = "drawing.svg";
const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";

/// How the runner runs: the engine, the allowed commands, the limits and where it works.
#[derive(Debug, Clone)]
pub struct Config {
    engine: PathBuf,
    allowed: BTreeSet<String>,
    replay_limit: Duration,
    busy_wait: Duration,
    read_limit: Duration,
    work_root: PathBuf,
    container: bool,
    /// Directories that could not be removed; retried before each replay.
    leftovers: Arc<Mutex<Vec<PathBuf>>>,
}

impl Config {
    /// The engine binary and the allowed command ids, with the plan's limits: 10 s per replay,
    /// 5 s waiting for the slot, 5 s to read the request line; replays work under the system
    /// temp dir.
    pub fn new(engine: PathBuf, allowed: BTreeSet<String>) -> Config {
        Config {
            engine,
            allowed,
            replay_limit: Duration::from_secs(10),
            busy_wait: Duration::from_secs(5),
            read_limit: Duration::from_secs(5),
            work_root: std::env::temp_dir(),
            container: false,
            leftovers: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// The time one replay may take, engine start to answer.
    pub fn replay_limit(mut self, limit: Duration) -> Config {
        self.replay_limit = limit;
        self
    }

    /// How long a request waits for the one replay slot before `busy`.
    pub fn busy_wait(mut self, wait: Duration) -> Config {
        self.busy_wait = wait;
        self
    }

    /// How long a client has to send its request line.
    pub fn read_limit(mut self, limit: Duration) -> Config {
        self.read_limit = limit;
        self
    }

    /// The directory each replay's own directory is made in.
    pub fn work_root(mut self, dir: PathBuf) -> Config {
        self.work_root = dir;
        self
    }

    /// Container mode: after every replay, SIGKILL to every other process, all zombies
    /// reaped, and everything in the work root (`/tmp`) removed. Refused unless this process
    /// is pid 1 (`container_mode_allowed`); see there for what that does and does not prove.
    pub fn container_mode(mut self) -> Result<Config, String> {
        let pid = own_pid();
        if !crate::container_mode_allowed(true, pid) {
            return Err(format!(
                "--container is only for the cad container, where the runner is pid 1; this runner is pid {pid}"
            ));
        }
        self.container = true;
        self.work_root = PathBuf::from("/tmp");
        Ok(self)
    }

    pub fn replay_limit_value(&self) -> Duration {
        self.replay_limit
    }

    pub fn busy_wait_value(&self) -> Duration {
        self.busy_wait
    }

    pub fn read_limit_value(&self) -> Duration {
        self.read_limit
    }

    pub fn work_root_value(&self) -> &Path {
        &self.work_root
    }

    pub fn is_container(&self) -> bool {
        self.container
    }

    pub fn allowed(&self) -> &BTreeSet<String> {
        &self.allowed
    }
}

/// This process's pid (0 where there is none).
fn own_pid() -> u32 {
    #[cfg(unix)]
    {
        // SAFETY: a plain system call without arguments.
        let pid = unsafe { libc::getpid() };
        pid as u32
    }
    #[cfg(not(unix))]
    {
        0
    }
}

/// Reads the commands file: one command id per line; blank lines are skipped.
pub fn load_commands(path: &Path) -> std::io::Result<BTreeSet<String>> {
    let text = std::fs::read_to_string(path)?;
    parse_commands(&text).map_err(|bad| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("not a command id: {bad:?}"),
        )
    })
}

/// The ids in a commands file's text, or the first line that is not an id.
pub fn parse_commands(text: &str) -> Result<BTreeSet<String>, String> {
    let mut ids = BTreeSet::new();
    for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let good = line.len() <= 40
            && line
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '_');
        if !good {
            return Err(line.chars().take(40).collect());
        }
        ids.insert(line.to_string());
    }
    Ok(ids)
}

/// The checks made before any engine starts: the command count, then every command id.
pub fn check(cfg: &Config, req: &Request) -> Result<(), ErrorCode> {
    if req.command_count() > MAX_COMMANDS {
        return Err(ErrorCode::TooManyCommands);
    }
    if let Request::Replay { steps, .. } = req {
        let all_allowed = steps
            .iter()
            .flat_map(|s| &s.commands)
            .all(|c| cfg.allowed.contains(&c.command));
        if !all_allowed {
            return Err(ErrorCode::NotAllowed);
        }
    }
    Ok(())
}

/// A replay's own directory, and a handle on it opened before the engine starts (so a
/// directory the engine renames or replaces later is not the one read).
struct WorkDir {
    path: PathBuf,
    #[cfg(unix)]
    dir: std::fs::File,
}

impl WorkDir {
    fn create(root: &Path) -> std::io::Result<WorkDir> {
        let mut builder = tempfile::Builder::new();
        builder.prefix("jarvis-cad-");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            builder.permissions(std::fs::Permissions::from_mode(0o700));
        }
        let path = builder.tempdir_in(root)?.keep();
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            let dir = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&path);
            match dir {
                Ok(dir) => Ok(WorkDir { path, dir }),
                Err(e) => {
                    let _ = remove_tree(&path);
                    Err(e)
                }
            }
        }
        #[cfg(not(unix))]
        {
            Ok(WorkDir { path })
        }
    }
}

/// Everything that must happen after a replay, on every way out of it: the engine's group
/// killed, (container mode) every other process killed and reaped, the directory removed.
/// `finish` does it on the normal paths; `Drop` does what it can if a panic unwinds first.
struct Cleanup<'a> {
    cfg: &'a Config,
    engine: Option<Engine>,
    work: Option<WorkDir>,
}

impl Cleanup<'_> {
    /// Stops the engine (and, in container mode, every other process).
    async fn stop_processes(&mut self) {
        if let Some(engine) = self.engine.take() {
            engine.kill().await;
        }
        if self.cfg.container {
            let _ = tokio::task::spawn_blocking(container_sweep).await;
        }
    }

    /// Removes the replay's directory (and, in container mode, everything in the work root).
    async fn remove_files(&mut self) {
        let Some(work) = self.work.take() else {
            return;
        };
        let cfg = self.cfg.clone();
        let _ = tokio::task::spawn_blocking(move || remove_work(&cfg, work)).await;
    }
}

impl Drop for Cleanup<'_> {
    fn drop(&mut self) {
        // Only reached with something left when a panic unwound through the replay.
        if self.engine.is_none() && self.work.is_none() {
            return;
        }
        if let Some(engine) = self.engine.take() {
            drop(engine); // kills the group; tokio reaps the child
        }
        if self.cfg.container {
            container_sweep();
        }
        if let Some(work) = self.work.take() {
            remove_work(self.cfg, work);
        }
    }
}

/// Removes a replay's directory: once, then once more; a directory that is still there is
/// logged and kept in `leftovers`, and every following request fails until it is gone. In
/// container mode, everything else in the work root goes too.
fn remove_work(cfg: &Config, work: WorkDir) {
    let path = work.path.clone();
    drop(work);
    let mut failed = Vec::new();
    if let Err(e) = remove_tree(&path).or_else(|_| remove_tree(&path)) {
        eprintln!(
            "jarvis-cad-runner: could not remove {}: {e}",
            path.display()
        );
        failed.push(path);
    }
    if cfg.container {
        failed.extend(empty_dir(&cfg.work_root));
    }
    if !failed.is_empty() {
        if let Ok(mut leftovers) = cfg.leftovers.lock() {
            leftovers.extend(failed);
        }
    }
}

/// Retries the removal of every leftover; `true` when none is left.
pub fn settle_leftovers(
    leftovers: &mut Vec<PathBuf>,
    remove: impl Fn(&Path) -> std::io::Result<()>,
) -> bool {
    leftovers.retain(|path| match remove(path) {
        Ok(()) => false,
        Err(e) => {
            eprintln!(
                "jarvis-cad-runner: still cannot remove {}: {e}",
                path.display()
            );
            true
        }
    });
    leftovers.is_empty()
}

/// Makes a tree removable (directories `0700`, walked without following links), then removes
/// it. A link is removed, never followed. Missing is fine.
pub fn remove_tree(path: &Path) -> std::io::Result<()> {
    let meta = match std::fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if !meta.file_type().is_dir() {
        return std::fs::remove_file(path);
    }
    make_removable(path);
    match std::fs::remove_dir_all(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

/// `chmod 0700` on every directory of the tree (unix), so that it can be removed.
fn make_removable(root: &Path) {
    #[cfg(unix)]
    chmod_walk::make_removable(root);
    #[cfg(not(unix))]
    let _ = root;
}

/// The walk behind [`make_removable`]: by descriptor, never by path. Each directory is opened
/// relative to its parent's descriptor with `O_NOFOLLOW | O_DIRECTORY`, so a name swapped for a
/// symlink is refused rather than followed, and the mode is set on the open descriptor
/// (`fchmod`). Depth is capped at [`chmod_walk::MAX_DEPTH`]; a deeper tree is left for the
/// removal to fail on, which is logged and retried.
#[cfg(unix)]
mod chmod_walk {
    use std::ffi::{CStr, CString};
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    pub const MAX_DEPTH: usize = 64;

    pub fn make_removable(root: &Path) {
        let Ok(root) = CString::new(root.as_os_str().as_bytes()) else {
            return;
        };
        if let Ok(fd) = open_dir(libc::AT_FDCWD, &root) {
            walk(&fd, 0);
        }
    }

    /// Recursion bounded by `MAX_DEPTH` (so at most that many descriptors are open at once).
    fn walk(dir: &OwnedFd, depth: usize) {
        if depth >= MAX_DEPTH {
            return;
        }
        for name in subdirs(dir) {
            if let Ok(child) = open_dir(dir.as_raw_fd(), &name) {
                walk(&child, depth + 1);
            }
        }
    }

    fn openat(parent: RawFd, name: &CStr, flags: libc::c_int) -> io::Result<OwnedFd> {
        // SAFETY: a valid descriptor (or AT_FDCWD) and a NUL-terminated name.
        let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: `fd` was just opened and is owned by nothing else.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    /// Opens a directory (never a link) for reading and makes it `0700` through the descriptor.
    fn open_dir(parent: RawFd, name: &CStr) -> io::Result<OwnedFd> {
        let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
        let fd = match openat(parent, name, flags) {
            Ok(fd) => fd,
            Err(e) if e.raw_os_error() == Some(libc::EACCES) => unlock_unreadable(parent, name)?,
            Err(e) => return Err(e),
        };
        // SAFETY: a plain system call on an open descriptor.
        unsafe {
            libc::fchmod(fd.as_raw_fd(), 0o700);
        }
        Ok(fd)
    }

    /// A directory without read permission (mode 000) cannot be opened for reading. Hold it by
    /// `O_PATH | O_NOFOLLOW` (which pins the directory itself, not its name), set its mode
    /// through `/proc/self/fd/N` (which names that same directory), then open it relative to
    /// the held descriptor.
    #[cfg(target_os = "linux")]
    fn unlock_unreadable(parent: RawFd, name: &CStr) -> io::Result<OwnedFd> {
        let held = openat(
            parent,
            name,
            libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )?;
        let by_fd = CString::new(format!("/proc/self/fd/{}", held.as_raw_fd()))
            .map_err(io::Error::other)?;
        // SAFETY: a NUL-terminated path.
        if unsafe { libc::chmod(by_fd.as_ptr(), 0o700) } != 0 {
            return Err(io::Error::last_os_error());
        }
        openat(
            held.as_raw_fd(),
            c".",
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    }

    #[cfg(not(target_os = "linux"))]
    fn unlock_unreadable(_parent: RawFd, _name: &CStr) -> io::Result<OwnedFd> {
        Err(io::Error::from_raw_os_error(libc::EACCES))
    }

    /// The names of the real directories in `dir` (not links to them).
    fn subdirs(dir: &OwnedFd) -> Vec<CString> {
        // `fdopendir` takes ownership of its descriptor: give it a duplicate.
        // SAFETY: plain system calls; `stream` is checked before use and closed once.
        unsafe {
            let dup = libc::fcntl(dir.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0);
            if dup < 0 {
                return Vec::new();
            }
            let stream = libc::fdopendir(dup);
            if stream.is_null() {
                libc::close(dup);
                return Vec::new();
            }
            let mut names = Vec::new();
            loop {
                let entry = libc::readdir(stream);
                if entry.is_null() {
                    break;
                }
                let name = CStr::from_ptr((*entry).d_name.as_ptr());
                if name == c"." || name == c".." {
                    continue;
                }
                let is_dir = match (*entry).d_type {
                    libc::DT_DIR => true,
                    libc::DT_UNKNOWN => {
                        let mut st: libc::stat = std::mem::zeroed();
                        libc::fstatat(
                            dir.as_raw_fd(),
                            name.as_ptr(),
                            &mut st,
                            libc::AT_SYMLINK_NOFOLLOW,
                        ) == 0
                            && st.st_mode & libc::S_IFMT == libc::S_IFDIR
                    }
                    _ => false,
                };
                if is_dir {
                    names.push(name.to_owned());
                }
            }
            libc::closedir(stream);
            names
        }
    }
}

/// Removes everything inside `dir` (container mode: the whole of /tmp). Returns what could not
/// be removed, after logging it.
fn empty_dir(dir: &Path) -> Vec<PathBuf> {
    let mut failed = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        eprintln!("jarvis-cad-runner: cannot list {}", dir.display());
        return vec![dir.to_path_buf()];
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if let Err(e) = remove_tree(&path).or_else(|_| remove_tree(&path)) {
            eprintln!(
                "jarvis-cad-runner: could not remove {}: {e}",
                path.display()
            );
            failed.push(path);
        }
    }
    failed
}

/// Container mode only: SIGKILL to every process this one may signal (all of them in the
/// container but this one), then every zombie reaped. The pid is checked again here, so that
/// it can only ever run as pid 1.
fn container_sweep() {
    #[cfg(unix)]
    {
        if !crate::container_mode_allowed(true, own_pid()) {
            return;
        }
        // SAFETY: plain system calls. kill(-1) never signals the caller itself.
        unsafe {
            libc::kill(-1, libc::SIGKILL);
        }
        let until = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            // SAFETY: a plain system call; a null status pointer is allowed.
            let reaped = unsafe { libc::waitpid(-1, std::ptr::null_mut(), libc::WNOHANG) };
            if reaped > 0 {
                continue;
            }
            if reaped < 0 {
                let interrupted =
                    std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR);
                if interrupted {
                    continue;
                }
                break; // ECHILD: no children left
            }
            if std::time::Instant::now() > until {
                eprintln!("jarvis-cad-runner: children still alive 2 s after SIGKILL");
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

/// The SIGTERM handler's cleanup: the engine's group killed and, in container mode, every
/// other process too and the work root emptied.
pub fn shutdown(cfg: &Config) {
    crate::engine::kill_current_group();
    if cfg.container {
        container_sweep();
        let _ = empty_dir(&cfg.work_root);
    }
}

/// Runs one request in a fresh engine and answers it. Does not take the replay slot: the
/// listener does that (`crate::serve`).
pub async fn replay(cfg: &Config, req: Request) -> Answer {
    if let Err(code) = check(cfg, &req) {
        return Answer::failure(code);
    }
    let deadline = Instant::now() + cfg.replay_limit;
    if let Ok(mut leftovers) = cfg.leftovers.lock() {
        if !settle_leftovers(&mut leftovers, remove_tree) {
            return Answer::failure(ErrorCode::EngineFailed);
        }
    }
    let work = match WorkDir::create(&cfg.work_root) {
        Ok(work) => work,
        Err(e) => {
            eprintln!(
                "jarvis-cad-runner: no working directory under {}: {e}",
                cfg.work_root.display()
            );
            return Answer::failure(ErrorCode::EngineFailed);
        }
    };
    let mut cleanup = Cleanup {
        cfg,
        engine: None,
        work: Some(work),
    };
    let ran = {
        let Cleanup { engine, work, .. } = &mut cleanup;
        let work = work.as_ref().map(|w| w.path.clone()).unwrap_or_default();
        tokio::time::timeout_at(deadline, run(cfg, &req, &work, engine)).await
    };
    // The engine is dead and reaped before its files are touched. This order matters even
    // though the read itself is safe against links, FIFOs and devices (openat with O_NOFOLLOW
    // and O_NONBLOCK, fstat on the descriptor): a live engine, or a helper in its group, could
    // still change the file while it is read, or race the removal of the directory. No test
    // pins this order: without privileges, nothing the engine can do to the file before it is
    // killed gives a different answer from what it can do while the runner reads it, so a
    // "read first" mutation still passes. Keep it.
    cleanup.stop_processes().await;
    let answer = match ran {
        Err(_) => Answer::failure(ErrorCode::Timeout),
        Ok(Err(code)) => Answer::failure(code),
        Ok(Ok(Ran::Done(answer))) => answer,
        Ok(Ok(Ran::WantsSvg(mut answer))) => {
            let read = cleanup.work.as_ref().map(saved_svg_reader);
            match read {
                Some(Ok(read)) => {
                    match tokio::time::timeout_at(deadline, tokio::task::spawn_blocking(read)).await
                    {
                        Err(_) => Answer::failure(ErrorCode::Timeout),
                        Ok(Err(_)) => Answer::failure(ErrorCode::EngineFailed),
                        Ok(Ok(Err(code))) => Answer::failure(code),
                        Ok(Ok(Ok(svg))) => {
                            answer.svg_b64 = Some(BASE64.encode(&svg));
                            Answer::Replay(answer)
                        }
                    }
                }
                _ => Answer::failure(ErrorCode::EngineFailed),
            }
        }
    };
    cleanup.remove_files().await;
    answer
}

/// What the engine part of a replay produced.
enum Ran {
    Done(Answer),
    /// The replay answer, still without its SVG, which `save` wrote to the directory.
    WantsSvg(Box<ReplayAnswer>),
}

async fn run(
    cfg: &Config,
    req: &Request,
    workdir: &Path,
    slot: &mut Option<Engine>,
) -> Result<Ran, ErrorCode> {
    let engine = slot.insert(Engine::spawn(&cfg.engine, workdir, MAX_ENGINE_LINE).map_err(failed)?);
    engine.initialise().await.map_err(failed)?;
    match req {
        Request::Catalogue { .. } => catalogue(cfg, engine).await.map(Ran::Done),
        Request::Replay { steps, picture, .. } => {
            replay_steps(engine, steps, *picture, workdir).await
        }
    }
}

fn failed(e: EngineError) -> ErrorCode {
    let _ = e;
    ErrorCode::EngineFailed
}

/// One entry of the engine's `list_commands`: other fields, and values of other shapes, are
/// skipped.
#[derive(Deserialize)]
struct Listed {
    #[serde(default)]
    id: Lenient,
    #[serde(default)]
    params: Lenient,
}

/// Reads the `list_commands` list one entry at a time, keeping only the allowed ids (so a long
/// list costs no memory beyond the answer).
struct ListedSeed<'a>(&'a BTreeSet<String>);

impl<'de> DeserializeSeed<'de> for ListedSeed<'_> {
    type Value = BTreeMap<String, String>;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_seq(self)
    }
}

impl<'de> Visitor<'de> for ListedSeed<'_> {
    type Value = BTreeMap<String, String>;
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("a list of commands")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let mut commands = BTreeMap::new();
        while let Some(item) = seq.next_element::<Listed>()? {
            let Some(id) = item.id.0 else { continue };
            if self.0.contains(&id) {
                let note = item.params.0.unwrap_or_default();
                commands.insert(id, one_line(&note, 2_000));
            }
        }
        Ok(commands)
    }
}

async fn catalogue(cfg: &Config, engine: &mut Engine) -> Result<Answer, ErrorCode> {
    let reply = engine
        .call("list_commands", json!({}))
        .await
        .map_err(failed)?;
    if reply.text.len() >= MAX_TOOL_TEXT {
        return Err(ErrorCode::EngineFailed);
    }
    let mut de = serde_json::Deserializer::from_str(&reply.text);
    let commands = ListedSeed(&cfg.allowed)
        .deserialize(&mut de)
        .and_then(|c| de.end().map(|()| c))
        .map_err(|_| ErrorCode::EngineFailed)?;
    Ok(Answer::Catalogue(CatalogueAnswer {
        v: VERSION,
        ok: true,
        engine_version: engine.version().to_string(),
        commands,
    }))
}

/// The `execute` tool's arguments, with the client's parameters passed on as raw text.
#[derive(Serialize)]
struct Execute<'a> {
    command: &'a str,
    params: &'a RawValue,
}

async fn replay_steps(
    engine: &mut Engine,
    steps: &[Step],
    picture: bool,
    workdir: &Path,
) -> Result<Ran, ErrorCode> {
    engine
        .call("new_drawing", json!({"metric": true}))
        .await
        .map_err(failed)?;
    let mut results = Vec::new();
    let mut failed_count = 0;
    for (step_no, step) in steps.iter().enumerate() {
        for (cmd_no, Command { command, params }) in step.commands.iter().enumerate() {
            let reply = engine
                .call("execute", Execute { command, params })
                .await
                .map_err(failed)?;
            let ok = !reply.is_error;
            failed_count += usize::from(!ok);
            results.push(CommandResult {
                step: step_no + 1,
                n: cmd_no + 1,
                command: command.clone(),
                ok,
                text: one_line(&reply.text, RESULT_TEXT_CHARS),
            });
        }
    }

    let inspected = engine
        .call("inspect_drawing", json!({}))
        .await
        .map_err(failed)?;
    let summary = summarise(&inspected.text);
    drop(inspected);
    if summary.items > MAX_ITEMS {
        return Err(ErrorCode::TooLarge);
    }
    let found = engine
        .call("find_text", json!({"limit": MAX_TEXTS}))
        .await
        .map_err(failed)?;
    let texts = texts_of(&found.text);
    drop(found);
    let rendered = engine
        .call("render", json!({"width": 800, "height": 600}))
        .await
        .map_err(failed)?;
    let png_b64 = rendered.image.as_deref().and_then(checked_png);
    drop(rendered);

    let answer = Box::new(ReplayAnswer {
        v: VERSION,
        ok: true,
        engine: ENGINE_NAME.into(),
        engine_version: engine.version().to_string(),
        results,
        failed: failed_count,
        counts: summary.counts,
        items: summary.items,
        extents: summary.extents,
        texts,
        png_b64,
        svg_b64: None,
    });
    if !(picture && failed_count == 0) {
        return Ok(Ran::Done(Answer::Replay(answer)));
    }
    // `save` is the last call: the file is read only once the engine is dead.
    let path = workdir.join(SVG_NAME);
    let saved = engine
        .call("save", json!({"path": path.to_string_lossy()}))
        .await
        .map_err(failed)?;
    if saved.is_error {
        return Err(ErrorCode::EngineFailed);
    }
    Ok(Ran::WantsSvg(answer))
}

/// The checks on the saved SVG's open descriptor: a regular file of 1 to 2,000,000 bytes.
/// Larger is `too_large`; anything else (a FIFO, a directory, a device, empty) `engine_failed`.
pub fn svg_file_check(regular: bool, len: u64) -> Result<(), ErrorCode> {
    if !regular {
        return Err(ErrorCode::EngineFailed);
    }
    if len > MAX_SVG_BYTES {
        return Err(ErrorCode::TooLarge);
    }
    if len == 0 {
        return Err(ErrorCode::EngineFailed);
    }
    Ok(())
}

/// [`svg_file_check`] on an open descriptor's `fstat` (unix: its only caller is unix-only).
#[cfg(unix)]
fn check_svg_meta(meta: &std::fs::Metadata) -> Result<(), ErrorCode> {
    svg_file_check(meta.file_type().is_file(), meta.len())
}

type SvgRead = Box<dyn FnOnce() -> Result<Vec<u8>, ErrorCode> + Send>;

/// A blocking read of the saved SVG, to run off the async threads: opened relative to the
/// directory handle with `O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, checked on the open descriptor,
/// read to at most the limit.
fn saved_svg_reader(work: &WorkDir) -> std::io::Result<SvgRead> {
    #[cfg(unix)]
    {
        let dir = work.dir.try_clone()?;
        Ok(Box::new(move || read_svg_at(&dir)))
    }
    #[cfg(not(unix))]
    {
        let _ = work;
        Ok(Box::new(|| Err(ErrorCode::EngineFailed)))
    }
}

#[cfg(unix)]
fn read_svg_at(dir: &std::fs::File) -> Result<Vec<u8>, ErrorCode> {
    use std::io::Read;
    use std::os::fd::{AsRawFd, FromRawFd};
    let name = std::ffi::CString::new(SVG_NAME).map_err(|_| ErrorCode::EngineFailed)?;
    let flags =
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC | libc::O_NOCTTY;
    // SAFETY: a valid directory descriptor and a NUL-terminated name.
    let fd = unsafe { libc::openat(dir.as_raw_fd(), name.as_ptr(), flags) };
    if fd < 0 {
        return Err(ErrorCode::EngineFailed);
    }
    // SAFETY: `fd` was just opened and is owned by nothing else.
    let file = unsafe { std::fs::File::from_raw_fd(fd) };
    let meta = file.metadata().map_err(|_| ErrorCode::EngineFailed)?;
    check_svg_meta(&meta)?;
    let mut svg = Vec::with_capacity(meta.len() as usize);
    file.take(MAX_SVG_BYTES + 1)
        .read_to_end(&mut svg)
        .map_err(|_| ErrorCode::EngineFailed)?;
    svg_file_check(true, svg.len() as u64)?;
    Ok(svg)
}

/// The PNG, decoded and encoded again, if it is base64 of a PNG within the limit.
fn checked_png(data: &str) -> Option<String> {
    if data.len() > MAX_PNG_B64 {
        return None;
    }
    let png = BASE64.decode(data.as_bytes()).ok()?;
    png.starts_with(PNG_SIGNATURE).then(|| BASE64.encode(&png))
}

/// One line: whitespace runs become one space, control characters go, at most `max` characters.
pub fn one_line(text: &str, max: usize) -> String {
    let mut out = String::new();
    let mut chars = 0;
    'words: for word in text.split_whitespace() {
        let mut word = word.chars().filter(|c| !c.is_control()).peekable();
        if word.peek().is_none() {
            continue;
        }
        if chars >= max {
            break;
        }
        if chars > 0 {
            out.push(' ');
            chars += 1;
        }
        for c in word {
            if chars == max {
                break 'words;
            }
            out.push(c);
            chars += 1;
        }
    }
    out.truncate(out.trim_end().len());
    out
}

/// The drawing's texts from a `find_text` reply (at most 80, each one line of at most 80
/// characters).
pub fn texts_of(find_text: &str) -> Vec<String> {
    crate::scan::texts_of(find_text, MAX_TEXTS, TEXT_CHARS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_line_cuts_and_cleans() {
        assert_eq!(one_line("  a\n b\t\tc  ", 300), "a b c");
        assert_eq!(one_line("abcdef", 3), "abc");
        assert_eq!(one_line("ab cd", 3), "ab");
        assert_eq!(one_line("\u{1b}[31mred", 10), "[31mred");
        assert_eq!(one_line("ééé", 2), "éé");
        assert_eq!(one_line("", 5), "");
    }

    #[test]
    fn commands_file() {
        let ids = parse_commands("line\n\ntext\nlayer.new\n").unwrap();
        assert_eq!(ids.len(), 3);
        assert!(parse_commands("line\nrm -rf\n").is_err());
        let file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../server/config/cad_commands.txt");
        let ids = load_commands(&file).unwrap();
        assert_eq!(ids.len(), 37);
        assert!(ids.contains("dimlinear") && ids.contains("arraypolar"));
        assert!(!ids.contains("save") && !ids.contains("open"));
    }

    #[test]
    fn the_svg_must_be_a_regular_file_of_1_to_2000000_bytes() {
        assert_eq!(svg_file_check(true, 1), Ok(()));
        assert_eq!(svg_file_check(true, MAX_SVG_BYTES), Ok(()));
        assert_eq!(
            svg_file_check(true, MAX_SVG_BYTES + 1),
            Err(ErrorCode::TooLarge)
        );
        assert_eq!(svg_file_check(true, 0), Err(ErrorCode::EngineFailed));
        // A FIFO, a directory or a device: not regular, whatever its size.
        assert_eq!(svg_file_check(false, 100), Err(ErrorCode::EngineFailed));
        assert_eq!(
            svg_file_check(false, MAX_SVG_BYTES + 1),
            Err(ErrorCode::EngineFailed)
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_fifo_or_a_directory_is_refused_on_its_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let fifo = dir.path().join("fifo");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: a valid NUL-terminated path.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        let sub = dir.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        std::fs::write(sub.join("f"), "x").unwrap();
        let file = dir.path().join("file");
        std::fs::write(&file, "<svg/>").unwrap();
        let meta = |p: &Path| std::fs::symlink_metadata(p).unwrap();
        assert_eq!(check_svg_meta(&meta(&fifo)), Err(ErrorCode::EngineFailed));
        assert_eq!(check_svg_meta(&meta(&sub)), Err(ErrorCode::EngineFailed));
        assert_eq!(check_svg_meta(&meta(&file)), Ok(()));
    }

    #[test]
    fn a_leftover_fails_until_it_can_be_removed() {
        use std::cell::Cell;
        let mut leftovers = vec![PathBuf::from("/x/a"), PathBuf::from("/x/b")];
        let attempts = Cell::new(0);
        let stuck = |_: &Path| {
            attempts.set(attempts.get() + 1);
            Err(std::io::Error::other("busy"))
        };
        assert!(!settle_leftovers(&mut leftovers, stuck));
        assert_eq!((leftovers.len(), attempts.get()), (2, 2));
        let only_a = |p: &Path| {
            if p.ends_with("a") {
                Ok(())
            } else {
                Err(std::io::Error::other("busy"))
            }
        };
        assert!(!settle_leftovers(&mut leftovers, only_a));
        assert_eq!(leftovers, vec![PathBuf::from("/x/b")]);
        assert!(settle_leftovers(&mut leftovers, |_| Ok(())));
        assert!(leftovers.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn remove_tree_unlocks_directories_and_does_not_follow_links() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("keep"), "keep").unwrap();
        std::fs::set_permissions(outside.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = tempfile::tempdir().unwrap().keep();
        let locked = root.join("a/b");
        std::fs::create_dir_all(&locked).unwrap();
        std::fs::write(locked.join("f"), "x").unwrap();
        symlink(outside.path(), locked.join("out")).unwrap();
        symlink(outside.path(), root.join("out")).unwrap();
        for dir in [locked.clone(), root.join("a")] {
            std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o000)).unwrap();
        }
        remove_tree(&root).unwrap();
        assert!(!root.exists());
        assert_eq!(
            std::fs::read_to_string(outside.path().join("keep")).unwrap(),
            "keep"
        );
        // The outside directory's mode is untouched (a link is not followed for chmod).
        let mode = std::fs::metadata(outside.path())
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o755);
        remove_tree(&root).unwrap(); // already gone: fine
    }

    /// A helper keeps swapping a directory of the tree with a link to a directory outside it
    /// while the tree is unlocked again and again: the outside directory's mode never changes.
    #[cfg(target_os = "linux")]
    #[test]
    fn unlocking_never_follows_a_swapped_in_link() {
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::{symlink, PermissionsExt};
        use std::sync::atomic::{AtomicBool, Ordering};
        let outside = tempfile::tempdir().unwrap();
        std::fs::set_permissions(outside.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("d")).unwrap();
        symlink(outside.path(), root.path().join("alt")).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let swapper = {
            let stop = stop.clone();
            let d = std::ffi::CString::new(root.path().join("d").as_os_str().as_bytes()).unwrap();
            let alt =
                std::ffi::CString::new(root.path().join("alt").as_os_str().as_bytes()).unwrap();
            std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    // SAFETY: valid NUL-terminated paths.
                    unsafe {
                        libc::syscall(
                            libc::SYS_renameat2,
                            libc::AT_FDCWD,
                            d.as_ptr(),
                            libc::AT_FDCWD,
                            alt.as_ptr(),
                            libc::RENAME_EXCHANGE,
                        );
                    }
                }
            })
        };
        let mode = || {
            std::fs::metadata(outside.path())
                .unwrap()
                .permissions()
                .mode()
                & 0o777
        };
        let began = std::time::Instant::now();
        while began.elapsed() < Duration::from_secs(2) {
            make_removable(root.path());
            if mode() != 0o755 {
                break;
            }
        }
        stop.store(true, Ordering::Relaxed);
        swapper.join().unwrap();
        assert_eq!(mode(), 0o755, "the walk followed a link out of the tree");
    }

    #[test]
    fn png_is_checked() {
        let good = BASE64.encode(b"\x89PNG\r\n\x1a\nrest");
        assert_eq!(checked_png(&good), Some(good.clone()));
        assert_eq!(checked_png(&BASE64.encode(b"GIF89a")), None);
        assert_eq!(checked_png("not base64!"), None);
        let mut big = PNG_SIGNATURE.to_vec();
        big.resize(MAX_PNG_B64 / 4 * 3 + 3, 0);
        assert_eq!(
            checked_png(&BASE64.encode(&big)),
            None,
            "over 2 MiB of base64"
        );
        big.resize(MAX_PNG_B64 / 4 * 3, 0);
        assert!(
            checked_png(&BASE64.encode(&big)).is_some(),
            "exactly 2 MiB of base64"
        );
    }
}
