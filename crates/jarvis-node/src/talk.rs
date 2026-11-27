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
//! - A typed line reaches the coordinator like a spoken one, so the app says "this text was
//!   typed" first; the mark is for that text only and is used once.

use std::sync::mpsc;
use std::sync::{Arc, Mutex};

use uuid::Uuid;

use crate::board::{BoardStore, ConversationEntry, How, Who};

/// The longest line kept, in characters. A spoken sentence or a typed request is far shorter.
pub const TEXT_MAX: usize = 2000;

enum Job {
    Heard(String),
    Typed(String),
    Spoke(String),
    Panel(Uuid),
}

/// Hand lines to the recorder; cheap to call from anywhere.
pub struct Recorder {
    jobs: Mutex<mpsc::Sender<Job>>,
}

fn kept(text: &str) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.chars().take(TEXT_MAX).collect())
}

impl Recorder {
    /// Start recording into `store`. `on_entry` is called, on the recorder's own thread, with
    /// each line once it has been written (or could not be: it is still shown).
    pub fn start(
        store: Arc<Mutex<BoardStore>>,
        on_entry: impl Fn(&ConversationEntry) + Send + 'static,
    ) -> Self {
        let (jobs, work) = mpsc::channel::<Job>();
        let spawned = std::thread::Builder::new()
            .name("board-conversation".into())
            .spawn(move || {
                let mut typed: Option<String> = None;
                let mut shown: Vec<Uuid> = Vec::new();
                for job in work {
                    let (who, how, text, panels) = match job {
                        Job::Typed(text) => {
                            typed = Some(text);
                            continue;
                        }
                        Job::Panel(id) => {
                            if !shown.contains(&id) {
                                shown.push(id);
                            }
                            continue;
                        }
                        Job::Heard(text) => {
                            let how = if typed.take().as_deref() == Some(text.as_str()) {
                                How::Typed
                            } else {
                                How::Voice
                            };
                            (Who::Person, how, text, Vec::new())
                        }
                        Job::Spoke(text) => {
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
                    on_entry(&entry);
                }
            });
        if let Err(e) = spawned {
            tracing::warn!(error = %e, "the conversation recorder could not start");
        }
        Recorder {
            jobs: Mutex::new(jobs),
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
            self.send(Job::Typed(text));
        }
    }

    /// Jarvis said this.
    pub fn spoke(&self, text: &str) {
        if let Some(text) = kept(text) {
            self.send(Job::Spoke(text));
        }
    }

    /// A panel was shown or updated on the Board.
    pub fn panel_shown(&self, panel_id: Uuid) {
        self.send(Job::Panel(panel_id));
    }
}
