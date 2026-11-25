//! Turning the server's reply chunks into things the desktop can say.
//!
//! The server streams Claude's text in the pieces it arrives in, often parts of words. The
//! desktop's voice says each `Speech` event as one utterance, so pieces must be joined into
//! whole sentences first ([`Sentences`]), as the desktop's own Claude brain does (live check,
//! 25 Nov: spoken pieces sounded like gibberish).
//!
//! Replies that answer no utterance (`in_reply_to` null: core's `speak()` results, such as
//! "moved to the Recycle Bin") are collected whole by [`Announcements`], for the app to say
//! when JARVIS is free (owner, 25 Nov).

use std::collections::{HashMap, VecDeque};

use uuid::Uuid;

use crate::node::ReplyEvent;

/// Joins streamed text into whole sentences.
#[derive(Debug, Default)]
pub struct Sentences {
    buf: String,
}

impl Sentences {
    /// Add a piece; return the sentences it completes, in order.
    pub fn push(&mut self, piece: &str) -> Vec<String> {
        self.buf.push_str(piece);
        let mut out = Vec::new();
        loop {
            // A sentence ends at . ! or ? followed by whitespace (so "3.5" and "e.g" stay whole).
            let mut chars = self.buf.char_indices().peekable();
            let mut cut = None;
            while let Some((_, c)) = chars.next() {
                if matches!(c, '.' | '!' | '?') {
                    if let Some(&(j, next)) = chars.peek() {
                        if next.is_whitespace() {
                            cut = Some(j);
                            break;
                        }
                    }
                }
            }
            let Some(j) = cut else { return out };
            let sentence = self.buf[..j].trim().to_owned();
            self.buf = self.buf[j..].trim_start().to_owned();
            if !sentence.is_empty() {
                out.push(sentence);
            }
        }
    }

    /// Whatever is left (the reply ended, or something else must be said or done first).
    pub fn finish(&mut self) -> Option<String> {
        let rest = std::mem::take(&mut self.buf);
        let rest = rest.trim();
        (!rest.is_empty()).then(|| rest.to_owned())
    }
}

/// How many replies [`Announcements`] collects at once, and remembers as finished.
const OPEN_MAX: usize = 16;
const DONE_MAX: usize = 64;

/// Collects replies that answer no utterance until their final chunk.
#[derive(Debug, Default)]
pub struct Announcements {
    open: HashMap<Uuid, (i64, String)>,
    order: VecDeque<Uuid>,
    done: VecDeque<Uuid>,
}

impl Announcements {
    /// Feed every reply event; returns the whole text of an announcement once it is complete.
    /// Events of replies to an utterance, actions, re-sent chunks and replies already
    /// announced are ignored.
    pub fn on_event(&mut self, ev: &ReplyEvent) -> Option<String> {
        let ReplyEvent::Speech {
            reply_id,
            seq,
            text,
            final_,
            in_reply_to: None,
        } = ev
        else {
            return None;
        };
        if self.done.contains(reply_id) {
            return None;
        }
        if !self.open.contains_key(reply_id) {
            if self.order.len() >= OPEN_MAX {
                if let Some(old) = self.order.pop_front() {
                    self.open.remove(&old);
                }
            }
            self.order.push_back(*reply_id);
            self.open.insert(*reply_id, (0, String::new()));
        }
        let (last, buf) = self.open.get_mut(reply_id)?;
        if *seq <= *last {
            return None; // a re-send
        }
        *last = *seq;
        buf.push_str(text);
        if !final_ {
            return None;
        }
        let (_, whole) = self.open.remove(reply_id)?;
        self.order.retain(|id| id != reply_id);
        if self.done.len() >= DONE_MAX {
            self.done.pop_front();
        }
        self.done.push_back(*reply_id);
        let whole = whole.trim();
        (!whole.is_empty()).then(|| whole.to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn half_words_become_whole_sentences() {
        let mut s = Sentences::default();
        let mut said = Vec::new();
        for piece in [
            "The mo",
            "on is about 3.",
            "5 times smal",
            "ler than Earth. It ",
            "has no air",
            "!",
        ] {
            said.extend(s.push(piece));
        }
        assert_eq!(said, ["The moon is about 3.5 times smaller than Earth."]);
        assert_eq!(s.finish().as_deref(), Some("It has no air!"));
        assert_eq!(s.finish(), None);
    }

    #[test]
    fn several_sentences_in_one_piece_come_out_in_order() {
        let mut s = Sentences::default();
        assert_eq!(s.push("One. Two? Three! Fo"), ["One.", "Two?", "Three!"]);
        assert_eq!(s.finish().as_deref(), Some("Fo"));
    }

    fn speech(
        reply_id: Uuid,
        seq: i64,
        text: &str,
        final_: bool,
        in_reply_to: Option<Uuid>,
    ) -> ReplyEvent {
        ReplyEvent::Speech {
            reply_id,
            seq,
            text: text.into(),
            final_,
            in_reply_to,
        }
    }

    #[test]
    fn an_announcement_is_given_whole_once() {
        let mut a = Announcements::default();
        let r = Uuid::new_v4();
        assert_eq!(
            a.on_event(&speech(r, 1, "Moved old-notes.txt ", false, None)),
            None
        );
        assert_eq!(
            a.on_event(&speech(r, 1, "Moved old-notes.txt ", false, None)),
            None
        );
        assert_eq!(
            a.on_event(&speech(r, 2, "to the Recycle Bin.", true, None))
                .as_deref(),
            Some("Moved old-notes.txt to the Recycle Bin.")
        );
        // Re-sent after a reconnect: not said again.
        assert_eq!(
            a.on_event(&speech(r, 2, "to the Recycle Bin.", true, None)),
            None
        );
    }

    #[test]
    fn replies_to_an_utterance_are_not_announcements() {
        let mut a = Announcements::default();
        let r = Uuid::new_v4();
        assert_eq!(
            a.on_event(&speech(r, 1, "Hello.", true, Some(Uuid::new_v4()))),
            None
        );
    }
}
