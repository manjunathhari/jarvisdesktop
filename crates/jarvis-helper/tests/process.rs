use std::path::Path;
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use jarvis_core::Speaks;
use jarvis_helper::{spawn, HelperSettings, LinkEvent, Running};

const WAIT: Duration = Duration::from_secs(10);
const FAKE: &str = env!("CARGO_BIN_EXE_fake-helper");

fn settings() -> HelperSettings {
    HelperSettings {
        voice: "george".into(),
        tts_speed: 1.0,
        model_path: "m".into(),
        voices_path: "v".into(),
        output_device: None,
    }
}

fn start(args: &[&str]) -> (Running, Receiver<LinkEvent>, Receiver<String>) {
    let (tx, events) = mpsc::channel();
    let (etx, stderr) = mpsc::channel();
    let running = spawn(
        Path::new(FAKE),
        args,
        &settings(),
        Box::new(move |e| drop(tx.send(e))),
        Box::new(move |l| drop(etx.send(l))),
    )
    .expect("spawn fake helper");
    (running, events, stderr)
}

fn expect(events: &Receiver<LinkEvent>, want: LinkEvent) {
    assert_eq!(events.recv_timeout(WAIT).expect("event in time"), want);
}

#[test]
fn spawn_reaches_ready() {
    let (running, events, _e) = start(&[]);
    expect(
        &events,
        LinkEvent::Ready {
            helper_version: "fake".into(),
        },
    );
    running.voice.speak("hi");
    expect(&events, LinkEvent::Speaking(true));
    expect(&events, LinkEvent::Speaking(false));
    running.shutdown();
}

#[test]
fn a_helper_that_dies_reports_closed_and_is_not_speaking() {
    let (running, events, _e) = start(&["--die-while-speaking"]);
    expect(
        &events,
        LinkEvent::Ready {
            helper_version: "fake".into(),
        },
    );
    running.voice.speak("hi");
    expect(&events, LinkEvent::Speaking(true));
    expect(&events, LinkEvent::Closed);
    assert!(!running.voice.is_speaking());
    running.voice.speak("again"); // silent no-op
    running.shutdown();
}

#[test]
fn shutdown_ends_the_process() {
    let (running, events, _e) = start(&[]);
    expect(
        &events,
        LinkEvent::Ready {
            helper_version: "fake".into(),
        },
    );
    running.shutdown();
    expect(&events, LinkEvent::Closed);
}

#[test]
fn a_missing_exe_is_an_error_not_a_panic() {
    let r = spawn(
        Path::new("/definitely/not/here/helper"),
        &[],
        &settings(),
        Box::new(|_| {}),
        Box::new(|_| {}),
    );
    assert!(r.is_err());
}

#[test]
fn stderr_lines_reach_the_callback() {
    let (running, _events, stderr) = start(&[]);
    assert_eq!(stderr.recv_timeout(WAIT).unwrap(), "fake-helper started");
    running.shutdown();
}

#[test]
fn dropping_running_ends_the_child() {
    let (running, events, _e) = start(&[]);
    expect(
        &events,
        LinkEvent::Ready {
            helper_version: "fake".into(),
        },
    );
    drop(running);
    expect(&events, LinkEvent::Closed);
}

#[test]
fn a_helper_that_never_answers_can_still_be_shut_down() {
    let (running, events, _e) = start(&["--never-ready"]);
    assert!(events.recv_timeout(Duration::from_millis(300)).is_err());
    run_bounded(move || running.shutdown());
    expect(&events, LinkEvent::Closed);
}

/// Run `f` on its own thread and fail (not hang) if it does not finish in time.
fn run_bounded(f: impl FnOnce() + Send + 'static) {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        f();
        let _ = tx.send(());
    });
    rx.recv_timeout(WAIT).expect("finished in time");
}

/// Start a helper that never reads stdin and flood it with long sentences.
fn flooded() -> (Running, Receiver<LinkEvent>) {
    let (running, events, _e) = start(&["--never-read"]);
    let voice = running.voice.clone();
    run_bounded(move || {
        let text = "x".repeat(2000);
        for _ in 0..2000 {
            voice.speak(&text);
        }
    });
    (running, events)
}

#[test]
fn a_helper_that_never_reads_cannot_block_the_caller() {
    let (running, _events) = flooded();
    drop(running);
}

#[test]
fn shutdown_returns_even_when_the_helper_never_reads() {
    let (running, events) = flooded();
    run_bounded(move || running.shutdown());
    expect(&events, LinkEvent::Closed);
}

#[test]
fn drop_returns_at_once_even_when_the_helper_never_reads() {
    let (running, events) = flooded();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        drop(running);
        let _ = tx.send(());
    });
    rx.recv_timeout(Duration::from_secs(1))
        .expect("drop returned within 1 s");
    expect(&events, LinkEvent::Closed);
}
