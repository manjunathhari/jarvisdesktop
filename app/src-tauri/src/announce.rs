//! Results Jarvis reports on its own (core's `speak()`, such as "moved to the Recycle Bin"):
//! they answer no utterance, so `ServerBrain` never follows them. The owner chose (25 Nov) that
//! the desktop says and shows them when JARVIS is free, and holds them while it is listening
//! or thinking, until that turn is over.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use jarvis_core::coordinator::Mode;
use jarvis_node::node::ReplyEvent;
use jarvis_node::speech::Announcements;
use tokio::sync::broadcast;

/// At most this many results wait; older ones give way.
const QUEUE_MAX: usize = 8;

/// Whether JARVIS is free to speak, and what waits until it is.
pub struct Announcer {
    state: Mutex<(bool, VecDeque<String>)>,
}

impl Default for Announcer {
    fn default() -> Self {
        Announcer {
            state: Mutex::new((true, VecDeque::new())),
        }
    }
}

/// Free: nothing of a turn is under way (idle, the follow-up window, or the mic off).
fn free(m: Mode) -> bool {
    matches!(m, Mode::Idle | Mode::FollowUp | Mode::MicOff)
}

impl Announcer {
    /// A result arrived: return it to say now, or hold it.
    pub fn arrived(&self, text: String) -> Option<String> {
        let Ok(mut s) = self.state.lock() else {
            return Some(text);
        };
        if s.0 {
            return Some(text);
        }
        if s.1.len() >= QUEUE_MAX {
            s.1.pop_front();
        }
        s.1.push_back(text);
        None
    }

    /// JARVIS changed mode: return the held results to say now, in order.
    pub fn mode(&self, m: Mode) -> Vec<String> {
        let Ok(mut s) = self.state.lock() else {
            return vec![];
        };
        s.0 = free(m);
        if s.0 {
            s.1.drain(..).collect()
        } else {
            vec![]
        }
    }
}

/// Follow the node's replies; say each finished announcement through `say` when free.
pub async fn run(
    mut rx: broadcast::Receiver<ReplyEvent>,
    announcer: Arc<Announcer>,
    say: impl Fn(&str) + Send + 'static,
) {
    let mut collected = Announcements::default();
    loop {
        match rx.recv().await {
            Ok(ev) => {
                if let Some(text) = collected.on_event(&ev) {
                    if let Some(now) = announcer.arrived(text) {
                        say(&now);
                    }
                }
            }
            Err(broadcast::error::RecvError::Lagged(n)) => {
                tracing::warn!(lost = n, "announcements lagged behind the node");
            }
            Err(broadcast::error::RecvError::Closed) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn said_at_once_when_free() {
        let a = Announcer::default();
        assert_eq!(
            a.arrived("Moved to the Recycle Bin.".into()).as_deref(),
            Some("Moved to the Recycle Bin.")
        );
        assert!(a.mode(Mode::FollowUp).is_empty());
        assert!(a.arrived("Done.".into()).is_some());
    }

    #[test]
    fn held_while_a_turn_is_under_way_then_said_in_order() {
        let a = Announcer::default();
        assert!(a.mode(Mode::Listening).is_empty());
        assert_eq!(a.arrived("First.".into()), None);
        assert!(a.mode(Mode::Thinking).is_empty());
        assert_eq!(a.arrived("Second.".into()), None);
        assert_eq!(a.mode(Mode::Idle), ["First.", "Second."]);
        assert!(a.mode(Mode::Idle).is_empty(), "said once");
    }

    #[test]
    fn the_queue_is_bounded() {
        let a = Announcer::default();
        a.mode(Mode::Thinking);
        for i in 0..20 {
            a.arrived(format!("{i}"));
        }
        let said = a.mode(Mode::Idle);
        assert_eq!(said.len(), QUEUE_MAX);
        assert_eq!(said.last().map(String::as_str), Some("19"));
    }
}
