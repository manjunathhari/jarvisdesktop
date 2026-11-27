//! The log file. The installed Windows app is a GUI program, so its stderr goes nowhere;
//! this also writes `info` and above to a rolling file in the app's local data folder.
//!
//! Nothing said is logged at `info` or above: no utterance text, no reply text.

use std::path::{Path, PathBuf};
use tracing_appender::non_blocking::WorkerGuard;
use tracing_appender::rolling::{RollingFileAppender, Rotation};
use tracing_subscriber::layer::SubscriberExt;

/// How many daily files are kept.
const MAX_LOG_FILES: usize = 7;

/// The folder `<per-user local data>/<identifier>/logs`: the same place Tauri's
/// `app_local_data_dir()` resolves to, plus `logs`. Needed because logging starts before any
/// Tauri app handle exists. `None` if the variable it depends on is unset or empty.
pub fn dir_for(identifier: &str) -> Option<PathBuf> {
    let base = local_data_base()?;
    Some(base.join(identifier).join("logs"))
}

#[cfg(windows)]
fn local_data_base() -> Option<PathBuf> {
    non_empty_var("LOCALAPPDATA").map(PathBuf::from)
}

#[cfg(not(windows))]
fn local_data_base() -> Option<PathBuf> {
    if let Some(xdg) = non_empty_var("XDG_DATA_HOME") {
        return Some(PathBuf::from(xdg));
    }
    non_empty_var("HOME").map(|home| PathBuf::from(home).join(".local").join("share"))
}

fn non_empty_var(name: &str) -> Option<std::ffi::OsString> {
    std::env::var_os(name).filter(|v| !v.is_empty())
}

/// stderr as before (`RUST_LOG` if set, else `info`) plus, when `dir` is given and usable,
/// a daily-rolling file `jarvis.<date>.log` there, without colour codes. If the folder can't
/// be created or the appender can't be built, stderr only, and no guard. Never panics.
pub fn subscriber(
    dir: Option<&Path>,
) -> (
    impl tracing::Subscriber + Send + Sync + 'static,
    Option<WorkerGuard>,
) {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));

    let (writer, guard) = match dir.and_then(open_appender) {
        Some(appender) => {
            let (writer, guard) = tracing_appender::non_blocking(appender);
            (Some(writer), Some(guard))
        }
        None => (None, None),
    };
    let file_layer = writer.map(|w| {
        tracing_subscriber::fmt::layer()
            .with_ansi(false)
            .with_writer(w)
    });

    let subscriber = tracing_subscriber::registry()
        .with(filter)
        .with(tracing_subscriber::fmt::layer())
        .with(file_layer);
    (subscriber, guard)
}

fn open_appender(dir: &Path) -> Option<RollingFileAppender> {
    std::fs::create_dir_all(dir).ok()?;
    RollingFileAppender::builder()
        .rotation(Rotation::DAILY)
        .filename_prefix("jarvis")
        .filename_suffix("log")
        .max_log_files(MAX_LOG_FILES)
        .build(dir)
        .ok()
}

/// Install [`subscriber`] as the global default. Keep the guard alive until the program
/// ends: dropping it flushes the file.
pub fn init(dir: Option<&Path>) -> Option<WorkerGuard> {
    let (subscriber, guard) = subscriber(dir);
    // A second install (a test, or a host that already set one) is not an error here.
    let _ = tracing::subscriber::set_global_default(subscriber);
    guard
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_a_line_into_the_logs_folder() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("logs");
        let (sub, guard) = subscriber(Some(&dir));
        assert!(guard.is_some());
        tracing::subscriber::with_default(sub, || {
            tracing::info!("marker-7f3a91c2");
        });
        drop(guard);

        let files: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        assert_eq!(files.len(), 1, "{files:?}");
        let name = files[0].file_name().unwrap().to_string_lossy().into_owned();
        assert!(name.starts_with("jarvis."), "{name}");
        let text = std::fs::read(&files[0]).unwrap();
        let text = String::from_utf8_lossy(&text);
        assert!(text.contains("marker-7f3a91c2"), "{text}");
        assert!(!text.contains('\u{1b}'), "colour codes in the file");
    }

    #[test]
    fn an_unusable_folder_falls_back_to_stderr_only() {
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("not-a-folder");
        std::fs::write(&file, b"x").unwrap();
        let (_sub, guard) = subscriber(Some(&file));
        assert!(guard.is_none());
    }

    #[test]
    fn dir_for_uses_the_identifier() {
        // The only test in this crate that touches this variable.
        #[cfg(windows)]
        {
            std::env::set_var("LOCALAPPDATA", r"C:\Users\someone\AppData\Local");
            assert_eq!(
                dir_for("in.example.app"),
                Some(
                    PathBuf::from(r"C:\Users\someone\AppData\Local")
                        .join("in.example.app")
                        .join("logs")
                )
            );
            std::env::set_var("LOCALAPPDATA", "");
            assert_eq!(dir_for("in.example.app"), None);
        }
        #[cfg(not(windows))]
        {
            std::env::set_var("XDG_DATA_HOME", "/tmp/xdg-test");
            assert_eq!(
                dir_for("in.example.app"),
                Some(PathBuf::from("/tmp/xdg-test/in.example.app/logs"))
            );
            std::env::set_var("XDG_DATA_HOME", "");
            std::env::set_var("HOME", "/home/someone");
            assert_eq!(
                dir_for("in.example.app"),
                Some(PathBuf::from(
                    "/home/someone/.local/share/in.example.app/logs"
                ))
            );
        }
    }
}
