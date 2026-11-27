//! The conversation recorder for the Board's Conversation column.
//!
//! The app tells it what was heard, what was typed and what Jarvis said, and which panels
//! were shown. It writes each line to the Board's store (`conversation.jsonl`) and hands it
//! to the app for the window, in the order the calls came.
//!
//! - Calls never wait: they come from the voice loop and from command handlers, and the store
//!   does file work under its lock. One worker thread does the writing.
//! - A Jarvis line carries the panels shown since the previous Jarvis line. A Board event
//!   does not say which reply made a panel, and the headline is its own reply, so this is the
//!   nearest honest link: the line spoken after a panel went up points at it.
//! - "Jarvis is working": the server says when Board work starts (`board.working`) and
//!   nothing says when it ends, so it ends here: when a panel is shown, when Jarvis speaks
//!   unasked (the headline, or what went wrong), or after [`WORKING_LASTS`].
//! - A typed line reaches the coordinator like a spoken one, so the app says "this text was
//!   typed" first; the mark is for that text only, is used once, and lasts a few seconds.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use uuid::Uuid;

use crate::board::{BoardStore, ConversationEntry, How, Who};

/// The longest line kept, in characters. A spoken sentence or a typed request is far shorter.
pub const TEXT_MAX: usize = 2000;
/// How long a "this was typed" mark waits for its line. The line follows at once; a mark
/// whose line was dropped (Jarvis was busy) must not label the same words spoken later.
pub const TYPED_MARK_LASTS: Duration = Duration::from_secs(3);

/// How long "Jarvis is working" is shown if nothing ends it. Board work takes 30 to 90
/// seconds; after this the line is taken down, not left up for ever.
pub const WORKING_LASTS: Duration = Duration::from_secs(180);

/// What the recorder tells the app, for the window.
pub enum Talk<'a> {
    /// A line of the conversation, already written to the store.
    Line(&'a ConversationEntry),
    /// Jarvis started (`true`) or stopped (`false`) working on something for the Board.
    Working(bool),
}

enum Job {
    /// The server said Board work has started (`board.working`).
    Working,
    /// Jarvis spoke unasked (the headline, or what went wrong): a line, and the work is over.
    Announced(String),
    Heard(String),
    Typed(String, Instant),
    Spoke(String),
    Panel(Uuid),
}

/// Hand lines to the recorder; cheap to call from anywhere.
pub struct Recorder {
    jobs: Mutex<mpsc::Sender<Job>>,
    working: Arc<AtomicBool>,
}

fn kept(text: &str) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.chars().take(TEXT_MAX).collect())
}

impl Recorder {
    /// Start recording into `store`. `on_talk` is called, on the recorder's own thread, with
    /// each line once it has been written (or could not be: it is still shown), and with each
    /// change of "Jarvis is working".
    pub fn start(
        store: Arc<Mutex<BoardStore>>,
        on_talk: impl Fn(Talk<'_>) + Send + 'static,
    ) -> Self {
        Self::start_with(store, WORKING_LASTS, on_talk)
    }

    /// As [`Recorder::start`], with how long "working" lasts unanswered (for tests).
    pub fn start_with(
        store: Arc<Mutex<BoardStore>>,
        working_lasts: Duration,
        on_talk: impl Fn(Talk<'_>) + Send + 'static,
    ) -> Self {
        let (jobs, work) = mpsc::channel::<Job>();
        let working = Arc::new(AtomicBool::new(false));
        let flag = working.clone();
        let spawned = std::thread::Builder::new()
            .name("board-conversation".into())
            .spawn(move || {
                let mut typed: Option<(String, Instant)> = None;
                let mut shown: Vec<Uuid> = Vec::new();
                // While working: when the line is taken down if nothing ends it.
                let mut working_until: Option<Instant> = None;
                let stop_working = |until: &mut Option<Instant>| {
                    if until.take().is_some() {
                        flag.store(false, Ordering::SeqCst);
                        on_talk(Talk::Working(false));
                    }
                };
                loop {
                    let job = match working_until {
                        None => match work.recv() {
                            Ok(job) => job,
                            Err(_) => return,
                        },
                        Some(until) => {
                            // The time is checked first, so that a steady stream of lines
                            // cannot keep "working" up past its time.
                            if Instant::now() >= until {
                                stop_working(&mut working_until);
                                continue;
                            }
                            match work.recv_timeout(until.saturating_duration_since(Instant::now()))
                            {
                                Ok(job) => job,
                                Err(RecvTimeoutError::Timeout) => {
                                    stop_working(&mut working_until);
                                    continue;
                                }
                                Err(RecvTimeoutError::Disconnected) => return,
                            }
                        }
                    };
                    let (who, how, text, panels) = match job {
                        Job::Working => {
                            let already = working_until.is_some();
                            working_until = Some(Instant::now() + working_lasts);
                            if !already {
                                flag.store(true, Ordering::SeqCst);
                                on_talk(Talk::Working(true));
                            }
                            continue;
                        }
                        Job::Typed(text, at) => {
                            typed = Some((text, at));
                            continue;
                        }
                        Job::Panel(id) => {
                            if !shown.contains(&id) {
                                shown.push(id);
                            }
                            stop_working(&mut working_until); // the work is on the Board
                            continue;
                        }
                        Job::Heard(text) => {
                            // The mark is for exactly these words, soon after: any other
                            // line leaves it waiting, and time runs it out.
                            let marked = typed.as_ref().is_some_and(|(marked, at)| {
                                *marked == text && at.elapsed() <= TYPED_MARK_LASTS
                            });
                            let how = if marked {
                                typed = None;
                                How::Typed
                            } else {
                                How::Voice
                            };
                            (Who::Person, how, text, Vec::new())
                        }
                        Job::Spoke(text) => {
                            (Who::Jarvis, How::Spoken, text, std::mem::take(&mut shown))
                        }
                        Job::Announced(text) => {
                            stop_working(&mut working_until);
                            (Who::Jarvis, How::Spoken, text, std::mem::take(&mut shown))
                        }
                    };
                    let entry = ConversationEntry {
                        at: chrono::Local::now().fixed_offset(),
                        who,
                        text,
                        how,
                        panels,
                    };
                    let written = store
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .append_turn(entry.clone());
                    if let Err(e) = written {
                        tracing::warn!(error = %e, "couldn't keep a line of the conversation");
                    }
                    on_talk(Talk::Line(&entry));
                }
            });
        if let Err(e) = spawned {
            tracing::warn!(error = %e, "the conversation recorder could not start");
        }
        Recorder {
            jobs: Mutex::new(jobs),
            working,
        }
    }

    fn send(&self, job: Job) {
        // After the worker has gone (it never stops while the app runs) lines are dropped.
        let _ = self
            .jobs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .send(job);
    }

    /// The person said this (or typed it: see [`Recorder::typed`]).
    pub fn heard(&self, text: &str) {
        if let Some(text) = kept(text) {
            self.send(Job::Heard(text));
        }
    }

    /// This text is about to come through as heard, and it was typed.
    pub fn typed(&self, text: &str) {
        if let Some(text) = kept(text) {
            self.send(Job::Typed(text, Instant::now()));
        }
    }

    /// Jarvis said this.
    pub fn spoke(&self, text: &str) {
        if let Some(text) = kept(text) {
            self.send(Job::Spoke(text));
        }
    }

    /// Jarvis said this unasked (an announcement: the headline when a panel is up, or a
    /// sentence saying what went wrong). Recorded as a line, and it ends "working".
    pub fn announced(&self, text: &str) {
        if let Some(text) = kept(text) {
            self.send(Job::Announced(text));
        }
    }

    /// The server said Board work has started. "Working" lasts until a panel is shown, an
    /// announcement is spoken, or its time runs out.
    pub fn working_started(&self) {
        self.send(Job::Working);
    }

    /// Whether Jarvis is working on something for the Board now (for a window that opens
    /// part-way through).
    pub fn is_working(&self) -> bool {
        self.working.load(Ordering::SeqCst)
    }

    /// A panel was shown or updated on the Board.
    pub fn panel_shown(&self, panel_id: Uuid) {
        self.send(Job::Panel(panel_id));
    }
}
