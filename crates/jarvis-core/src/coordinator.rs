//! The loop that joins Ears, Brain, Voice, Modules and Screen.
//!
//! `step` is pure: it takes an event and returns effects for the runtime to
//! carry out. No I/O happens here, so every transition is unit-testable.

use crate::action::{Action, ModuleOutput};
use crate::brain::{Role, Turn};
use crate::screen::{PanelUpdate, ScreenState, ScreenView};
use serde::Serialize;
use std::time::Duration;

pub const LISTEN_TIMEOUT: Duration = Duration::from_secs(10);
/// How long JARVIS keeps listening after a reply before the mic goes off (owner, 25 Nov: 30 s).
pub const FOLLOW_UP_WINDOW: Duration = Duration::from_secs(30);
const MAX_HISTORY: usize = 6;
/// How many channels to try in a row when live video refuses to play.
pub const MAX_VIDEO_SKIPS: u8 = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Idle,
    Listening,
    Thinking,
    FollowUp,
    MicOff,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Timer {
    Listen,
    FollowUp,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// "Hey Jarvis" heard. `hour` is local time 0–23, for the greeting.
    Wake {
        hour: u32,
    },
    /// Screen tapped.
    Tap {
        hour: u32,
    },
    /// What the user said (or typed, in dev mode).
    Utterance(String),
    BrainSpeech(String),
    BrainAction(Action),
    BrainDone,
    BrainFailed,
    ActionDone {
        output: ModuleOutput,
        depth: u8,
    },
    ActionFailed(String),
    Timeout {
        timer: Timer,
        id: u64,
    },
    /// The live video player reported that the channel won't play here.
    VideoFailed,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    Speak(String),
    StopSpeaking,
    Think {
        utterance: String,
        screen_summary: String,
        history: Vec<Turn>,
    },
    Dispatch {
        action: Action,
        depth: u8,
    },
    Screen(ScreenView),
    SetTimer {
        timer: Timer,
        id: u64,
        after: Duration,
    },
    Mic(bool),
    Status(Mode),
    /// What JARVIS heard, for the caption line.
    Heard(String),
    /// Close JARVIS.
    Quit,
}

/// Commands handled on the PC without the brain: instant and offline.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalCommand {
    StopTalking,
    ClearScreen,
    StopListening,
    Quit,
}

fn normalise(text: &str) -> String {
    let lowered: String = text
        .to_lowercase()
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == ' ' || c == '\'' {
                c
            } else {
                ' '
            }
        })
        .collect();
    let mut words: Vec<&str> = lowered.split_whitespace().collect();
    if words.first() == Some(&"hey") && words.get(1) == Some(&"jarvis") {
        words.drain(..2);
    } else if words.first() == Some(&"jarvis") {
        words.remove(0);
    }
    if words.last() == Some(&"please") {
        words.pop();
    }
    if words.last() == Some(&"jarvis") {
        words.pop();
    }
    words.join(" ")
}

pub fn local_command(text: &str) -> Option<LocalCommand> {
    match normalise(text).as_str() {
        "stop" | "stop talking" | "quiet" | "be quiet" | "enough" | "that's enough"
        | "thats enough" => Some(LocalCommand::StopTalking),
        "clear" | "clear the screen" | "clear screen" | "clear everything" | "clear all" => {
            Some(LocalCommand::ClearScreen)
        }
        "stop listening" | "go to sleep" | "mute" | "mute yourself" | "mic off"
        | "microphone off" => Some(LocalCommand::StopListening),
        "shut down" | "shutdown" | "exit" | "quit" | "goodbye" | "close yourself" | "turn off" => {
            Some(LocalCommand::Quit)
        }
        _ => None,
    }
}

pub fn greeting(hour: u32, name: &str) -> String {
    let part = match hour {
        5..=11 => "morning",
        12..=16 => "afternoon",
        _ => "evening",
    };
    format!("Good {part}, {name}. How can I help?")
}

pub struct Coordinator {
    user_name: String,
    mode: Mode,
    screen: ScreenState,
    history: Vec<Turn>,
    reply_so_far: String,
    retried: bool,
    timer_id: u64,
    video_skips: u8,
}

impl Coordinator {
    pub fn new(user_name: &str) -> Self {
        Self {
            user_name: user_name.into(),
            mode: Mode::Idle,
            screen: ScreenState::new(),
            history: Vec::new(),
            reply_so_far: String::new(),
            retried: false,
            timer_id: 0,
            video_skips: 0,
        }
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    pub fn screen(&self) -> &ScreenState {
        &self.screen
    }

    pub fn step(&mut self, event: Event) -> Vec<Effect> {
        let mut fx = Vec::new();
        match event {
            Event::Wake { hour } => {
                if matches!(self.mode, Mode::Idle | Mode::FollowUp) {
                    self.wake(hour, &mut fx);
                }
            }
            Event::Tap { hour } => {
                if self.mode == Mode::MicOff {
                    fx.push(Effect::Mic(true));
                }
                if self.mode != Mode::Thinking {
                    self.wake(hour, &mut fx);
                }
            }
            Event::Utterance(text) => self.on_utterance(text, &mut fx),
            Event::BrainSpeech(text) => {
                if !self.reply_so_far.is_empty() {
                    self.reply_so_far.push(' ');
                }
                self.reply_so_far.push_str(text.trim());
                fx.push(Effect::Speak(text));
            }
            Event::BrainAction(action) => fx.push(Effect::Dispatch { action, depth: 0 }),
            Event::BrainDone => {
                if self.mode == Mode::Thinking {
                    let reply = std::mem::take(&mut self.reply_so_far);
                    if !reply.is_empty() {
                        self.remember(Role::Assistant, reply);
                    }
                    self.set_mode(Mode::FollowUp, &mut fx);
                    self.start_timer(Timer::FollowUp, FOLLOW_UP_WINDOW, &mut fx);
                }
            }
            Event::BrainFailed => {
                self.reply_so_far.clear();
                fx.push(Effect::Speak(
                    "Sorry, I can't reach my brain just now. Please try again in a moment.".into(),
                ));
                self.set_mode(Mode::Idle, &mut fx);
            }
            Event::ActionDone { output, depth } => {
                if !output.updates.is_empty() {
                    for u in output.updates {
                        self.screen.apply(u);
                    }
                    fx.push(Effect::Screen(self.screen.view()));
                }
                if let Some(s) = output.speech {
                    fx.push(Effect::Speak(s));
                }
                if depth == 0 {
                    for action in output.follow_up {
                        fx.push(Effect::Dispatch { action, depth: 1 });
                    }
                }
            }
            Event::ActionFailed(msg) => fx.push(Effect::Speak(msg)),
            Event::VideoFailed => {
                if self.video_skips < MAX_VIDEO_SKIPS {
                    self.video_skips += 1;
                    fx.push(Effect::Dispatch {
                        action: Action::new("next_channel", serde_json::json!({})),
                        depth: 1,
                    });
                } else {
                    self.video_skips = 0;
                    fx.push(Effect::Speak(
                        "None of the live channels will play right now.".into(),
                    ));
                    fx.push(Effect::Dispatch {
                        action: Action::new("close_panel", serde_json::json!({ "which": "video" })),
                        depth: 1,
                    });
                }
            }
            Event::Timeout { timer, id } => {
                let live = id == self.timer_id
                    && matches!(
                        (timer, self.mode),
                        (Timer::Listen, Mode::Listening) | (Timer::FollowUp, Mode::FollowUp)
                    );
                if live {
                    self.set_mode(Mode::Idle, &mut fx);
                }
            }
        }
        fx
    }

    fn wake(&mut self, hour: u32, fx: &mut Vec<Effect>) {
        self.retried = false;
        fx.push(Effect::Speak(greeting(hour, &self.user_name)));
        self.set_mode(Mode::Listening, fx);
        self.start_timer(Timer::Listen, LISTEN_TIMEOUT, fx);
    }

    fn on_utterance(&mut self, text: String, fx: &mut Vec<Effect>) {
        if matches!(self.mode, Mode::MicOff | Mode::Thinking) {
            return;
        }
        let text = text.trim().to_string();
        if text.is_empty() {
            if self.mode == Mode::Listening && !self.retried {
                self.retried = true;
                fx.push(Effect::Speak("Sorry, I didn't catch that.".into()));
                self.start_timer(Timer::Listen, LISTEN_TIMEOUT, fx);
            } else {
                self.set_mode(Mode::Idle, fx);
            }
            return;
        }
        fx.push(Effect::Heard(text.clone()));
        self.video_skips = 0;
        match local_command(&text) {
            Some(LocalCommand::StopTalking) => {
                fx.push(Effect::StopSpeaking);
                self.set_mode(Mode::FollowUp, fx);
                self.start_timer(Timer::FollowUp, FOLLOW_UP_WINDOW, fx);
            }
            Some(LocalCommand::ClearScreen) => {
                self.screen.apply(PanelUpdate::Clear);
                fx.push(Effect::Screen(self.screen.view()));
                self.set_mode(Mode::FollowUp, fx);
                self.start_timer(Timer::FollowUp, FOLLOW_UP_WINDOW, fx);
            }
            Some(LocalCommand::Quit) => {
                fx.push(Effect::StopSpeaking);
                fx.push(Effect::Speak(format!("Goodbye, {}.", self.user_name)));
                fx.push(Effect::Quit);
            }
            Some(LocalCommand::StopListening) => {
                fx.push(Effect::StopSpeaking);
                fx.push(Effect::Speak(
                    "Microphone off. Tap the screen when you need me.".into(),
                ));
                fx.push(Effect::Mic(false));
                self.set_mode(Mode::MicOff, fx);
            }
            None => {
                fx.push(Effect::StopSpeaking);
                fx.push(Effect::Think {
                    utterance: text.clone(),
                    screen_summary: self.screen.summary(),
                    history: self.history.clone(),
                });
                self.remember(Role::User, text);
                self.reply_so_far.clear();
                self.set_mode(Mode::Thinking, fx);
            }
        }
    }

    fn remember(&mut self, role: Role, text: String) {
        self.history.push(Turn { role, text });
        if self.history.len() > MAX_HISTORY {
            let excess = self.history.len() - MAX_HISTORY;
            self.history.drain(..excess);
        }
    }

    fn set_mode(&mut self, mode: Mode, fx: &mut Vec<Effect>) {
        if self.mode != mode {
            self.mode = mode;
            fx.push(Effect::Status(mode));
        }
    }

    fn start_timer(&mut self, timer: Timer, after: Duration, fx: &mut Vec<Effect>) {
        self.timer_id += 1;
        fx.push(Effect::SetTimer {
            timer,
            id: self.timer_id,
            after,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::screen::{Card, Panel, PanelContent, Slot};
    use serde_json::json;

    fn spoken(effects: &[Effect]) -> Vec<String> {
        effects
            .iter()
            .filter_map(|e| {
                if let Effect::Speak(s) = e {
                    Some(s.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    fn timer_of(effects: &[Effect], want: Timer) -> Option<u64> {
        effects.iter().find_map(|e| match e {
            Effect::SetTimer { timer, id, .. } if *timer == want => Some(*id),
            _ => None,
        })
    }

    fn think_of(effects: &[Effect]) -> Option<(String, String, Vec<Turn>)> {
        effects.iter().find_map(|e| match e {
            Effect::Think {
                utterance,
                screen_summary,
                history,
            } => Some((utterance.clone(), screen_summary.clone(), history.clone())),
            _ => None,
        })
    }

    fn news_output() -> ModuleOutput {
        ModuleOutput {
            updates: vec![PanelUpdate::Show(Panel {
                slot: Slot::Secondary,
                content: PanelContent::CardList {
                    title: "India".into(),
                    cards: vec![Card {
                        title: "Monsoon arrives".into(),
                        source: "The Hindu".into(),
                        summary: "s".into(),
                        link: "l".into(),
                        published: None,
                    }],
                },
            })],
            speech: Some("Here is today's news.".into()),
            follow_up: vec![Action::new("play_live_tv", json!({}))],
        }
    }

    // ── greeting & local commands ────────────────────────────────

    #[test]
    fn greeting_depends_on_time_of_day() {
        assert_eq!(greeting(8, "Manj"), "Good morning, Manj. How can I help?");
        assert_eq!(
            greeting(14, "Manj"),
            "Good afternoon, Manj. How can I help?"
        );
        assert_eq!(greeting(20, "Manj"), "Good evening, Manj. How can I help?");
        assert_eq!(greeting(2, "Manj"), "Good evening, Manj. How can I help?");
    }

    // ── video that won't play ────────────────────────────────────

    fn dispatched(fx: &[Effect]) -> Vec<String> {
        fx.iter()
            .filter_map(|e| match e {
                Effect::Dispatch { action, .. } => Some(action.name.clone()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn a_channel_that_wont_play_is_skipped_automatically() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::VideoFailed);
        assert_eq!(dispatched(&fx), vec!["next_channel"]);
        assert!(fx.contains(&Effect::Dispatch {
            action: Action::new("next_channel", json!({})),
            depth: 1
        }));
    }

    #[test]
    fn after_too_many_failures_jarvis_gives_up_and_says_so() {
        let mut c = Coordinator::new("Manj");
        for _ in 0..MAX_VIDEO_SKIPS {
            assert_eq!(
                dispatched(&c.step(Event::VideoFailed)),
                vec!["next_channel"]
            );
        }
        let fx = c.step(Event::VideoFailed);
        assert_eq!(
            spoken(&fx),
            vec!["None of the live channels will play right now."]
        );
        assert_eq!(dispatched(&fx), vec!["close_panel"]);
    }

    #[test]
    fn a_new_request_resets_the_skip_count() {
        let mut c = Coordinator::new("Manj");
        for _ in 0..MAX_VIDEO_SKIPS {
            c.step(Event::VideoFailed);
        }
        c.step(Event::Utterance("put on WION".into()));
        assert_eq!(
            dispatched(&c.step(Event::VideoFailed)),
            vec!["next_channel"]
        );
    }

    #[test]
    fn shut_down_says_goodbye_then_quits() {
        assert_eq!(local_command("Jarvis, shut down"), Some(LocalCommand::Quit));
        assert_eq!(local_command("goodbye jarvis"), Some(LocalCommand::Quit));
        assert_eq!(local_command("exit"), Some(LocalCommand::Quit));
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::Utterance("shut down".into()));
        assert_eq!(spoken(&fx), vec!["Goodbye, Manj."]);
        assert_eq!(fx.last(), Some(&Effect::Quit));
        assert!(think_of(&fx).is_none());
    }

    #[test]
    fn local_commands_match_whole_phrases_only() {
        assert_eq!(local_command("Stop."), Some(LocalCommand::StopTalking));
        assert_eq!(
            local_command("Jarvis, clear the screen please"),
            Some(LocalCommand::ClearScreen)
        );
        assert_eq!(
            local_command("hey jarvis stop listening"),
            Some(LocalCommand::StopListening)
        );
        assert_eq!(local_command("stop the video"), None, "goes to the brain");
        assert_eq!(local_command("bring me the latest news"), None);
    }

    // ── waking ───────────────────────────────────────────────────

    #[test]
    fn wake_from_idle_greets_and_listens() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::Wake { hour: 9 });
        assert_eq!(spoken(&fx), vec!["Good morning, Manj. How can I help?"]);
        assert!(fx.contains(&Effect::Status(Mode::Listening)));
        assert!(timer_of(&fx, Timer::Listen).is_some());
        assert_eq!(c.mode(), Mode::Listening);
    }

    #[test]
    fn silence_after_waking_goes_back_to_idle_quietly() {
        let mut c = Coordinator::new("Manj");
        let id = timer_of(&c.step(Event::Tap { hour: 9 }), Timer::Listen).unwrap();
        let fx = c.step(Event::Timeout {
            timer: Timer::Listen,
            id,
        });
        assert!(spoken(&fx).is_empty());
        assert_eq!(c.mode(), Mode::Idle);
    }

    // ── the main loop ────────────────────────────────────────────

    #[test]
    fn utterance_goes_to_the_brain_with_the_screen_summary() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Wake { hour: 9 });
        let fx = c.step(Event::Utterance("bring me the latest news".into()));
        let (u, summary, _) = think_of(&fx).expect("think effect");
        assert_eq!(u, "bring me the latest news");
        assert_eq!(summary, "The screen is empty.");
        assert!(fx.contains(&Effect::Heard("bring me the latest news".into())));
        assert_eq!(c.mode(), Mode::Thinking);
    }

    #[test]
    fn brain_speech_is_spoken_and_actions_dispatched() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Utterance("news".into()));
        assert_eq!(
            spoken(&c.step(Event::BrainSpeech("Right away.".into()))),
            vec!["Right away."]
        );
        let a = Action::new("show_news", json!({}));
        assert_eq!(
            c.step(Event::BrainAction(a.clone())),
            vec![Effect::Dispatch {
                action: a,
                depth: 0
            }]
        );
    }

    #[test]
    fn action_results_update_the_screen_speak_and_run_follow_ups_once() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::ActionDone {
            output: news_output(),
            depth: 0,
        });
        assert!(fx
            .iter()
            .any(|e| matches!(e, Effect::Screen(v) if v.panels.len() == 1)));
        assert_eq!(spoken(&fx), vec!["Here is today's news."]);
        assert!(fx.contains(&Effect::Dispatch {
            action: Action::new("play_live_tv", json!({})),
            depth: 1
        }));
        assert_eq!(c.screen().card(1).unwrap().title, "Monsoon arrives");

        // A follow-up's own follow-ups are not chained further (no loops).
        let fx = c.step(Event::ActionDone {
            output: news_output(),
            depth: 1,
        });
        assert!(!fx.iter().any(|e| matches!(e, Effect::Dispatch { .. })));
    }

    #[test]
    fn after_the_reply_jarvis_keeps_listening_for_a_follow_up() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Utterance("news".into()));
        let fx = c.step(Event::BrainDone);
        assert_eq!(c.mode(), Mode::FollowUp);
        let id = timer_of(&fx, Timer::FollowUp).expect("follow-up timer");

        // Follow-up works without saying "Hey Jarvis" again.
        let fx = c.step(Event::Utterance("open number one".into()));
        assert!(think_of(&fx).is_some());

        // The earlier follow-up timer is now stale and must be ignored.
        c.step(Event::BrainDone);
        c.step(Event::Timeout {
            timer: Timer::FollowUp,
            id,
        });
        assert_eq!(c.mode(), Mode::FollowUp, "stale timer ignored");
    }

    #[test]
    fn follow_up_window_closes_to_idle() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Utterance("news".into()));
        let id = timer_of(&c.step(Event::BrainDone), Timer::FollowUp).unwrap();
        c.step(Event::Timeout {
            timer: Timer::FollowUp,
            id,
        });
        assert_eq!(c.mode(), Mode::Idle);
    }

    #[test]
    fn conversation_history_is_sent_with_the_next_request() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Utterance("bring me the news".into()));
        c.step(Event::BrainSpeech("Here you are.".into()));
        c.step(Event::BrainDone);
        let (_, _, history) = think_of(&c.step(Event::Utterance("open two".into()))).unwrap();
        assert_eq!(
            history,
            vec![
                Turn {
                    role: Role::User,
                    text: "bring me the news".into()
                },
                Turn {
                    role: Role::Assistant,
                    text: "Here you are.".into()
                },
            ]
        );
    }

    // ── failures ─────────────────────────────────────────────────

    #[test]
    fn brain_failure_apologises_and_returns_to_idle() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Utterance("news".into()));
        let fx = c.step(Event::BrainFailed);
        assert_eq!(
            spoken(&fx),
            vec!["Sorry, I can't reach my brain just now. Please try again in a moment."]
        );
        assert_eq!(c.mode(), Mode::Idle);
    }

    #[test]
    fn action_failure_is_spoken() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::ActionFailed(
            "There's no story number nine on screen.".into(),
        ));
        assert_eq!(spoken(&fx), vec!["There's no story number nine on screen."]);
    }

    #[test]
    fn empty_speech_is_retried_once_then_gives_up() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::Wake { hour: 9 });
        let fx = c.step(Event::Utterance("   ".into()));
        assert_eq!(spoken(&fx), vec!["Sorry, I didn't catch that."]);
        assert_eq!(c.mode(), Mode::Listening);
        let fx = c.step(Event::Utterance("".into()));
        assert!(spoken(&fx).is_empty());
        assert_eq!(c.mode(), Mode::Idle);
    }

    // ── local commands in the loop ───────────────────────────────

    #[test]
    fn clear_the_screen_is_handled_locally() {
        let mut c = Coordinator::new("Manj");
        c.step(Event::ActionDone {
            output: news_output(),
            depth: 1,
        });
        let fx = c.step(Event::Utterance("clear the screen".into()));
        assert!(think_of(&fx).is_none(), "no brain call");
        assert!(fx
            .iter()
            .any(|e| matches!(e, Effect::Screen(v) if v.panels.is_empty())));
        assert!(c.screen().is_empty());
    }

    #[test]
    fn stop_interrupts_speech_without_the_brain() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::Utterance("stop".into()));
        assert!(fx.contains(&Effect::StopSpeaking));
        assert!(think_of(&fx).is_none());
    }

    #[test]
    fn stop_listening_turns_the_mic_off_until_tapped() {
        let mut c = Coordinator::new("Manj");
        let fx = c.step(Event::Utterance("stop listening".into()));
        assert!(fx.contains(&Effect::Mic(false)));
        assert_eq!(c.mode(), Mode::MicOff);

        assert!(
            c.step(Event::Wake { hour: 9 }).is_empty(),
            "wake word ignored while mic is off"
        );
        assert!(c.step(Event::Utterance("news".into())).is_empty());

        let fx = c.step(Event::Tap { hour: 9 });
        assert!(fx.contains(&Effect::Mic(true)));
        assert_eq!(c.mode(), Mode::Listening);
    }
}
