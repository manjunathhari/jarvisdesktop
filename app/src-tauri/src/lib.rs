//! The JARVIS window: wires the Rust parts together and relays events to the UI.

mod announce;
mod approvals;
mod board;
mod key;
mod node_config;
mod server_mode;

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
    /// The "Hey Jarvis" spotter loaded (design 2026-11-25).
    wake: bool,
}

impl Listening {
    /// Point the mic and ears at what JARVIS is doing now. The mic is on while JARVIS waits for
    /// a command, and while it is idle with the wake word available (sound is checked on this
    /// PC only); off while thinking and when the mic was turned off ("stop listening").
    fn follow(&self, m: Mode) {
        let command = matches!(m, Mode::Listening | Mode::FollowUp);
        let wake = self.wake && m == Mode::Idle;
        self.mic.set_active(command || wake);
        self.ears.set_listening(command);
        self.ears.set_wake(wake);
    }
}
type EarsSlot = Arc<Mutex<Option<Listening>>>;
/// JARVIS's latest mode, for ears that become ready after it changed.
type ModeCell = Arc<Mutex<Mode>>;

struct TauriUi {
    app: AppHandle,
    voice: VoiceSlot,
    ears: EarsSlot,
    clock: server_mode::TurnClock,
    announcer: Arc<announce::Announcer>,
    mode: ModeCell,
}

impl Ui for TauriUi {
    fn emit(&self, ev: UiEvent) {
        let app = &self.app;
        let r = match ev {
            UiEvent::Screen(v) => app.emit("jarvis://screen", v),
            UiEvent::Speak(s) => {
                board::spoke(&s);
                self.clock.speaking();
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
            UiEvent::Heard(s) => {
                board::heard(&s);
                self.clock.heard();
                app.emit("jarvis://heard", s)
            }
            UiEvent::Status(m) => {
                if m == Mode::Idle {
                    self.clock.cancel();
                }
                if let Ok(mut cell) = self.mode.lock() {
                    *cell = m;
                }
                if let Some(l) = self.ears.lock().ok().as_deref().and_then(Option::as_ref) {
                    l.follow(m);
                }
                let r = app.emit("jarvis://status", m);
                // Results Jarvis reported on its own, held until JARVIS was free.
                for line in self.announcer.mode(m) {
                    speak_locally(app, &self.voice, &line);
                }
                r
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
fn say(
    text: String,
    jarvis: State<'_, Handle>,
    node: State<'_, approvals::NodeSlot>,
    voice: State<'_, VoiceSlot>,
    app: AppHandle,
) {
    say_text(text, true, &jarvis, &node, &voice, &app);
}

/// The Board's "Type instead of speaking" box. Its own command, so that the Board window can
/// be allowed this and not the main window's `say`, and with two differences:
/// - it never decides an approval (the Board shows untrusted content);
/// - it says why when the line would be dropped (the coordinator takes nothing while it is
///   thinking or the microphone is off), so the text stays in the box.
#[tauri::command]
fn board_say(
    text: String,
    jarvis: State<'_, Handle>,
    node: State<'_, approvals::NodeSlot>,
    voice: State<'_, VoiceSlot>,
    mode: State<'_, ModeCell>,
    app: AppHandle,
) -> Result<(), String> {
    match mode.lock().map(|m| *m) {
        Ok(Mode::Thinking) => return Err("Jarvis is busy. Try again in a moment.".to_owned()),
        Ok(Mode::MicOff) => {
            return Err("Jarvis is not listening. Say or tap to wake it first.".to_owned())
        }
        _ => {}
    }
    say_text(text, false, &jarvis, &node, &voice, &app);
    Ok(())
}

/// A typed line, from either window. `may_decide`: whether an approval phrase may decide.
fn say_text(
    text: String,
    may_decide: bool,
    jarvis: &Handle,
    node: &approvals::NodeSlot,
    voice: &VoiceSlot,
    app: &AppHandle,
) {
    // Typed stand-in for the microphone until Milestone B; length-capped.
    let text: String = text.chars().take(500).collect();
    // For the Board's conversation: this line, when it is heard, was typed.
    board::typed(&text);
    let (app, voice) = (app.clone(), voice.clone());
    if approvals::take_spoken_decision(node, &text, may_decide, move |line| {
        speak_locally(&app, &voice, line)
    }) {
        // Taken here, so the coordinator never reports it heard: record it ourselves.
        board::heard(&text);
        return;
    }
    jarvis.send(Event::Utterance(text));
}

/// Say a line in George's voice (or the stand-in), outside the coordinator.
fn speak_locally(app: &AppHandle, voice: &VoiceSlot, text: &str) {
    board::spoke(text);
    let _ = app.emit("jarvis://caption", text);
    match voice.lock().ok().as_deref().and_then(Option::as_ref) {
        Some(v) => v.speak(text),
        None => {
            let _ = app.emit("jarvis://speak", text);
        }
    }
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

/// First run: the UI asks for the key once if none is stored (never in server mode, where
/// the key isn't read at all).
#[tauri::command]
fn needs_key(mode: State<'_, server_mode::Mode>) -> bool {
    !mode.server() && mode.error.is_none() && key::load().is_none()
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
    node: approvals::NodeSlot,
    mode: ModeCell,
) {
    // For a spoken "approve" / "deny" answered here, not by the coordinator (Task 7).
    let (decision_app, decision_voice) = (app.clone(), voice.clone());
    use jarvis_ears::models::*;
    use jarvis_ears::sherpa::{KeywordWake, SileroVad, Whisper};
    use jarvis_ears::{capture::start_microphone, Ears, WakeDetector};
    use jarvis_voice::model::{ensure_archive, ensure_files, HttpDownload, ModelFile};

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
    // "Hey Jarvis": optional. Without it, tapping and typing work as before.
    let wake_dir = ensure_archive(
        &models_dir,
        KWS_DIR,
        KWS_URL,
        KWS_ARCHIVE_DIR,
        KWS_FILES,
        &dl,
        &|_| {},
    )
    .await;
    let wake_detector = match wake_dir {
        Ok(dir) => match tokio::task::spawn_blocking(move || KeywordWake::load(&dir)).await {
            Ok(Ok(w)) => Some(Box::new(w) as Box<dyn WakeDetector>),
            Ok(Err(e)) => {
                tracing::warn!(error = %e, "couldn't load the wake word; tap or type instead");
                None
            }
            Err(e) => {
                tracing::warn!(error = %e, "wake-word loader crashed");
                None
            }
        },
        Err(e) => {
            tracing::warn!(error = %e, "wake-word model unavailable; tap or type instead");
            None
        }
    };
    let has_wake = wake_detector.is_some();
    let (wake_app, wake_jarvis) = (app.clone(), jarvis.clone());
    let wake = wake_detector.map(|w| {
        let fire: Box<dyn FnMut() + Send> = Box::new(move || {
            bring_to_front(&wake_app);
            wake_jarvis.send(Event::Wake { hour: local_hour() });
        });
        (w, fire)
    });
    let ears = Ears::start_with_wake(
        Box::new(vad),
        Box::new(whisper),
        speaking,
        Box::new(move |text| {
            tracing::info!(%text, "heard");
            let (app, voice) = (decision_app.clone(), decision_voice.clone());
            if approvals::take_spoken_decision(&node, &text, true, move |line| {
                speak_locally(&app, &voice, line)
            }) {
                // Taken here, so the coordinator never reports it heard.
                board::heard(&text);
                return;
            }
            jarvis.send(Event::Utterance(text));
        }),
        wake,
    );
    let feed = ears.clone();
    match start_microphone(move |chunk| feed.push_audio(chunk)) {
        Ok(mic) => {
            let l = Listening {
                ears,
                mic,
                wake: has_wake,
            };
            // Catch up with whatever JARVIS is doing now (idle: start listening for the phrase).
            l.follow(mode.lock().map(|m| *m).unwrap_or(Mode::Idle));
            if let Ok(mut s) = slot.lock() {
                *s = Some(l);
            }
            tracing::info!(wake_word = has_wake, "listening is ready");
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
    _node: approvals::NodeSlot,
    _mode: ModeCell,
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

/// The installer's build number, set by the Windows installer workflow (`JARVIS_BUILD`).
#[tauri::command]
fn build_label() -> String {
    match option_env!("JARVIS_BUILD") {
        Some(n) if !n.is_empty() => format!("build {n}"),
        _ => "dev build".into(),
    }
}

/// Show, restore and focus the main window.
fn bring_to_front(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

pub fn run() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    tauri::Builder::default()
        // One JARVIS at a time (live check, 25 Nov): a second copy can't open the node's
        // journal, so it had no brain and blamed a missing key. Starting JARVIS again now
        // brings the running window to the front instead. Registered first, as the plugin
        // requires.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            bring_to_front(app);
        }))
        // Only for `board_open_link` (Rust side). No window is granted the plugin's own
        // commands, and its script that takes over link clicks in every page is left out.
        .plugin(
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
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

            // Which brain: node.toml in the data folder (none: today's Claude brain).
            let node: approvals::NodeSlot = Arc::default();
            let data_dir = app.path().app_local_data_dir().ok();
            let config = match &data_dir {
                Some(dir) => node_config::load(dir),
                None => Ok(node_config::NodeConfig::default()),
            };
            let (brain, mode) = match config {
                Ok(c) if c.brain == node_config::BrainChoice::Server => {
                    let dir = data_dir.clone().expect("a config was read from it");
                    match server_mode::start(app.handle(), &c, &dir, &node) {
                        Ok(b) => {
                            tracing::info!("brain: the Jarvis server");
                            (
                                Some(b),
                                server_mode::Mode {
                                    brain: c.brain,
                                    error: None,
                                },
                            )
                        }
                        Err(e) => {
                            tracing::error!(error = %e, "server mode couldn't start");
                            (
                                None,
                                server_mode::Mode {
                                    brain: c.brain,
                                    error: Some(e),
                                },
                            )
                        }
                    }
                }
                Ok(c) => {
                    let b = brain();
                    if b.is_none() {
                        tracing::warn!(
                            "no Anthropic key stored yet; only local commands will work"
                        );
                    }
                    (
                        b,
                        server_mode::Mode {
                            brain: c.brain,
                            error: None,
                        },
                    )
                }
                Err(e) => {
                    // A wrong node.toml is shown, never silently replaced by another brain.
                    tracing::error!(error = %e, "node.toml");
                    let mode = server_mode::Mode {
                        brain: node_config::BrainChoice::Claude,
                        error: Some(e),
                    };
                    (None, mode)
                }
            };
            app.manage(mode);
            let voice: VoiceSlot = Arc::new(Mutex::new(None));
            let ears: EarsSlot = Arc::new(Mutex::new(None));
            let mode: ModeCell = Arc::new(Mutex::new(Mode::Idle));
            app.manage(mode.clone());
            let announcer = Arc::new(announce::Announcer::default());
            if let Some(n) = node.lock().ok().and_then(|n| n.clone()) {
                let (app, voice) = (app.handle().clone(), voice.clone());
                tauri::async_runtime::spawn(announce::run(
                    n.subscribe_replies(),
                    announcer.clone(),
                    move |line| speak_locally(&app, &voice, line),
                ));
            }
            let ui = Arc::new(TauriUi {
                app: app.handle().clone(),
                voice: voice.clone(),
                ears: ears.clone(),
                clock: server_mode::TurnClock::default(),
                announcer: announcer.clone(),
                mode: mode.clone(),
            });
            let (handle, fut) = runtime(USER_NAME, brain, Arc::new(registry), ui);
            board::create(app.handle());
            // The Board (slice 1) is the server's: it opens only when the node runs, and by
            // itself only on a monitor of its own.
            let running = node.lock().ok().and_then(|n| n.clone());
            if let Some(running) = running {
                board::start_recorder(app.handle(), &running);
                board::open_at_start(app.handle());
            }
            app.manage(node.clone());
            app.manage(voice.clone());
            tauri::async_runtime::spawn(fut);
            match app.path().app_local_data_dir() {
                Ok(dir) => {
                    let (app_handle, jarvis, node) =
                        (app.handle().clone(), handle.clone(), node.clone());
                    let models = dir.join("models");
                    tauri::async_runtime::spawn(async move {
                        // Voice first (George), then hearing; one download at a time.
                        prepare_voice(app_handle.clone(), voice.clone(), models.clone()).await;
                        prepare_ears(app_handle, ears, voice, models, jarvis, node, mode).await;
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
            save_key,
            approvals::decide_approval,
            server_mode::node_status,
            server_mode::enrol,
            server_mode::forget_claude_key,
            build_label,
            board::board_snapshot,
            board::board_set_state,
            board::board_rendered,
            board::board_clear_closed,
            board::board_open,
            board::board_open_link,
            board::board_canvas_address,
            board::board_canvas_served,
            board_say
        ])
        .on_window_event(board::on_window_event)
        .run(tauri::generate_context!())
        .expect("error while running JARVIS");
}
