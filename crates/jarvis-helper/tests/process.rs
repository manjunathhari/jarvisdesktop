use std::path::Path;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

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
    let t = Instant::now();
    assert_eq!(
        events.recv_timeout(Duration::from_secs(5)).unwrap(),
        LinkEvent::Closed
    );
    assert!(t.elapsed() < Duration::from_secs(5));
}

#[test]
fn a_helper_that_never_answers_can_still_be_shut_down() {
    let (running, events, _e) = start(&["--never-ready"]);
    assert!(events.recv_timeout(Duration::from_millis(300)).is_err());
    let t = Instant::now();
    running.shutdown();
    assert!(t.elapsed() < Duration::from_secs(3));
}
