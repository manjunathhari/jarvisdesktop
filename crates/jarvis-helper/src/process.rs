//! Starting the helper program and owning the child.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::link::{HelperLink, HelperVoice, LinkEvent};
use crate::protocol::{HelperSettings, ToHelper, PROTOCOL_VERSION};

const EXIT_GRACE: Duration = Duration::from_secs(2);

/// A running helper. Dropping it without `shutdown` closes the helper's stdin and makes sure
/// the child is reaped (killed after a grace period if it does not exit).
pub struct Running {
    pub voice: Arc<HelperVoice>,
    link: Arc<HelperLink>,
    child: Option<Child>,
}

/// Start `exe` with `args`, piped stdin/stdout/stderr, and send `hello` at once.
/// `Ready` arrives later through `on_event`.
pub fn spawn(
    exe: &Path,
    args: &[&str],
    settings: &HelperSettings,
    on_event: Box<dyn Fn(LinkEvent) + Send>,
    on_stderr_line: Box<dyn Fn(String) + Send>,
) -> Result<Running, String> {
    let mut child = command(exe, args)
        .spawn()
        .map_err(|e| format!("couldn't start the voice helper: {e}"))?;
    let (Some(stdin), Some(stdout), Some(stderr)) =
        (child.stdin.take(), child.stdout.take(), child.stderr.take())
    else {
        let _ = child.kill();
        let _ = child.wait();
        return Err("couldn't open the voice helper's pipes".into());
    };

    thread::Builder::new()
        .name("jarvis-helper-stderr".into())
        .spawn(move || {
            let mut reader = BufReader::new(stderr);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buf);
                        on_stderr_line(line.trim_end_matches(['\r', '\n']).to_string());
                    }
                }
            }
        })
        .map_err(|e| format!("couldn't start the stderr reader: {e}"))?;

    let link = HelperLink::start(stdout, stdin, on_event);
    link.send(&ToHelper::Hello {
        protocol: PROTOCOL_VERSION,
        settings: settings.clone(),
    });
    let voice = Arc::new(HelperVoice::new(link.clone()));
    Ok(Running {
        voice,
        link,
        child: Some(child),
    })
}

/// The helper's command line: piped stdin/stdout/stderr, no console window on Windows.
fn command(exe: &Path, args: &[&str]) -> Command {
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // Kokoro logs each sentence's phonemes at DEBUG via LOG_LEVEL; stderr reaches our log file.
        .env_remove("LOG_LEVEL");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd
}

impl Running {
    /// Ask the helper to exit, wait up to 2 s, then kill it and reap it.
    pub fn shutdown(mut self) {
        self.link.send(&ToHelper::Shutdown);
        self.link.close_writer();
        // If the helper never reads, killing it (in `end_child`) breaks the blocked write.
        if let Some(child) = self.child.take() {
            end_child(child);
        }
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        self.link.close_writer();
        if let Some(child) = self.child.take() {
            // Never blocks: closing the queue only drops a sender, and the wait, kill and reap
            // (which also frees a writer stuck on a helper that stopped reading) run in the background.
            let _ = thread::Builder::new()
                .name("jarvis-helper-reaper".into())
                .spawn(move || end_child(child));
        }
    }
}

fn end_child(mut child: Child) {
    let deadline = Instant::now() + EXIT_GRACE;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => thread::sleep(Duration::from_millis(20)),
            Err(_) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_helper_never_inherits_log_level() {
        let cmd = command(Path::new("voice-helper"), &[]);
        let removed = cmd
            .get_envs()
            .any(|(key, value)| key == "LOG_LEVEL" && value.is_none());
        assert!(
            removed,
            "LOG_LEVEL must be removed from the helper's environment"
        );
    }
}
