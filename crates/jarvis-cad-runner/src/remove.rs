//! Removing a tree the engine may have made hostile: any depth, mode-000 directories, links,
//! FIFOs, sockets, and (in process-group mode) a helper still renaming things during the walk.
//!
//! One routine for the replay's own directory and for container mode's wipe of the work root.
//! It replaces `std::fs::remove_dir_all`, which recurses once per level: a chain of about
//! 20,000 directories overflowed the runner's stack (independent re-check, R1).
//!
//! The walk (unix) is iterative and entirely by descriptor, with at most a few descriptors
//! open at once whatever the depth:
//! - going down: `openat(O_NOFOLLOW | O_DIRECTORY)` relative to the current directory, which is
//!   made `0700` first through its descriptor (`fchmod`); a directory that cannot be opened at
//!   all (mode 000) is unlocked through `O_PATH` and `/proc/self/fd/N` (this needs /proc, which
//!   docker always mounts);
//! - only the (device, inode) of every level is kept, in memory (16 bytes a level); back in a
//!   directory, the subdirectory just emptied is found again by its inode number and removed
//!   (renaming it to a known name instead would cost time growing with the depth: the kernel
//!   checks a directory rename against its ancestors);
//! - files, links, FIFOs and sockets are removed with `unlinkat` (a link is never followed);
//!   emptied directories with `unlinkat(AT_REMOVEDIR)`;
//! - going back up: `openat(current, "..")`, then `fstat` must give the (device, inode) of the
//!   directory the walk came from; on any mismatch the walk stops and nothing more is removed
//!   (so a directory moved out of the tree while the walk is inside it never leads the walk
//!   outside); a directory on another device is never entered;
//! - the walk has a time limit; what is not removed by then is left, and reported.

use std::path::Path;
use std::time::Duration;

/// The time one removal may take.
pub const REMOVE_LIMIT: Duration = Duration::from_secs(5);

/// Removes the file, link or directory tree at `path` (missing is fine), within
/// [`REMOVE_LIMIT`]. An error means something was left.
pub fn remove_tree(path: &Path) -> std::io::Result<()> {
    remove_tree_within(path, REMOVE_LIMIT)
}

/// [`remove_tree`] with another time limit.
pub fn remove_tree_within(path: &Path, limit: Duration) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        walk::remove(path, std::time::Instant::now() + limit)
    }
    #[cfg(not(unix))]
    {
        // The runner only runs on Linux; this keeps the crate building elsewhere.
        let _ = limit;
        match std::fs::symlink_metadata(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e),
            Ok(meta) if meta.is_dir() => std::fs::remove_dir_all(path),
            Ok(_) => std::fs::remove_file(path),
        }
    }
}

#[cfg(unix)]
mod walk {
    // `dev_t` and `ino_t` are u64 on Linux but not on every unix.
    #![allow(clippy::unnecessary_cast)]

    use std::ffi::{CStr, CString};
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    use std::time::Instant;

    type Id = (u64, u64);

    fn left(what: &str) -> io::Error {
        io::Error::other(format!("left behind: {what}"))
    }

    fn last() -> io::Error {
        io::Error::last_os_error()
    }

    fn openat(parent: RawFd, name: &CStr, flags: libc::c_int) -> io::Result<OwnedFd> {
        // SAFETY: a valid descriptor (or AT_FDCWD) and a NUL-terminated name.
        let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
        if fd < 0 {
            return Err(last());
        }
        // SAFETY: `fd` was just opened and is owned by nothing else.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    fn stat_fd(fd: &OwnedFd) -> io::Result<libc::stat> {
        // SAFETY: an open descriptor and a zeroed buffer of the right type.
        unsafe {
            let mut st: libc::stat = std::mem::zeroed();
            if libc::fstat(fd.as_raw_fd(), &mut st) != 0 {
                return Err(last());
            }
            Ok(st)
        }
    }

    /// `fstatat` without following a link.
    fn stat_at(dir: &OwnedFd, name: &CStr) -> io::Result<libc::stat> {
        // SAFETY: an open descriptor, a NUL-terminated name and a zeroed buffer.
        unsafe {
            let mut st: libc::stat = std::mem::zeroed();
            if libc::fstatat(
                dir.as_raw_fd(),
                name.as_ptr(),
                &mut st,
                libc::AT_SYMLINK_NOFOLLOW,
            ) != 0
            {
                return Err(last());
            }
            Ok(st)
        }
    }

    fn id(st: &libc::stat) -> Id {
        (st.st_dev as u64, st.st_ino as u64)
    }

    fn is_dir(st: &libc::stat) -> bool {
        st.st_mode & libc::S_IFMT == libc::S_IFDIR
    }

    fn unlink_at(dir: &OwnedFd, name: &CStr, flags: libc::c_int) -> io::Result<()> {
        // SAFETY: an open descriptor and a NUL-terminated name.
        if unsafe { libc::unlinkat(dir.as_raw_fd(), name.as_ptr(), flags) } != 0 {
            return Err(last());
        }
        Ok(())
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
    /// through `/proc/self/fd/N` (which names that same directory; this needs /proc, which
    /// docker always mounts), then open it relative to the held descriptor.
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
            return Err(last());
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

    /// Reads `dir` from its start, removing every entry that is not a directory, and the
    /// directory `done` (the one just emptied, found by its inode number), until it meets
    /// another directory: that directory's name, or `None` when `dir` is now empty.
    fn clear_until_subdir(dir: &OwnedFd, done: Option<Id>) -> io::Result<Option<CString>> {
        // `fdopendir` takes ownership of its descriptor: give it a duplicate, and rewind it
        // (a duplicate shares the read position).
        // SAFETY: plain system calls; `stream` is checked before use and closed once.
        unsafe {
            let dup = libc::fcntl(dir.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0);
            if dup < 0 {
                return Err(last());
            }
            let stream = libc::fdopendir(dup);
            if stream.is_null() {
                let e = last();
                libc::close(dup);
                return Err(e);
            }
            libc::rewinddir(stream);
            let mut found = Ok(None);
            loop {
                let entry = libc::readdir(stream);
                if entry.is_null() {
                    break;
                }
                let name = CStr::from_ptr((*entry).d_name.as_ptr());
                if name == c"." || name == c".." {
                    continue;
                }
                let kind = (*entry).d_type;
                if kind == libc::DT_DIR {
                    if done.is_some_and(|d| d.1 == (*entry).d_ino as u64)
                        && stat_at(dir, name).is_ok_and(|st| Some(id(&st)) == done)
                        && unlink_at(dir, name, libc::AT_REMOVEDIR).is_ok()
                    {
                        continue;
                    }
                    found = Ok(Some(name.to_owned()));
                    break;
                }
                // Not a directory by its entry (or unknown): unlink it, which never follows a
                // link; a directory refuses with EISDIR (or EPERM on some systems).
                match unlink_at(dir, name, 0) {
                    Ok(()) => {}
                    Err(e) if e.raw_os_error() == Some(libc::ENOENT) => {}
                    Err(e)
                        if matches!(e.raw_os_error(), Some(libc::EISDIR) | Some(libc::EPERM))
                            && stat_at(dir, name).is_ok_and(|st| is_dir(&st)) =>
                    {
                        if stat_at(dir, name).is_ok_and(|st| Some(id(&st)) == done)
                            && unlink_at(dir, name, libc::AT_REMOVEDIR).is_ok()
                        {
                            continue;
                        }
                        found = Ok(Some(name.to_owned()));
                        break;
                    }
                    Err(e) => {
                        found = Err(e);
                        break;
                    }
                }
            }
            libc::closedir(stream);
            found
        }
    }

    pub fn remove(path: &Path, deadline: Instant) -> io::Result<()> {
        let parent_path = match path.parent() {
            Some(p) if !p.as_os_str().is_empty() => p,
            _ => Path::new("."),
        };
        let name = path
            .file_name()
            .ok_or_else(|| left("a path without a name"))?;
        let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
        let parent_c =
            CString::new(parent_path.as_os_str().as_bytes()).map_err(io::Error::other)?;
        let parent = openat(
            libc::AT_FDCWD,
            &parent_c,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )?;
        let top = match stat_at(&parent, &name) {
            Ok(st) => st,
            Err(e) if e.raw_os_error() == Some(libc::ENOENT) => return Ok(()),
            Err(e) => return Err(e),
        };
        if !is_dir(&top) {
            return unlink_at(&parent, &name, 0);
        }
        let mut cur = open_dir(parent.as_raw_fd(), &name)?;
        let root = id(&stat_fd(&cur)?);
        if root != id(&top) {
            return Err(left("the directory changed while it was opened"));
        }
        let device = root.0;
        // The (device, inode) of every level from the top down to `cur`.
        let mut levels: Vec<Id> = vec![root];
        // The directory just emptied, to remove from `cur` on the next pass.
        let mut done: Option<Id> = None;
        loop {
            if Instant::now() > deadline {
                return Err(left("out of time"));
            }
            if let Some(sub) = clear_until_subdir(&cur, done.take())? {
                let seen = stat_at(&cur, &sub)?;
                if !is_dir(&seen) {
                    continue; // swapped for something else: cleared on the next pass
                }
                if seen.st_dev as u64 != device {
                    return Err(left("a directory on another device"));
                }
                let child = open_dir(cur.as_raw_fd(), &sub)?;
                let child_id = id(&stat_fd(&child)?);
                if child_id != id(&seen) || child_id.0 != device {
                    return Err(left("a directory changed while it was entered"));
                }
                levels.push(child_id);
                cur = child;
                continue;
            }
            // `cur` is empty.
            if levels.len() == 1 {
                drop(cur);
                // Remove the top by its name, if the name still is the top.
                return match stat_at(&parent, &name) {
                    Ok(st) if id(&st) == root => unlink_at(&parent, &name, libc::AT_REMOVEDIR),
                    Ok(_) => Err(left("the top directory was replaced")),
                    Err(e) if e.raw_os_error() == Some(libc::ENOENT) => {
                        Err(left("the top directory was moved"))
                    }
                    Err(e) => Err(e),
                };
            }
            let emptied = levels.pop().unwrap_or(root);
            let up = openat(
                cur.as_raw_fd(),
                c"..",
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )?;
            let expected = *levels.last().unwrap_or(&root);
            if id(&stat_fd(&up)?) != expected {
                // Moved out of the tree under the walk: stop here, remove nothing more.
                return Err(left("a directory was moved out of the tree"));
            }
            drop(cur);
            cur = up;
            done = Some(emptied);
        }
    }
}

#[cfg(unix)]
#[doc(hidden)]
pub mod testing {
    use std::ffi::CString;
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    /// Makes a chain `d/d/d/...` of `depth` directories inside `dir`, with a file in the
    /// deepest one; the directory at level `lock_at` (1 = the first `d`) is left mode 000.
    /// Returns a descriptor on the deepest directory.
    pub fn chain(dir: &Path, depth: usize, lock_at: Option<usize>) -> io::Result<OwnedFd> {
        let c = CString::new(dir.as_os_str().as_bytes()).map_err(io::Error::other)?;
        let open = |parent: i32, name: &std::ffi::CStr| -> io::Result<OwnedFd> {
            // SAFETY: a valid descriptor and a NUL-terminated name.
            let fd = unsafe {
                libc::openat(
                    parent,
                    name.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: just opened, owned by nothing else.
            Ok(unsafe { OwnedFd::from_raw_fd(fd) })
        };
        let mut cur = open(libc::AT_FDCWD, &c)?;
        let mut locked = None;
        for level in 1..=depth {
            // SAFETY: a valid descriptor and a NUL-terminated name.
            if unsafe { libc::mkdirat(cur.as_raw_fd(), c"d".as_ptr(), 0o755) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let next = open(cur.as_raw_fd(), c"d")?;
            if Some(level) == lock_at {
                locked = Some(next.try_clone()?);
            }
            cur = next;
        }
        // SAFETY: a valid descriptor and a NUL-terminated name.
        let fd = unsafe {
            libc::openat(
                cur.as_raw_fd(),
                c"bottom.txt".as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_CLOEXEC,
                0o644,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: just opened.
        drop(unsafe { OwnedFd::from_raw_fd(fd) });
        if let Some(locked) = locked {
            // SAFETY: a plain system call on an open descriptor.
            unsafe {
                libc::fchmod(locked.as_raw_fd(), 0o000);
            }
        }
        Ok(cur)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    fn mode(p: &Path) -> u32 {
        std::fs::metadata(p).unwrap().permissions().mode() & 0o777
    }

    fn outside_with_keep() -> tempfile::TempDir {
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("keep"), "keep").unwrap();
        std::fs::set_permissions(outside.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
        outside
    }

    #[test]
    fn fifos_sockets_and_links_go_and_link_targets_stay() {
        let outside = outside_with_keep();
        let top = tempfile::tempdir().unwrap();
        let root = top.path().join("root");
        std::fs::create_dir_all(root.join("sub")).unwrap();
        let fifo = std::ffi::CString::new(root.join("fifo").as_os_str().as_bytes()).unwrap();
        // SAFETY: a valid NUL-terminated path.
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        let _socket = std::os::unix::net::UnixListener::bind(root.join("sub/socket")).unwrap();
        symlink("/nonexistent/target", root.join("dangling")).unwrap();
        symlink(outside.path(), root.join("sub/out")).unwrap();
        symlink(outside.path().join("keep"), root.join("keep-link")).unwrap();
        std::fs::write(root.join("sub/file"), "x").unwrap();
        remove_tree(&root).unwrap();
        assert!(!root.exists());
        assert_eq!(
            std::fs::read_to_string(outside.path().join("keep")).unwrap(),
            "keep"
        );
        assert_eq!(mode(outside.path()), 0o755);
        remove_tree(&root).unwrap(); // already gone: fine
                                     // A top that is itself a link: the link goes, not its target.
        symlink(outside.path(), top.path().join("link")).unwrap();
        remove_tree(&top.path().join("link")).unwrap();
        assert!(outside.path().join("keep").exists());
    }

    #[test]
    fn twenty_thousand_files_in_one_directory() {
        let top = tempfile::tempdir().unwrap();
        let root = top.path().join("root");
        std::fs::create_dir(&root).unwrap();
        for i in 0..20_000 {
            std::fs::File::create(root.join(format!("f{i}"))).unwrap();
        }
        let began = std::time::Instant::now();
        remove_tree(&root).unwrap();
        assert!(!root.exists());
        assert!(
            began.elapsed() < Duration::from_secs(5),
            "{:?}",
            began.elapsed()
        );
    }

    /// A helper keeps swapping a directory of the tree with a link to a directory outside it
    /// while the tree is made and removed again and again: the outside directory and its file
    /// are never touched.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_swapped_in_link_is_never_followed() {
        let outside = outside_with_keep();
        let top = tempfile::tempdir().unwrap();
        let root = top.path().join("root");
        let stop = Arc::new(AtomicBool::new(false));
        let swapper = {
            let stop = stop.clone();
            let d = std::ffi::CString::new(root.join("d").as_os_str().as_bytes()).unwrap();
            let alt = std::ffi::CString::new(root.join("alt").as_os_str().as_bytes()).unwrap();
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
        let began = std::time::Instant::now();
        while began.elapsed() < Duration::from_secs(2) {
            let _ = std::fs::create_dir_all(root.join("d"));
            let _ = std::fs::write(root.join("d/file"), "x");
            let _ = symlink(outside.path(), root.join("alt"));
            let _ = remove_tree(&root);
            if mode(outside.path()) != 0o755 || !outside.path().join("keep").exists() {
                break;
            }
        }
        stop.store(true, Ordering::Relaxed);
        swapper.join().unwrap();
        assert_eq!(
            mode(outside.path()),
            0o755,
            "the walk followed a link out of the tree"
        );
        assert!(
            outside.path().join("keep").exists(),
            "the walk removed a file outside the tree"
        );
    }

    /// While the walk is deep inside the tree, a helper moves the directory it is under out of
    /// the tree: the walk stops at the move and never removes anything outside the tree.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_directory_moved_out_under_the_walk_never_leads_it_outside() {
        let mut moved = 0;
        for _ in 0..5 {
            // top/s/{root/a/<chain>, outside/keep}: even a broken walk climbing two levels too
            // far stays inside top.
            let top = tempfile::tempdir().unwrap();
            let s = top.path().join("s");
            let root = s.join("root");
            let a = root.join("a");
            let outside = s.join("outside");
            std::fs::create_dir_all(&a).unwrap();
            std::fs::create_dir_all(&outside).unwrap();
            std::fs::write(outside.join("keep"), "keep").unwrap();
            let bottom = testing::chain(&a, 3_000, None).unwrap();
            let a_dir = std::fs::File::open(&a).unwrap();
            let mover = {
                let outside = outside.clone();
                std::thread::spawn(move || {
                    use std::os::fd::AsRawFd;
                    let began = std::time::Instant::now();
                    // Wait until the walk has reached the bottom (it removes the file there).
                    loop {
                        // SAFETY: a valid descriptor, a NUL-terminated name, a zeroed buffer.
                        let gone = unsafe {
                            let mut st: libc::stat = std::mem::zeroed();
                            libc::fstatat(bottom.as_raw_fd(), c"bottom.txt".as_ptr(), &mut st, 0)
                                != 0
                        };
                        if gone || began.elapsed() > Duration::from_secs(5) {
                            break;
                        }
                    }
                    // `a` itself has been renamed by the walk by now: name it by descriptor.
                    let held = format!("/proc/self/fd/{}", a_dir.as_raw_fd());
                    let Some(Ok(entry)) = std::fs::read_dir(&held).ok().and_then(|mut d| d.next())
                    else {
                        return false;
                    };
                    let from = std::ffi::CString::new(entry.file_name().as_bytes()).unwrap();
                    let to = std::ffi::CString::new(outside.join("moved").as_os_str().as_bytes())
                        .unwrap();
                    // SAFETY: a valid descriptor and NUL-terminated names.
                    unsafe {
                        libc::renameat(
                            a_dir.as_raw_fd(),
                            from.as_ptr(),
                            libc::AT_FDCWD,
                            to.as_ptr(),
                        ) == 0
                    }
                })
            };
            let result = remove_tree(&root);
            let did_move = mover.join().unwrap();

            assert_eq!(
                std::fs::read_to_string(outside.join("keep")).unwrap(),
                "keep",
                "the walk removed a file outside the tree"
            );
            if did_move {
                moved += 1;
                assert!(result.is_err(), "a moved directory is reported as left");
            }
        }
        assert!(
            moved > 0,
            "the helper never managed to move the directory in time"
        );
    }
}
