//! THROWAWAY: the slice 0 spike (design 2026-11-26). On-device readouts for the phone build:
//! what the device is, how fast the voice engines run, and whether the server can be reached.
//! Never merged.

// The timing wrappers are used only where the voice engines are built (Windows, Android).
#![allow(dead_code)]

use futures_util::StreamExt;
use jarvis_ears::{Transcriber, WakeDetector, SAMPLE_RATE};
use jarvis_voice::{Audio, SpeechEngine};
use serde::Serialize;
use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

static LOG: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
const KEEP: usize = 300;

/// Add a line to the on-screen log.
pub fn note(msg: impl Into<String>) {
    let line = format!("{} {}", chrono::Local::now().format("%H:%M:%S"), msg.into());
    tracing::info!("spike: {line}");
    if let Ok(mut log) = LOG.lock() {
        if log.len() == KEEP {
            log.pop_front();
        }
        log.push_back(line);
    }
}

/// Times each sentence George speaks.
pub struct TimedEngine<E>(pub E);

impl<E: SpeechEngine> SpeechEngine for TimedEngine<E> {
    fn synth(&mut self, text: &str) -> Result<Audio, String> {
        let t = Instant::now();
        let out = self.0.synth(text);
        if let Ok(a) = &out {
            note(format!(
                "voice: {} letters -> {:.1} s of sound, made in {} ms",
                text.chars().count(),
                a.samples.len() as f32 / a.sample_rate.max(1) as f32,
                t.elapsed().as_millis()
            ));
        }
        out
    }
}

/// Times each stretch of speech recognised.
pub struct TimedEars<T>(pub T);

impl<T: Transcriber> Transcriber for TimedEars<T> {
    fn transcribe(&mut self, samples: &[f32]) -> Result<String, String> {
        let t = Instant::now();
        let out = self.0.transcribe(samples);
        note(format!(
            "heard: {:.1} s of speech, recognised in {} ms{}",
            samples.len() as f32 / SAMPLE_RATE as f32,
            t.elapsed().as_millis(),
            if out.is_ok() { "" } else { " (failed)" }
        ));
        out
    }
}

/// Notes each time the wake word fires.
pub struct TimedWake(pub Box<dyn WakeDetector>);

impl WakeDetector for TimedWake {
    fn accept(&mut self, samples: &[f32]) -> bool {
        let fired = self.0.accept(samples);
        if fired {
            note("wake word heard");
        }
        fired
    }
    fn reset(&mut self) {
        self.0.reset();
    }
}

fn prop(name: &str) -> String {
    std::process::Command::new("getprop")
        .arg(name)
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "?".into())
}

fn device() -> String {
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(0);
    if cfg!(target_os = "android") {
        format!(
            "{} {} | Android {} (API {}) | chip {} | {} cores",
            prop("ro.product.manufacturer"),
            prop("ro.product.model"),
            prop("ro.build.version.release"),
            prop("ro.build.version.sdk"),
            prop("ro.soc.model"),
            cores
        )
    } else {
        format!("{} | {} cores", std::env::consts::OS, cores)
    }
}

/// The battery lines the Android service writes (it knows the app's data folder; we try the
/// places that folder can be).
fn battery(app: &AppHandle) -> Vec<String> {
    let mut places: Vec<PathBuf> = Vec::new();
    for dir in [app.path().app_local_data_dir(), app.path().app_data_dir()]
        .into_iter()
        .flatten()
    {
        places.push(dir.join("spike-battery.log"));
        if let Some(parent) = dir.parent() {
            places.push(parent.join("spike-battery.log"));
        }
    }
    places
        .iter()
        .find_map(|p| std::fs::read_to_string(p).ok())
        .map(|text| {
            let lines: Vec<&str> = text.lines().collect();
            let from = lines.len().saturating_sub(40);
            lines[from..].iter().map(|l| l.to_string()).collect()
        })
        .unwrap_or_default()
}

#[derive(Serialize)]
pub struct SpikeInfo {
    android: bool,
    device: String,
    lines: Vec<String>,
    battery: Vec<String>,
}

#[tauri::command]
pub fn spike_info(app: AppHandle) -> SpikeInfo {
    static DEVICE: OnceLock<String> = OnceLock::new();
    SpikeInfo {
        android: cfg!(target_os = "android"),
        device: DEVICE.get_or_init(device).clone(),
        lines: LOG
            .lock()
            .map(|l| l.iter().cloned().collect())
            .unwrap_or_default(),
        battery: battery(&app),
    }
}

/// One try: open the gateway's WebSocket and wait for its signed challenge. Nothing is sent,
/// so no node is enrolled.
async fn reach_once(addr: &str) -> String {
    let t = Instant::now();
    let url = format!("ws://{addr}/v1/node");
    let opened = tokio::time::timeout(
        Duration::from_secs(10),
        tokio_tungstenite::connect_async(url.as_str()),
    )
    .await;
    match opened {
        Err(_) => "no answer in 10 s".into(),
        Ok(Err(_)) => "could not connect".into(),
        Ok(Ok((mut ws, _))) => {
            let ms = t.elapsed().as_millis();
            match tokio::time::timeout(Duration::from_secs(5), ws.next()).await {
                Ok(Some(Ok(m))) => {
                    format!("ok: connected in {ms} ms, challenge of {} bytes", m.len())
                }
                _ => format!("connected in {ms} ms, but no challenge came"),
            }
        }
    }
}

/// Test the link now, then once a minute (so the log shows what happens with the screen locked).
/// The address is typed on the phone and kept in memory only.
#[tauri::command]
pub async fn spike_reach(addr: String) -> Result<String, String> {
    let addr = addr.trim().to_string();
    let ok = addr.rsplit_once(':').is_some_and(|(host, port)| {
        !host.is_empty()
            && port.parse::<u16>().is_ok()
            && host
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-'))
    });
    if !ok {
        return Err("Type it as host:port".into());
    }
    let first = reach_once(&addr).await;
    note(format!("server link: {first}"));
    static REPEAT: OnceLock<()> = OnceLock::new();
    if REPEAT.set(()).is_ok() {
        tauri::async_runtime::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(60)).await;
                let again = reach_once(&addr).await;
                note(format!("server link: {again}"));
            }
        });
    }
    Ok(first)
}
