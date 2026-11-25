//! Carries out the coordinator's effects: calls the brain, runs actions,
//! starts timers, and tells the UI what to show and say.

use crate::action::{ActionError, Registry};
use crate::brain::{Brain, BrainEvent, BrainRequest};
use crate::coordinator::{Coordinator, Effect, Event, Mode};

use crate::screen::ScreenView;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

pub const BRAIN_TIMEOUT: Duration = Duration::from_secs(8);
pub const NO_KEY_REPLY: &str =
    "I don't have my Anthropic key yet, so for now I can only do simple things, \
                                like clearing the screen.";

/// Everything the UI needs to hear about.
#[derive(Debug, Clone, PartialEq)]
pub enum UiEvent {
    Screen(ScreenView),
    Speak(String),
    StopSpeaking,
    Heard(String),
    Status(Mode),
    Mic(bool),
    /// Close the app (after the goodbye has had a moment to play).
    Quit,
}

pub trait Ui: Send + Sync + 'static {
    fn emit(&self, ev: UiEvent);
}

#[derive(Clone)]
pub struct Handle {
    tx: mpsc::UnboundedSender<Event>,
}

impl Handle {
    pub fn send(&self, ev: Event) {
        let _ = self.tx.send(ev);
    }
}

/// Build the runtime. Spawn the returned future on a tokio runtime; use the
/// handle to feed it taps, wake words and utterances.
pub fn runtime(
    user_name: &str,
    brain: Option<Arc<dyn Brain>>,
    registry: Arc<Registry>,
    ui: Arc<dyn Ui>,
) -> (Handle, impl Future<Output = ()> + Send + 'static) {
    let (tx, rx) = mpsc::unbounded_channel::<Event>();
    let handle = Handle { tx: tx.clone() };
    let coordinator = Coordinator::new(user_name);
    let fut = run(rx, tx, coordinator, brain, registry, ui);
    (handle, fut)
}

async fn run(
    mut rx: mpsc::UnboundedReceiver<Event>,
    tx: mpsc::UnboundedSender<Event>,
    mut coordinator: Coordinator,
    brain: Option<Arc<dyn Brain>>,
    registry: Arc<Registry>,
    ui: Arc<dyn Ui>,
) {
    while let Some(event) = rx.recv().await {
        for effect in coordinator.step(event) {
            match effect {
                Effect::Speak(s) => ui.emit(UiEvent::Speak(s)),
                Effect::StopSpeaking => ui.emit(UiEvent::StopSpeaking),
                Effect::Heard(s) => ui.emit(UiEvent::Heard(s)),
                Effect::Status(m) => ui.emit(UiEvent::Status(m)),
                Effect::Mic(on) => ui.emit(UiEvent::Mic(on)),
                Effect::Screen(v) => ui.emit(UiEvent::Screen(v)),
                Effect::Quit => ui.emit(UiEvent::Quit),
                Effect::SetTimer { timer, id, after } => {
                    let tx = tx.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(after).await;
                        let _ = tx.send(Event::Timeout { timer, id });
                    });
                }
                Effect::Dispatch { action, depth } => {
                    let (tx, registry) = (tx.clone(), registry.clone());
                    let screen = coordinator.screen().clone();
                    tokio::spawn(async move {
                        let ev = match registry.dispatch(&action, &screen).await {
                            Ok(output) => Event::ActionDone { output, depth },
                            Err(e) => Event::ActionFailed(plain_failure(e)),
                        };
                        let _ = tx.send(ev);
                    });
                }
                Effect::Think {
                    utterance,
                    screen_summary,
                    history,
                } => {
                    let tx = tx.clone();
                    let Some(brain) = brain.clone() else {
                        let _ = tx.send(Event::BrainSpeech(NO_KEY_REPLY.into()));
                        let _ = tx.send(Event::BrainDone);
                        continue;
                    };
                    let req = BrainRequest {
                        utterance,
                        screen_summary,
                        history,
                        tools: registry.specs(),
                    };
                    tokio::spawn(async move {
                        let (btx, mut brx) = mpsc::channel::<BrainEvent>(32);
                        let forward = async {
                            let think = brain.think(req, btx);
                            let relay = async {
                                let started = std::time::Instant::now();
                                let mut first = true;
                                while let Some(ev) = brx.recv().await {
                                    if std::mem::take(&mut first) {
                                        tracing::info!(
                                            brain_first_reply_ms =
                                                started.elapsed().as_millis() as u64,
                                            "brain"
                                        );
                                    }
                                    let _ = tx.send(match ev {
                                        BrainEvent::Speech(s) => Event::BrainSpeech(s),
                                        BrainEvent::Action(a) => Event::BrainAction(a),
                                    });
                                }
                            };
                            let (result, ()) = tokio::join!(think, relay);
                            result
                        };
                        let done = match tokio::time::timeout(BRAIN_TIMEOUT, forward).await {
                            Ok(Ok(())) => Event::BrainDone,
                            Ok(Err(e)) => {
                                tracing::warn!(error = %e, "brain failed");
                                Event::BrainFailed
                            }
                            Err(_) => {
                                tracing::warn!("brain timed out");
                                Event::BrainFailed
                            }
                        };
                        let _ = tx.send(done);
                    });
                }
            }
        }
    }
}

/// What the user hears when an action can't be carried out.
fn plain_failure(e: ActionError) -> String {
    match e {
        ActionError::Failed(msg) => msg,
        other => {
            tracing::warn!(error = %other, "refused action");
            "Sorry, I can't do that yet.".into()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::action::{Action, ActionSpec, Module, ModuleOutput};
    use crate::brain::BrainError;
    use crate::screen::{Card, Panel, PanelContent, PanelUpdate, ScreenState, Slot};
    use async_trait::async_trait;
    use serde_json::json;
    use std::sync::Mutex;

    #[derive(Default)]
    struct Recorder(Mutex<Vec<UiEvent>>);
    impl Ui for Recorder {
        fn emit(&self, ev: UiEvent) {
            self.0.lock().unwrap().push(ev);
        }
    }
    impl Recorder {
        fn spoken(&self) -> Vec<String> {
            self.0
                .lock()
                .unwrap()
                .iter()
                .filter_map(|e| {
                    if let UiEvent::Speak(s) = e {
                        Some(s.clone())
                    } else {
                        None
                    }
                })
                .collect()
        }
        fn last_screen(&self) -> Option<ScreenView> {
            self.0.lock().unwrap().iter().rev().find_map(|e| match e {
                UiEvent::Screen(v) => Some(v.clone()),
                _ => None,
            })
        }
        fn last_status(&self) -> Option<Mode> {
            self.0.lock().unwrap().iter().rev().find_map(|e| match e {
                UiEvent::Status(m) => Some(*m),
                _ => None,
            })
        }
    }

    /// Replies with fixed events, optionally after a delay.
    struct ScriptBrain {
        events: Vec<BrainEvent>,
        delay: Duration,
        fail: bool,
    }
    #[async_trait]
    impl Brain for ScriptBrain {
        async fn think(
            &self,
            _: BrainRequest,
            tx: mpsc::Sender<BrainEvent>,
        ) -> Result<(), BrainError> {
            tokio::time::sleep(self.delay).await;
            if self.fail {
                return Err(BrainError::Unavailable("down".into()));
            }
            for e in &self.events {
                tx.send(e.clone()).await.ok();
            }
            Ok(())
        }
    }

    struct News;
    #[async_trait]
    impl Module for News {
        fn actions(&self) -> Vec<ActionSpec> {
            vec![ActionSpec {
                name: "show_news".into(),
                description: "d".into(),
                input_schema: json!({"type":"object","properties":{}}),
            }]
        }
        async fn handle(&self, _: &Action, _: &ScreenState) -> Result<ModuleOutput, ActionError> {
            Ok(ModuleOutput {
                updates: vec![PanelUpdate::Show(Panel {
                    slot: Slot::Secondary,
                    content: PanelContent::CardList {
                        title: "India".into(),
                        cards: vec![Card {
                            title: "Monsoon".into(),
                            source: "The Hindu".into(),
                            summary: "s".into(),
                            link: "l".into(),
                            published: None,
                        }],
                    },
                })],
                speech: Some("Here's the latest.".into()),
                follow_up: vec![],
            })
        }
    }

    fn start(brain: Option<Arc<dyn Brain>>) -> (Handle, Arc<Recorder>) {
        let mut reg = Registry::new();
        reg.register(Box::new(News));
        let ui = Arc::new(Recorder::default());
        let (h, fut) = runtime("Manj", brain, Arc::new(reg), ui.clone());
        tokio::spawn(fut);
        (h, ui)
    }

    async fn settle() {
        for _ in 0..50 {
            tokio::task::yield_now().await;
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_request_flows_from_brain_to_module_to_screen() {
        let brain = ScriptBrain {
            events: vec![BrainEvent::Action(Action::new("show_news", json!({})))],
            delay: Duration::from_millis(300),
            fail: false,
        };
        let (h, ui) = start(Some(Arc::new(brain)));
        h.send(Event::Wake { hour: 9 });
        h.send(Event::Utterance("bring me the news".into()));
        tokio::time::sleep(Duration::from_secs(1)).await;
        settle().await;

        let screen = ui.last_screen().expect("screen update");
        assert_eq!(screen.panels.len(), 1);
        assert_eq!(
            ui.spoken(),
            vec!["Good morning, Manj. How can I help?", "Here's the latest."]
        );
        assert_eq!(ui.last_status(), Some(Mode::FollowUp));
    }

    #[tokio::test(start_paused = true)]
    async fn follow_up_window_times_out_to_idle() {
        let brain = ScriptBrain {
            events: vec![],
            delay: Duration::ZERO,
            fail: false,
        };
        let (h, ui) = start(Some(Arc::new(brain)));
        h.send(Event::Utterance("hello".into()));
        // Still listening just before the window ends, idle just after (30 s, owner, 25 Nov).
        tokio::time::sleep(crate::coordinator::FOLLOW_UP_WINDOW - Duration::from_secs(1)).await;
        settle().await;
        assert_eq!(ui.last_status(), Some(Mode::FollowUp));
        tokio::time::sleep(Duration::from_secs(2)).await;
        settle().await;
        assert_eq!(ui.last_status(), Some(Mode::Idle));
    }

    #[tokio::test(start_paused = true)]
    async fn a_slow_brain_times_out_with_an_apology() {
        let brain = ScriptBrain {
            events: vec![],
            delay: Duration::from_secs(30),
            fail: false,
        };
        let (h, ui) = start(Some(Arc::new(brain)));
        h.send(Event::Utterance("news".into()));
        tokio::time::sleep(Duration::from_secs(9)).await;
        settle().await;
        assert!(
            ui.spoken()
                .iter()
                .any(|s| s.starts_with("Sorry, I can't reach my brain")),
            "{:?}",
            ui.spoken()
        );
        assert_eq!(ui.last_status(), Some(Mode::Idle));
    }

    #[tokio::test(start_paused = true)]
    async fn a_failing_brain_apologises() {
        let brain = ScriptBrain {
            events: vec![],
            delay: Duration::ZERO,
            fail: true,
        };
        let (h, ui) = start(Some(Arc::new(brain)));
        h.send(Event::Utterance("news".into()));
        settle().await;
        assert!(ui
            .spoken()
            .iter()
            .any(|s| s.starts_with("Sorry, I can't reach my brain")));
    }

    #[tokio::test(start_paused = true)]
    async fn without_a_key_jarvis_says_so_but_local_commands_still_work() {
        let (h, ui) = start(None);
        h.send(Event::Utterance("news".into()));
        settle().await;
        assert_eq!(ui.spoken(), vec![NO_KEY_REPLY.to_string()]);
        h.send(Event::Utterance("clear the screen".into()));
        settle().await;
        assert_eq!(ui.last_screen(), Some(ScreenView::default()));
    }

    #[tokio::test(start_paused = true)]
    async fn shut_down_reaches_the_ui() {
        let (h, ui) = start(None);
        h.send(Event::Utterance("Jarvis, shut down".into()));
        settle().await;
        assert!(ui.0.lock().unwrap().contains(&UiEvent::Quit));
    }

    #[tokio::test(start_paused = true)]
    async fn an_unknown_action_from_the_brain_is_refused_politely() {
        let brain = ScriptBrain {
            events: vec![BrainEvent::Action(Action::new("format_disk", json!({})))],
            delay: Duration::ZERO,
            fail: false,
        };
        let (h, ui) = start(Some(Arc::new(brain)));
        h.send(Event::Utterance("do something odd".into()));
        settle().await;
        assert!(
            ui.spoken()
                .contains(&"Sorry, I can't do that yet.".to_string()),
            "{:?}",
            ui.spoken()
        );
        assert!(ui.last_screen().is_none());
    }
}
