//! JARVIS's voice: a local neural voice (Kokoro, "George") that speaks each
//! sentence as soon as it is ready and stops the instant it is interrupted.
//!
//! The engine and the speaker sit behind small traits so the queueing and
//! interruption logic is tested without audio hardware or model files.

pub mod model;
pub mod speaker;

#[cfg(windows)]
pub mod kokoro;

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;

/// Mono audio.
#[derive(Debug, Clone, PartialEq)]
pub struct Audio {
    pub samples: Vec<f32>,
    pub sample_rate: u32,
}

/// Turns text into audio. Runs on the voice thread.
pub trait SpeechEngine: Send + 'static {
    fn synth(&mut self, text: &str) -> Result<Audio, String>;
}

/// Plays audio, returning when it finishes or as soon as `cancelled()` is true.
/// Created on the voice thread (audio devices are often not `Send`).
pub trait AudioOut {
    fn play(&mut self, audio: Audio, cancelled: &dyn Fn() -> bool);
}

pub type OutFactory = Box<dyn FnOnce() -> Result<Box<dyn AudioOut>, String> + Send>;

/// Handle to the voice thread.
pub struct Voice {
    tx: mpsc::Sender<(u64, String)>,
    epoch: Arc<AtomicU64>,
    speaking: Arc<AtomicBool>,
}

impl Voice {
    pub fn start(mut engine: Box<dyn SpeechEngine>, out: OutFactory) -> Self {
        let (tx, rx) = mpsc::channel::<(u64, String)>();
        let epoch = Arc::new(AtomicU64::new(0));
        let current = epoch.clone();
        let speaking = Arc::new(AtomicBool::new(false));
        let playing = speaking.clone();
        thread::Builder::new()
            .name("jarvis-voice".into())
            .spawn(move || {
                let mut out = match out() {
                    Ok(o) => o,
                    Err(e) => {
                        tracing::error!(error = %e, "no audio output; voice disabled");
                        return;
                    }
                };
                for (e, text) in rx {
                    let stale = || current.load(Ordering::SeqCst) != e;
                    if stale() {
                        continue;
                    }
                    match engine.synth(&text) {
                        Ok(audio) if !stale() => {
                            playing.store(true, Ordering::SeqCst);
                            out.play(audio, &stale);
                            playing.store(false, Ordering::SeqCst);
                        }
                        Ok(_) => {}
                        Err(err) => tracing::warn!(error = %err, "couldn't synthesise a sentence"),
                    }
                }
            })
            .expect("spawn voice thread");
        Self {
            tx,
            epoch,
            speaking,
        }
    }

    /// Is JARVIS's voice playing right now? (The ears ignore the mic meanwhile.)
    pub fn is_speaking(&self) -> bool {
        self.speaking.load(Ordering::SeqCst)
    }

    /// The same signal, shareable with another thread.
    pub fn speaking_flag(&self) -> Arc<AtomicBool> {
        self.speaking.clone()
    }

    /// Queue a sentence.
    pub fn speak(&self, text: &str) {
        let e = self.epoch.load(Ordering::SeqCst);
        let _ = self.tx.send((e, text.to_string()));
    }

    /// Stop what is playing and drop everything queued.
    pub fn stop(&self) {
        self.epoch.fetch_add(1, Ordering::SeqCst);
    }
}

impl jarvis_core::Speaks for Voice {
    fn speak(&self, text: &str) {
        Voice::speak(self, text)
    }

    fn stop(&self) {
        Voice::stop(self)
    }

    fn is_speaking(&self) -> bool {
        Voice::is_speaking(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    /// Audio whose length encodes the text, so tests can tell sentences apart.
    struct Echo {
        fail_on: Option<&'static str>,
    }
    impl SpeechEngine for Echo {
        fn synth(&mut self, text: &str) -> Result<Audio, String> {
            if Some(text) == self.fail_on {
                return Err("boom".into());
            }
            Ok(Audio {
                samples: vec![0.0; text.len()],
                sample_rate: 24_000,
            })
        }
    }

    #[derive(Default)]
    struct Log {
        finished: Vec<usize>,
        cut: Vec<usize>,
    }

    /// "Plays" for `ms` milliseconds, checking for cancellation as rodio would.
    struct FakeOut {
        log: Arc<Mutex<Log>>,
        ms: u64,
    }
    impl AudioOut for FakeOut {
        fn play(&mut self, audio: Audio, cancelled: &dyn Fn() -> bool) {
            let end = Instant::now() + Duration::from_millis(self.ms);
            while Instant::now() < end {
                if cancelled() {
                    self.log.lock().unwrap().cut.push(audio.samples.len());
                    return;
                }
                thread::sleep(Duration::from_millis(2));
            }
            self.log.lock().unwrap().finished.push(audio.samples.len());
        }
    }

    fn voice(ms: u64, fail_on: Option<&'static str>) -> (Voice, Arc<Mutex<Log>>) {
        let log = Arc::new(Mutex::new(Log::default()));
        let l = log.clone();
        let v = Voice::start(
            Box::new(Echo { fail_on }),
            Box::new(move || Ok(Box::new(FakeOut { log: l, ms }) as Box<dyn AudioOut>)),
        );
        (v, log)
    }

    fn wait_for(log: &Arc<Mutex<Log>>, cond: impl Fn(&Log) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if cond(&log.lock().unwrap()) {
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
        let l = log.lock().unwrap();
        panic!("timed out; finished={:?} cut={:?}", l.finished, l.cut);
    }

    #[test]
    fn speaks_sentences_in_order() {
        let (v, log) = voice(10, None);
        v.speak("a");
        v.speak("bb");
        v.speak("ccc");
        wait_for(&log, |l| l.finished.len() == 3);
        assert_eq!(log.lock().unwrap().finished, vec![1, 2, 3]);
    }

    #[test]
    fn stop_cuts_the_current_sentence_and_drops_the_queue() {
        let (v, log) = voice(300, None);
        v.speak("first");
        v.speak("second");
        v.speak("third");
        thread::sleep(Duration::from_millis(60));
        v.stop();
        wait_for(&log, |l| l.cut.len() == 1);
        thread::sleep(Duration::from_millis(400));
        let l = log.lock().unwrap();
        assert_eq!(l.cut, vec![5], "\"first\" was cut");
        assert!(
            l.finished.is_empty(),
            "nothing queued was played: {:?}",
            l.finished
        );
    }

    #[test]
    fn speech_after_a_stop_plays_normally() {
        let (v, log) = voice(30, None);
        v.speak("one");
        v.stop();
        v.speak("again");
        wait_for(&log, |l| l.finished.contains(&5));
    }

    #[test]
    fn reports_when_it_is_speaking() {
        let (v, log) = voice(200, None);
        assert!(!v.is_speaking());
        v.speak("hello");
        thread::sleep(Duration::from_millis(80));
        assert!(v.is_speaking(), "speaking during playback");
        wait_for(&log, |l| l.finished.len() == 1);
        thread::sleep(Duration::from_millis(20));
        assert!(!v.is_speaking(), "quiet afterwards");
        let flag = v.speaking_flag();
        v.speak("again");
        thread::sleep(Duration::from_millis(80));
        assert!(
            flag.load(std::sync::atomic::Ordering::SeqCst),
            "shared flag follows too"
        );
    }

    #[test]
    fn a_sentence_that_fails_to_synthesise_is_skipped() {
        let (v, log) = voice(5, Some("bad"));
        v.speak("ok");
        v.speak("bad");
        v.speak("fine");
        wait_for(&log, |l| l.finished.len() == 2);
        assert_eq!(log.lock().unwrap().finished, vec![2, 4]);
    }
}
