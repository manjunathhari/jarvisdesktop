//! The removal routine on trees no path can name: very deep chains and deeply buried mode-000
//! directories (independent re-check 2, R1 and R2). Each removal runs in a child process
//! (`fake-cadcraft remove-tree`, on a 256 KiB stack), so a removal that recursed per level
//! crashes that child and fails the test instead of aborting this harness.
#![cfg(unix)]

mod common;

use std::path::Path;
use std::time::{Duration, Instant};

use common::*;
use jarvis_cad_runner::remove::testing::chain;

/// Removes `path` in a child process (removal limit 30 s: these tests run on whatever disk
/// the temp dir is on, often much slower than the container's tmpfs); `true` when it is gone.
fn remove_in_child(path: &Path) -> bool {
    let mut child = std::process::Command::new(fake_engine())
        .arg("remove-tree")
        .arg(path)
        .arg("30")
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let began = Instant::now();
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status.success();
        }
        if began.elapsed() > Duration::from_secs(60) {
            let _ = child.kill();
            let _ = child.wait();
            return false;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn deep_case(depth: usize, lock_at: Option<usize>) {
    let scratch = Scratch::new();
    let root = scratch.file("root");
    std::fs::create_dir(&root).unwrap();
    let made = Instant::now();
    drop(chain(&root, depth, lock_at).unwrap());
    let made = made.elapsed();
    let began = Instant::now();
    assert!(
        remove_in_child(&root),
        "depth {depth}, locked at {lock_at:?}: not removed"
    );
    println!(
        "depth {depth}, locked at {lock_at:?}: made in {made:?}, removed in {:?}",
        began.elapsed()
    );
    assert!(std::fs::symlink_metadata(&root).is_err());
}

#[test]
fn a_chain_50000_deep_is_removed() {
    deep_case(50_000, None);
}

#[test]
fn a_locked_directory_at_depth_100_is_removed() {
    deep_case(120, Some(100));
}

#[test]
fn a_locked_directory_at_depth_5000_is_removed() {
    deep_case(5_100, Some(5_000));
}

/// Root only (needs a mount namespace): a directory of the tree that is another file
/// system's mount point is not entered, and nothing on it is removed.
#[cfg(target_os = "linux")]
#[test]
fn a_mount_inside_the_tree_is_not_crossed() {
    // SAFETY: a plain system call.
    if unsafe { libc::geteuid() } != 0 || !Path::new("/usr/bin/unshare").exists() {
        println!("skipped: needs root and unshare");
        return;
    }
    let scratch = Scratch::new();
    let root = scratch.file("root");
    std::fs::create_dir_all(root.join("m")).unwrap();
    let script = format!(
        "mount -t tmpfs none '{m}' && echo keep > '{m}/keep' && mkdir '{m}/sub' && \
         {fake} remove-tree '{root}'; test -f '{m}/keep' && test -d '{m}/sub' && echo intact",
        m = root.join("m").display(),
        root = root.display(),
        fake = fake_engine().display(),
    );
    let out = std::process::Command::new("unshare")
        .args(["--mount", "--propagation", "private", "sh", "-c", &script])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    if !out.status.success() && !text.contains("left behind") && !text.contains("intact") {
        println!("skipped: could not make a mount namespace here");
        return;
    }
    assert!(text.contains("intact"), "{text}");
    assert!(text.contains("another device"), "{text}");
}
