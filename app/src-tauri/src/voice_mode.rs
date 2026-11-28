//! The app's side of the voice supervisor (Windows): how the voice helper and the built-in
//! voice are started and stopped ([`VoiceOps`]), and the set-up glue for `setup`.
//!
//! Every helper the app spawns gets a generation number. A helper's late `Closed` reaches the
//! supervisor only while that helper is the current one and had been handed over as the
//! voice, so a helper that was replaced or stopped can never trigger a restart.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, Weak};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use jarvis_core::Speaks;
use jarvis_helper::install::{self, HelperFiles};
use jarvis_helper::process::{self, Running};
use jarvis_helper::supervisor::{Status, StatusFn, Supervisor, VoiceOps, VoiceSlot};
use jarvis_helper::voice_config::{VoiceConfig, VoiceEngine};
use jarvis_helper::{HelperSettings, LinkEvent};
use jarvis_voice::model::HttpDownload;
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;

use crate::server_mode::TurnClock;
use crate::VoiceStatus;

/// How long a started helper has to say `ready`.
const READY_WAIT: Duration = Duration::from_secs(60);
/// The helper's Kokoro voice: George.
const HELPER_VOICE: &str = "bm_george";

/// Build the supervisor for `setup`, without starting it: the caller awaits
/// `supervisor.start(engine)` with the engine returned here.
pub fn start(
    app: &AppHandle,
    slot: VoiceSlot,
    data_dir: &Path,
    clock: TurnClock,
) -> (Arc<Supervisor>, VoiceEngine) {
    let (config, warnings) = VoiceConfig::load(data_dir);
    for w in &warnings {
        tracing::warn!("{w}");
    }
    let shared = Arc::new(Shared {
        next: AtomicU64::new(0),
        current: AtomicU64::new(0),
        running: Mutex::new(None),
        supervisor: OnceLock::new(),
        clock,
    });
    let ops = Arc::new(AppVoiceOps {
        shared: shared.clone(),
        models_dir: data_dir.join("models"),
        tts_speed: config.tts_speed,
    });
    let app = app.clone();
    let set_status: StatusFn = Arc::new(move |s: Status| {
        let _ = app.emit(
            "jarvis://voice_status",
            VoiceStatus {
                state: s.state(),
                percent: s.percent(),
            },
        );
    });
    let supervisor = Supervisor::new(ops, set_status, slot);
    let _ = shared.supervisor.set(Arc::downgrade(&supervisor));
    (supervisor, config.voice_engine)
}

/// Take a std lock, treating a poisoned one as usable.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

/// What the ops and every helper's event callback share.
struct Shared {
    /// The last generation handed out (the first helper is 1).
    next: AtomicU64,
    /// The generation whose events still count; 0 when none does.
    current: AtomicU64,
    /// The helper that is the voice, with its generation.
    running: Mutex<Option<(u64, Running)>>,
    /// Set right after the supervisor is made. Weak: the supervisor owns the ops.
    supervisor: OnceLock<Weak<Supervisor>>,
    clock: TurnClock,
}

impl Shared {
    fn is_current(&self, id: u64) -> bool {
        self.current.load(Ordering::SeqCst) == id
    }

    /// A new helper is about to start: older ones go stale, then the old one is ended.
    fn new_generation(&self) -> u64 {
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        let old = {
            let mut running = lock(&self.running);
            self.current.store(id, Ordering::SeqCst);
            running.take()
        };
        if let Some((_, old)) = old {
            end_in_background(old);
        }
        id
    }

    /// Every helper goes stale, then the running one is ended.
    fn stop(&self) {
        let old = {
            let mut running = lock(&self.running);
            self.current.store(0, Ordering::SeqCst);
            running.take()
        };
        if let Some((_, old)) = old {
            end_in_background(old);
        }
    }
}

/// End a helper off the caller's thread (`shutdown` can wait about 2 s).
fn end_in_background(running: Running) {
    // If the thread can't start, dropping `running` still closes the helper's stdin and
    // reaps it in the background.
    let _ = std::thread::Builder::new()
        .name("jarvis-helper-stop".into())
        .spawn(move || running.shutdown());
}

/// Before `ready` a helper's events go to `start_helper`; afterwards they are handled here.
enum Phase {
    Starting(mpsc::UnboundedSender<LinkEvent>),
    HandedOver,
}

/// The callback for the helper of generation `id`. Runs on the link's reader thread.
fn on_event(
    shared: Arc<Shared>,
    id: u64,
    phase: Arc<Mutex<Phase>>,
) -> Box<dyn Fn(LinkEvent) + Send> {
    Box::new(move |ev: LinkEvent| {
        if let Phase::Starting(tx) = &*lock(&phase) {
            let _ = tx.send(ev);
            return;
        }
        match ev {
            LinkEvent::Speaking(true) if shared.is_current(id) => shared.clock.sound(),
            LinkEvent::Closed => helper_closed(&shared, id),
            LinkEvent::Error { code, message } => {
                tracing::warn!(code = %code, "the voice helper reported an error");
                tracing::debug!(detail = %message, "the voice helper's error");
            }
            LinkEvent::Speaking(_) | LinkEvent::Ready { .. } => {}
        }
    })
}

/// A handed-over helper closed: tell the supervisor, unless that helper is stale.
fn helper_closed(shared: &Arc<Shared>, id: u64) {
    if !shared.is_current(id) {
        return;
    }
    let shared = shared.clone();
    tauri::async_runtime::spawn(async move {
        if !shared.is_current(id) {
            return;
        }
        let Some(supervisor) = shared.supervisor.get().and_then(Weak::upgrade) else {
            return;
        };
        supervisor.helper_closed().await;
    });
}

/// Wait for the helper's first answer: `Ok(version)` on `ready`.
async fn wait_ready(rx: &mut mpsc::UnboundedReceiver<LinkEvent>) -> Result<String, String> {
    let deadline = tokio::time::Instant::now() + READY_WAIT;
    loop {
        match tokio::time::timeout_at(deadline, rx.recv()).await {
            Err(_) => return Err("the voice helper did not become ready in 60 s".into()),
            Ok(Some(LinkEvent::Ready { helper_version })) => return Ok(helper_version),
            Ok(Some(LinkEvent::Error { code, .. })) => {
                return Err(format!("the voice helper failed to start ({code})"))
            }
            Ok(Some(LinkEvent::Closed)) | Ok(None) => {
                return Err("the voice helper exited before it was ready".into())
            }
            Ok(Some(LinkEvent::Speaking(_))) => {}
        }
    }
}

struct AppVoiceOps {
    shared: Arc<Shared>,
    models_dir: PathBuf,
    tts_speed: f32,
}

impl AppVoiceOps {
    /// Spawn the installed helper and wait for `ready`; a helper that fails is ended.
    async fn launch(&self, files: &HelperFiles, args: &[&str]) -> Result<Arc<dyn Speaks>, String> {
        let id = self.shared.new_generation();
        let settings = HelperSettings {
            voice: HELPER_VOICE.into(),
            tts_speed: self.tts_speed,
            model_path: files.model.to_string_lossy().into_owned(),
            voices_path: files.voices.to_string_lossy().into_owned(),
            output_device: None,
        };
        let (tx, mut rx) = mpsc::unbounded_channel();
        let phase = Arc::new(Mutex::new(Phase::Starting(tx)));
        let started = Instant::now();
        let running = process::spawn(
            &files.exe,
            args,
            &settings,
            on_event(self.shared.clone(), id, phase.clone()),
            Box::new(|line: String| tracing::info!("helper: {line}")),
        )?;
        match wait_ready(&mut rx).await {
            Ok(version) => {
                let ready_ms = started.elapsed().as_millis() as u64;
                tracing::info!(helper_ready_ms = ready_ms, "voice helper ready");
                self.hand_over(id, &phase, &mut rx, running, &version)
            }
            Err(e) => {
                end_in_background(running);
                Err(e)
            }
        }
    }

    /// The helper of generation `id` said `ready`: keep it and hand its voice over, unless it
    /// was stopped meanwhile or closed straight after.
    fn hand_over(
        &self,
        id: u64,
        phase: &Mutex<Phase>,
        rx: &mut mpsc::UnboundedReceiver<LinkEvent>,
        running: Running,
        version: &str,
    ) -> Result<Arc<dyn Speaks>, String> {
        let mut kept = lock(&self.shared.running);
        if !self.shared.is_current(id) {
            drop(kept);
            end_in_background(running);
            return Err("the voice helper was stopped while it started".into());
        }
        *lock(phase) = Phase::HandedOver;
        // Events after `ready` that came in before the switch above.
        let mut closed = false;
        while let Ok(ev) = rx.try_recv() {
            closed |= ev == LinkEvent::Closed;
        }
        if closed {
            drop(kept);
            end_in_background(running);
            return Err("the voice helper exited before it was ready".into());
        }
        let voice: Arc<dyn Speaks> = running.voice.clone();
        *kept = Some((id, running));
        tracing::info!(helper_version = version, "the voice helper is ready");
        Ok(voice)
    }
}

#[async_trait]
impl VoiceOps for AppVoiceOps {
    async fn start_helper(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String> {
        let last = AtomicU8::new(255);
        let files = install::ensure(&self.models_dir, &HttpDownload::default(), &|p| {
            if last.swap(p, Ordering::Relaxed) != p {
                status(Status::HelperDownloading(p));
            }
        })
        .await?;
        status(Status::HelperStarting);
        self.launch(&files, &[]).await
    }

    async fn start_builtin(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String> {
        use jarvis_voice::model::{self, GEORGE, KOKORO_URL};
        use jarvis_voice::{kokoro::Kokoro, speaker::Speaker, AudioOut, Voice};

        let last = AtomicU8::new(255);
        let files = model::ensure(
            &self.models_dir,
            KOKORO_URL,
            &HttpDownload::default(),
            &|p| {
                if last.swap(p, Ordering::Relaxed) != p {
                    status(Status::Downloading(p));
                }
            },
        )
        .await
        .map_err(|e| format!("voice download failed: {e}"))?;
        status(Status::Loading);
        let engine = match tokio::task::spawn_blocking(move || Kokoro::load(&files, GEORGE)).await {
            Ok(Ok(engine)) => engine,
            Ok(Err(e)) => return Err(format!("couldn't load the voice: {e}")),
            Err(e) => return Err(format!("voice loader crashed: {e}")),
        };
        // Voice::start can't report a missing output device, so check for one first.
        match tokio::task::spawn_blocking(|| Speaker::open().map(drop)).await {
            Ok(Ok(())) => {}
            Ok(Err(e)) => return Err(format!("no sound output: {e}")),
            Err(e) => return Err(format!("no sound output: {e}")),
        }
        let voice: Arc<dyn Speaks> = Arc::new(Voice::start(
            Box::new(engine),
            Box::new(|| Speaker::open().map(|s| Box::new(s) as Box<dyn AudioOut>)),
        ));
        Ok(voice)
    }

    fn stop_helper(&self) {
        self.shared.stop();
    }
}
