//! The JARVIS window: wires the Rust parts together and relays events to the UI.

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

#[tauri::command]
fn typed_input_enabled() -> bool {
    cfg!(debug_assertions) || std::env::var_os("JARVIS_TYPED_INPUT").is_some()
}

/// Milestone A reads the key from the environment. T13 moves it to
/// Windows Credential Manager.
fn brain() -> Option<Arc<dyn Brain>> {
    let key = std::env::var("ANTHROPIC_API_KEY")
        .ok()
        .filter(|k| !k.trim().is_empty())?;
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
                tracing::warn!("ANTHROPIC_API_KEY not set; only local commands will work");
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
        .invoke_handler(tauri::generate_handler![tap, say, typed_input_enabled])
        .run(tauri::generate_context!())
        .expect("error while running JARVIS");
}
