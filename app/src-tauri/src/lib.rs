//! The JARVIS window: wires the Rust parts together and relays events to the UI.

mod key;

use chrono::Timelike;
use jarvis_brain::ClaudeBrain;
use jarvis_core::action::Registry;
use jarvis_core::brain::Brain;
use jarvis_core::coordinator::{Event, Mode};
use jarvis_core::runtime::{runtime, Handle, Ui, UiEvent};
use jarvis_modules::{HttpFetch, LiveTvModule, NewsModule, ScreenModule, SourcesConfig};
use jarvis_voice::Voice;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

const USER_NAME: &str = "Manj";
const NEWS_REFRESH: Duration = Duration::from_secs(5 * 60);

/// George's voice once it is downloaded and loaded; until then the UI's
/// stand-in voice speaks.
type VoiceSlot = Arc<Mutex<Option<Voice>>>;

/// The microphone and the ears, once their models are ready.
struct Listening {
    ears: jarvis_ears::Ears,
    mic: jarvis_ears::capture::Mic,
}
type EarsSlot = Arc<Mutex<Option<Listening>>>;

struct TauriUi {
    app: AppHandle,
    voice: VoiceSlot,
    ears: EarsSlot,
}

impl Ui for TauriUi {
    fn emit(&self, ev: UiEvent) {
        let app = &self.app;
        let r = match ev {
            UiEvent::Screen(v) => app.emit("jarvis://screen", v),
            UiEvent::Speak(s) => {
                let _ = app.emit("jarvis://caption", &s);
                match self.voice.lock().ok().as_deref().and_then(Option::as_ref) {
                    Some(v) => {
                        v.speak(&s);
                        Ok(())
                    }
                    None => app.emit("jarvis://speak", s),
                }
            }
            UiEvent::StopSpeaking => {
                if let Some(v) = self.voice.lock().ok().as_deref().and_then(Option::as_ref) {
                    v.stop();
                }
                app.emit("jarvis://stop_speaking", ())
            }
            UiEvent::Heard(s) => app.emit("jarvis://heard", s),
            UiEvent::Status(m) => {
                // The mic is on only while JARVIS is waiting for you to speak.
                let on = matches!(m, Mode::Listening | Mode::FollowUp);
                if let Some(l) = self.ears.lock().ok().as_deref().and_then(Option::as_ref) {
                    l.mic.set_active(on);
                    l.ears.set_listening(on);
                }
                app.emit("jarvis://status", m)
            }
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

/// The live player said this channel won't play here; try the next one.
#[tauri::command]
fn video_failed(jarvis: State<'_, Handle>) {
    jarvis.send(Event::VideoFailed);
}

/// F11: switch between JARVIS's window and full screen.
#[tauri::command]
fn toggle_fullscreen(window: tauri::WebviewWindow) -> Result<(), String> {
    let full = window.is_fullscreen().map_err(|e| e.to_string())?;
    window.set_fullscreen(!full).map_err(|e| e.to_string())
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

#[derive(Clone, Serialize)]
struct VoiceStatus {
    state: &'static str,
    percent: u8,
}

/// Download George's voice the first time, load it, and switch to it.
#[cfg(windows)]
async fn prepare_voice(app: AppHandle, slot: VoiceSlot, models_dir: PathBuf) {
    use jarvis_voice::model::{self, HttpDownload, GEORGE, KOKORO_URL};
    use jarvis_voice::{kokoro::Kokoro, speaker::Speaker, AudioOut};

    let status = |state: &'static str, percent: u8| {
        let _ = app.emit("jarvis://voice_status", VoiceStatus { state, percent });
    };
    let last = std::sync::atomic::AtomicU8::new(255);
    let files = match model::ensure(&models_dir, KOKORO_URL, &HttpDownload::default(), &|p| {
        if last.swap(p, std::sync::atomic::Ordering::Relaxed) != p {
            status("downloading", p);
        }
    })
    .await
    {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!(error = %e, "voice download failed; using the stand-in voice");
            status("unavailable", 0);
            return;
        }
    };
    status("loading", 100);
    let loaded = tokio::task::spawn_blocking(move || Kokoro::load(&files, GEORGE)).await;
    match loaded {
        Ok(Ok(engine)) => {
            let voice = Voice::start(
                Box::new(engine),
                Box::new(|| Speaker::open().map(|s| Box::new(s) as Box<dyn AudioOut>)),
            );
            if let Ok(mut s) = slot.lock() {
                *s = Some(voice);
            }
            tracing::info!("George's voice is ready");
            status("ready", 100);
        }
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "couldn't load the voice; using the stand-in voice");
            status("unavailable", 0);
        }
        Err(e) => {
            tracing::warn!(error = %e, "voice loader crashed; using the stand-in voice");
            status("unavailable", 0);
        }
    }
}

/// Download the listening models the first time, load them, open the mic.
#[cfg(windows)]
async fn prepare_ears(
    app: AppHandle,
    slot: EarsSlot,
    voice: VoiceSlot,
    models_dir: PathBuf,
    jarvis: Handle,
) {
    use jarvis_ears::models::*;
    use jarvis_ears::sherpa::{SileroVad, Whisper};
    use jarvis_ears::{capture::start_microphone, Ears};
    use jarvis_voice::model::{ensure_files, HttpDownload, ModelFile};

    let status = |state: &'static str, percent: u8| {
        let _ = app.emit("jarvis://ears_status", VoiceStatus { state, percent });
    };
    let dl = HttpDownload::default();
    let whisper_files = [
        ModelFile {
            url: WHISPER_ENCODER_URL,
            name: WHISPER_ENCODER,
        },
        ModelFile {
            url: WHISPER_DECODER_URL,
            name: WHISPER_DECODER,
        },
        ModelFile {
            url: WHISPER_TOKENS_URL,
            name: WHISPER_TOKENS,
        },
    ];
    let progress = |p: u8| status("downloading", p);
    let dirs = async {
        let w = ensure_files(&models_dir, WHISPER_DIR, &whisper_files, &dl, &progress).await?;
        let v = ensure_files(
            &models_dir,
            VAD_DIR,
            &[ModelFile {
                url: VAD_URL,
                name: VAD_FILE,
            }],
            &dl,
            &|_| {},
        )
        .await?;
        Ok::<_, String>((w, v))
    };
    let (whisper_dir, vad_dir) = match dirs.await {
        Ok(d) => d,
        Err(e) => {
            tracing::warn!(error = %e, "listening models unavailable; typing still works");
            status("unavailable", 0);
            return;
        }
    };
    status("loading", 100);
    let loaded = tokio::task::spawn_blocking(move || {
        Ok::<_, String>((
            SileroVad::load(&vad_dir.join(VAD_FILE))?,
            Whisper::load(&whisper_dir)?,
        ))
    })
    .await;
    let (vad, whisper) = match loaded {
        Ok(Ok(m)) => m,
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "couldn't load the listening models");
            status("unavailable", 0);
            return;
        }
        Err(e) => {
            tracing::warn!(error = %e, "listening loader crashed");
            status("unavailable", 0);
            return;
        }
    };
    // Never listen to George.
    let speaking = Arc::new(move || {
        voice
            .lock()
            .ok()
            .and_then(|v| v.as_ref().map(|v| v.is_speaking()))
            .unwrap_or(false)
    });
    let ears = Ears::start(
        Box::new(vad),
        Box::new(whisper),
        speaking,
        Box::new(move |text| {
            tracing::info!(%text, "heard");
            jarvis.send(Event::Utterance(text));
        }),
    );
    let feed = ears.clone();
    match start_microphone(move |chunk| feed.push_audio(chunk)) {
        Ok(mic) => {
            if let Ok(mut s) = slot.lock() {
                *s = Some(Listening { ears, mic });
            }
            tracing::info!("listening is ready");
            status("ready", 100);
        }
        Err(e) => {
            tracing::warn!(error = %e, "no microphone; typing still works");
            status("no_mic", 0);
        }
    }
}

#[cfg(not(windows))]
async fn prepare_ears(
    _app: AppHandle,
    _slot: EarsSlot,
    _voice: VoiceSlot,
    _models_dir: PathBuf,
    _jarvis: Handle,
) {
    let _: Option<Listening> = None;
}

/// Other platforms keep the stand-in voice for now.
#[cfg(not(windows))]
async fn prepare_voice(_app: AppHandle, _slot: VoiceSlot, _models_dir: PathBuf) {
    let _ = VoiceStatus {
        state: "unavailable",
        percent: 0,
    };
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
            let voice: VoiceSlot = Arc::new(Mutex::new(None));
            let ears: EarsSlot = Arc::new(Mutex::new(None));
            let ui = Arc::new(TauriUi {
                app: app.handle().clone(),
                voice: voice.clone(),
                ears: ears.clone(),
            });
            let (handle, fut) = runtime(USER_NAME, brain, Arc::new(registry), ui);
            tauri::async_runtime::spawn(fut);
            match app.path().app_local_data_dir() {
                Ok(dir) => {
                    let (app_handle, jarvis) = (app.handle().clone(), handle.clone());
                    let models = dir.join("models");
                    tauri::async_runtime::spawn(async move {
                        // Voice first (George), then hearing; one download at a time.
                        prepare_voice(app_handle.clone(), voice.clone(), models.clone()).await;
                        prepare_ears(app_handle, ears, voice, models, jarvis).await;
                    });
                }
                Err(e) => tracing::warn!(error = %e, "no data folder; using the stand-in voice"),
            }
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
            video_failed,
            toggle_fullscreen,
            typed_input_enabled,
            needs_key,
            save_key
        ])
        .run(tauri::generate_context!())
        .expect("error while running JARVIS");
}
