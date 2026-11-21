//! The JARVIS window: wires the Rust parts together and relays events to the UI.

mod key;

use chrono::Timelike;
use jarvis_brain::ClaudeBrain;
use jarvis_core::action::Registry;
use jarvis_core::brain::Brain;
use jarvis_core::coordinator::Event;
use jarvis_core::runtime::{runtime, Handle, Ui, UiEvent};
use jarvis_modules::{HttpFetch, LiveTvModule, NewsModule, ScreenModule, SourcesConfig};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

const USER_NAME: &str = "Manj";
const NEWS_REFRESH: Duration = Duration::from_secs(5 * 60);

struct TauriUi(AppHandle);

impl Ui for TauriUi {
    fn emit(&self, ev: UiEvent) {
        let app = &self.0;
        let r = match ev {
            UiEvent::Screen(v) => app.emit("jarvis://screen", v),
            UiEvent::Speak(s) => app.emit("jarvis://speak", s),
            UiEvent::StopSpeaking => app.emit("jarvis://stop_speaking", ()),
            UiEvent::Heard(s) => app.emit("jarvis://heard", s),
            UiEvent::Status(m) => app.emit("jarvis://status", m),
            UiEvent::Mic(on) => app.emit("jarvis://mic", on),
            UiEvent::Quit => {
                // Let "Goodbye" play before the window closes.
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(Duration::from_millis(1600)).await;
                    app.exit(0);
                });
                Ok(())
            }
        };
        if let Err(e) = r {
            tracing::warn!(error = %e, "emit failed");
        }
    }
}

fn local_hour() -> u32 {
    chrono::Local::now().hour()
}

#[tauri::command]
fn tap(jarvis: State<'_, Handle>) {
    jarvis.send(Event::Tap { hour: local_hour() });
}

#[tauri::command]
fn say(text: String, jarvis: State<'_, Handle>) {
    // Typed stand-in for the microphone until Milestone B; length-capped.
    let text: String = text.chars().take(500).collect();
    jarvis.send(Event::Utterance(text));
}

/// Emergency exit (Esc key): closes immediately.
#[tauri::command]
fn quit(app: AppHandle) {
    app.exit(0);
}

/// Typing stays on until the microphone arrives (Milestone B).
#[tauri::command]
fn typed_input_enabled() -> bool {
    true
}

/// First run: the UI asks for the key once if none is stored.
#[tauri::command]
fn needs_key() -> bool {
    key::load().is_none()
}

/// Save the pasted key to Credential Manager, then restart so the brain picks it up.
#[tauri::command]
fn save_key(key: String, app: AppHandle) -> Result<(), String> {
    let k = key::clean_key(&key)?;
    key::save(&k)?;
    app.restart();
}

fn brain() -> Option<Arc<dyn Brain>> {
    let key = key::load()?;
    let model = std::env::var("JARVIS_MODEL").ok();
    Some(Arc::new(ClaudeBrain::new(key, model, USER_NAME)))
}

pub fn run() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    tauri::Builder::default()
        .setup(|app| {
            let cfg = SourcesConfig::default_config();
            let news = Arc::new(NewsModule::new(
                cfg.feeds.clone(),
                Arc::new(HttpFetch::new()),
            ));

            let mut registry = Registry::new();
            registry.register(Box::new(news.clone()));
            registry.register(Box::new(LiveTvModule::new(cfg.channels.clone())));
            registry.register(Box::new(ScreenModule));

            let brain = brain();
            if brain.is_none() {
                tracing::warn!("no Anthropic key stored yet; only local commands will work");
            }
            let ui = Arc::new(TauriUi(app.handle().clone()));
            let (handle, fut) = runtime(USER_NAME, brain, Arc::new(registry), ui);
            tauri::async_runtime::spawn(fut);
            app.manage(handle);

            // Keep headlines warm so "bring me the news" never waits on the network.
            tauri::async_runtime::spawn(async move {
                loop {
                    let failed = news.refresh().await;
                    if !failed.is_empty() {
                        tracing::warn!(?failed, "some feeds failed");
                    }
                    tokio::time::sleep(NEWS_REFRESH).await;
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            tap,
            say,
            quit,
            typed_input_enabled,
            needs_key,
            save_key
        ])
        .run(tauri::generate_context!())
        .expect("error while running JARVIS");
}
