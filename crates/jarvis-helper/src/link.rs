//! The pipe link: a reader thread turns the helper's lines into events, writes go through a mutex.

use std::io::{BufRead, BufReader, Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;

use jarvis_core::Speaks;

use crate::protocol::{FromHelper, ToHelper};

#[derive(Debug, Clone, PartialEq)]
pub enum LinkEvent {
    Ready {
        helper_version: String,
    },
    Speaking(bool),
    Error {
        code: String,
        message: String,
    },
    /// The helper's stdout ended. Emitted exactly once, last.
    Closed,
}

/// When this many messages are waiting unsent (the helper has stopped reading), new `say`
/// messages are dropped. `stop` and `shutdown` are always queued.
const MAX_QUEUED_SAY: usize = 256;

/// The link to the helper. Callers never touch the pipe: `send` only queues a message for the
/// writer thread, which is the only thing that can block on a wedged helper.
pub struct HelperLink {
    queue: Mutex<Option<mpsc::Sender<ToHelper>>>,
    pending: Arc<AtomicUsize>,
    warned: AtomicBool,
    speaking: AtomicBool,
    closed: AtomicBool,
}

impl HelperLink {
    pub fn start(
        reader: impl Read + Send + 'static,
        writer: impl Write + Send + 'static,
        on_event: Box<dyn Fn(LinkEvent) + Send>,
    ) -> Arc<HelperLink> {
        let (tx, rx) = mpsc::channel::<ToHelper>();
        let pending = Arc::new(AtomicUsize::new(0));
        let link = Arc::new(HelperLink {
            queue: Mutex::new(Some(tx)),
            pending: pending.clone(),
            warned: AtomicBool::new(false),
            speaking: AtomicBool::new(false),
            closed: AtomicBool::new(false),
        });
        thread::Builder::new()
            .name("jarvis-helper-writer".into())
            .spawn(move || write_loop(rx, writer, &pending))
            .expect("spawn helper writer thread");
        let for_thread = link.clone();
        thread::Builder::new()
            .name("jarvis-helper-reader".into())
            .spawn(move || for_thread.read_loop(reader, on_event))
            .expect("spawn helper reader thread");
        link
    }

    fn read_loop(&self, reader: impl Read, on_event: Box<dyn Fn(LinkEvent) + Send>) {
        let mut reader = BufReader::new(reader);
        let mut buf = Vec::new();
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            let Ok(line) = std::str::from_utf8(&buf) else {
                continue;
            };
            let Ok(msg) = serde_json::from_str::<FromHelper>(line.trim()) else {
                continue;
            };
            match msg {
                FromHelper::Ready { helper_version } => {
                    on_event(LinkEvent::Ready { helper_version })
                }
                FromHelper::Speaking { on } => {
                    self.speaking.store(on, Ordering::SeqCst);
                    on_event(LinkEvent::Speaking(on));
                }
                FromHelper::Error { code, message } => on_event(LinkEvent::Error { code, message }),
            }
        }
        self.closed.store(true, Ordering::SeqCst);
        self.speaking.store(false, Ordering::SeqCst);
        on_event(LinkEvent::Closed);
    }

    /// Queue one message for the writer thread; never blocks on the pipe. After `Closed`
    /// nothing is queued. A failed write (the helper has gone) ends the writer quietly;
    /// `Closed` comes from the reader.
    pub fn send(&self, msg: &ToHelper) {
        if self.is_closed() {
            return;
        }
        if matches!(msg, ToHelper::Say { .. })
            && self.pending.load(Ordering::SeqCst) > MAX_QUEUED_SAY
        {
            if !self.warned.swap(true, Ordering::SeqCst) {
                tracing::warn!("voice helper is not reading; dropping speech until it catches up");
            }
            return;
        }
        let guard = self.queue.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(tx) = guard.as_ref() {
            self.pending.fetch_add(1, Ordering::SeqCst);
            if tx.send(msg.clone()).is_err() {
                self.pending.fetch_sub(1, Ordering::SeqCst);
            }
        }
    }

    /// Stop accepting messages. The writer sends what is queued, then drops the pipe (which
    /// closes the helper's stdin). Never blocks.
    pub fn close_writer(&self) {
        *self.queue.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    pub fn is_speaking(&self) -> bool {
        self.speaking.load(Ordering::SeqCst)
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }
}

fn write_loop(rx: mpsc::Receiver<ToHelper>, mut writer: impl Write, pending: &AtomicUsize) {
    for msg in rx {
        let Ok(mut line) = serde_json::to_string(&msg) else {
            pending.fetch_sub(1, Ordering::SeqCst);
            continue;
        };
        line.push('\n');
        let result = writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush());
        pending.fetch_sub(1, Ordering::SeqCst);
        if result.is_err() {
            tracing::debug!("write to helper failed; writer ends");
            break;
        }
    }
}

/// A voice that lives in the helper process.
pub struct HelperVoice {
    link: Arc<HelperLink>,
    turn: AtomicU64,
}

impl HelperVoice {
    pub fn new(link: Arc<HelperLink>) -> Self {
        Self {
            link,
            turn: AtomicU64::new(0),
        }
    }
}

impl Speaks for HelperVoice {
    fn speak(&self, text: &str) {
        if text.trim().is_empty() || self.link.is_closed() {
            return;
        }
        self.link.send(&ToHelper::Say {
            turn: self.turn.load(Ordering::SeqCst),
            text: text.to_string(),
        });
    }

    fn stop(&self) {
        if self.link.is_closed() {
            return;
        }
        self.link.send(&ToHelper::Stop);
        self.turn.fetch_add(1, Ordering::SeqCst);
    }

    fn is_speaking(&self) -> bool {
        self.link.is_speaking()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::pipe;
    use std::sync::mpsc;
    use std::time::Duration;

    const WAIT: Duration = Duration::from_secs(10);

    /// A writer whose bytes the test can read back.
    #[derive(Clone, Default)]
    struct Sink(Arc<Mutex<Vec<u8>>>);
    impl Write for Sink {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    impl Sink {
        fn text(&self) -> String {
            String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
        }
    }

    struct Rig {
        voice: HelperVoice,
        sink: Sink,
        helper_out: std::io::PipeWriter,
        events: mpsc::Receiver<LinkEvent>,
    }

    fn rig() -> Rig {
        let (r, helper_out) = pipe().unwrap();
        let sink = Sink::default();
        let (tx, events) = mpsc::channel();
        let link = HelperLink::start(r, sink.clone(), Box::new(move |e| drop(tx.send(e))));
        Rig {
            voice: HelperVoice::new(link),
            sink,
            helper_out,
            events,
        }
    }

    /// Wait (bounded) until the sink holds `n` complete lines.
    fn wait_for_lines(sink: &Sink, n: usize) {
        let deadline = std::time::Instant::now() + WAIT;
        while sink.text().matches('\n').count() < n {
            assert!(std::time::Instant::now() < deadline, "{n} lines in time");
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn lines(sink: &Sink) -> Vec<serde_json::Value> {
        sink.text()
            .split_inclusive('\n')
            .map(|l| {
                assert!(l.ends_with('\n'));
                serde_json::from_str(l).unwrap()
            })
            .collect()
    }

    #[test]
    fn say_is_one_line_whatever_the_text() {
        let r = rig();
        let text = format!("a\n\"b\" 🙂{}", "x".repeat(2000));
        r.voice.speak(&text);
        wait_for_lines(&r.sink, 1);
        let out = r.sink.text();
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
        let v = &lines(&r.sink)[0];
        assert_eq!(v["type"], "say");
        assert_eq!(v["turn"], 0);
        assert_eq!(v["text"], text.as_str());
    }

    #[test]
    fn blank_text_is_not_sent() {
        let r = rig();
        r.voice.speak("");
        r.voice.speak("  \n\t ");
        r.voice.speak("real"); // queued after the blanks: if they had been sent they would come first
        wait_for_lines(&r.sink, 1);
        let v = lines(&r.sink);
        assert_eq!(v.len(), 1);
        assert_eq!(v[0]["text"], "real");
    }

    #[test]
    fn speaking_follows_the_helper() {
        let mut r = rig();
        writeln!(r.helper_out, r#"{{"type":"speaking","on":true,"turn":0}}"#).unwrap();
        assert_eq!(
            r.events.recv_timeout(WAIT).unwrap(),
            LinkEvent::Speaking(true)
        );
        assert!(r.voice.is_speaking());
        writeln!(r.helper_out, r#"{{"type":"speaking","on":false,"turn":0}}"#).unwrap();
        assert_eq!(
            r.events.recv_timeout(WAIT).unwrap(),
            LinkEvent::Speaking(false)
        );
        assert!(!r.voice.is_speaking());
    }

    #[test]
    fn a_closed_pipe_clears_speaking_and_reports_closed() {
        let mut r = rig();
        writeln!(r.helper_out, r#"{{"type":"speaking","on":true,"turn":0}}"#).unwrap();
        assert_eq!(
            r.events.recv_timeout(WAIT).unwrap(),
            LinkEvent::Speaking(true)
        );
        drop(r.helper_out);
        assert_eq!(r.events.recv_timeout(WAIT).unwrap(), LinkEvent::Closed);
        assert!(!r.voice.is_speaking());
        // exactly once, and speak/stop are now silent no-ops
        r.voice.speak("hello");
        r.voice.stop();
        thread::sleep(Duration::from_millis(100));
        assert_eq!(r.sink.text(), "");
        assert!(r.events.recv_timeout(Duration::from_millis(200)).is_err());
    }

    #[test]
    fn stop_bumps_the_turn() {
        let r = rig();
        r.voice.speak("one");
        r.voice.stop();
        r.voice.speak("two");
        wait_for_lines(&r.sink, 3);
        let v = lines(&r.sink);
        assert_eq!(v.len(), 3);
        assert_eq!(
            (v[0]["type"].as_str(), v[0]["turn"].as_u64()),
            (Some("say"), Some(0))
        );
        assert_eq!(v[1]["type"], "stop");
        assert_eq!(
            (v[2]["type"].as_str(), v[2]["turn"].as_u64()),
            (Some("say"), Some(1))
        );
    }

    #[test]
    fn unknown_lines_are_ignored() {
        let mut r = rig();
        r.helper_out.write_all(b"not json\n").unwrap();
        r.helper_out.write_all(b"\xff\xfe\n").unwrap();
        r.helper_out
            .write_all(b"{\"type\":\"mystery\",\"x\":1}\n")
            .unwrap();
        r.helper_out.write_all(b"[1,2]\n\n").unwrap();
        writeln!(
            r.helper_out,
            r#"{{"type":"ready","protocol":1,"helper_version":"9","voice":"v","extra":true}}"#
        )
        .unwrap();
        assert_eq!(
            r.events.recv_timeout(WAIT).unwrap(),
            LinkEvent::Ready {
                helper_version: "9".into()
            }
        );
        writeln!(
            r.helper_out,
            r#"{{"type":"error","code":"c","message":"m"}}"#
        )
        .unwrap();
        assert_eq!(
            r.events.recv_timeout(WAIT).unwrap(),
            LinkEvent::Error {
                code: "c".into(),
                message: "m".into()
            }
        );
    }

    /// A writer that blocks every write until the gate opens (or 10 s pass).
    struct Gated {
        gate: Arc<(Mutex<bool>, std::sync::Condvar)>,
        sink: Sink,
    }
    impl Write for Gated {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            let (open, cv) = &*self.gate;
            let guard = open.lock().unwrap();
            let _ = cv.wait_timeout_while(guard, WAIT, |o| !*o).unwrap();
            self.sink.write(b)
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn the_queue_cap_drops_say_but_still_accepts_stop() {
        let gate = Arc::new((Mutex::new(false), std::sync::Condvar::new()));
        let sink = Sink::default();
        let (r, _keep) = pipe().unwrap();
        let link = HelperLink::start(
            r,
            Gated {
                gate: gate.clone(),
                sink: sink.clone(),
            },
            Box::new(|_| {}),
        );
        let voice = HelperVoice::new(link);
        for _ in 0..400 {
            voice.speak("hello"); // returns at once although the writer is blocked
        }
        voice.stop();
        *gate.0.lock().unwrap() = true;
        gate.1.notify_all();
        let deadline = std::time::Instant::now() + WAIT;
        while std::time::Instant::now() < deadline && !sink.text().contains("\"stop\"") {
            thread::sleep(Duration::from_millis(10));
        }
        let v = lines(&sink);
        let says = v.iter().filter(|m| m["type"] == "say").count();
        assert_eq!(says, MAX_QUEUED_SAY + 1);
        assert_eq!(v.last().unwrap()["type"], "stop");
    }

    #[test]
    fn a_failed_write_is_dropped_quietly() {
        struct Broken;
        impl Write for Broken {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let (r, _keep) = pipe().unwrap();
        let link = HelperLink::start(r, Broken, Box::new(|_| {}));
        let voice = HelperVoice::new(link);
        voice.speak("hello");
        voice.stop();
    }
}
