//! The pipe link: a reader thread turns the helper's lines into events, writes go through a mutex.

use std::io::{BufRead, BufReader, Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
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

type Writer = Box<dyn Write + Send>;

pub struct HelperLink {
    writer: Mutex<Option<Writer>>,
    speaking: AtomicBool,
    closed: AtomicBool,
}

impl HelperLink {
    pub fn start(
        reader: impl Read + Send + 'static,
        writer: impl Write + Send + 'static,
        on_event: Box<dyn Fn(LinkEvent) + Send>,
    ) -> Arc<HelperLink> {
        let link = Arc::new(HelperLink {
            writer: Mutex::new(Some(Box::new(writer))),
            speaking: AtomicBool::new(false),
            closed: AtomicBool::new(false),
        });
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

    /// Write one message as one line. A failed write (the helper has gone) is dropped;
    /// `Closed` comes from the reader.
    pub fn send(&self, msg: &ToHelper) {
        let Ok(mut line) = serde_json::to_string(msg) else {
            return;
        };
        line.push('\n');
        let mut guard = self.writer.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(w) = guard.as_mut() {
            if w.write_all(line.as_bytes())
                .and_then(|_| w.flush())
                .is_err()
            {
                tracing::debug!("write to helper failed; dropped");
            }
        }
    }

    /// Drop the writing end (closes the helper's stdin).
    pub fn close_writer(&self) {
        *self.writer.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    pub fn is_speaking(&self) -> bool {
        self.speaking.load(Ordering::SeqCst)
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
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
        assert_eq!(r.sink.text(), "");
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
        assert_eq!(r.sink.text(), "");
        assert!(r.events.recv_timeout(Duration::from_millis(200)).is_err());
    }

    #[test]
    fn stop_bumps_the_turn() {
        let r = rig();
        r.voice.speak("one");
        r.voice.stop();
        r.voice.speak("two");
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
