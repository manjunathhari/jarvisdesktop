//! The default microphone, delivered as 16 kHz mono chunks.

use crate::resample::to_mono_16k;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

/// Switches the microphone stream on and off (off = Windows shows no
/// "microphone in use" indicator).
#[derive(Clone)]
pub struct Mic {
    tx: std::sync::mpsc::Sender<bool>,
}

impl Mic {
    pub fn set_active(&self, on: bool) {
        let _ = self.tx.send(on);
    }
}

/// Open the default input device on its own thread (audio streams are not
/// `Send`), paused, handing every chunk to `sink` while active.
pub fn start_microphone(sink: impl Fn(Vec<f32>) + Send + 'static) -> Result<Mic, String> {
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<(), String>>();
    let (ctl_tx, ctl_rx) = std::sync::mpsc::channel::<bool>();
    std::thread::Builder::new()
        .name("jarvis-mic".into())
        .spawn(move || {
            let opened = (|| -> Result<cpal::Stream, String> {
                let device = cpal::default_host()
                    .default_input_device()
                    .ok_or("no microphone found")?;
                let config = device.default_input_config().map_err(|e| e.to_string())?;
                let channels = config.channels();
                let rate = config.sample_rate().0;
                tracing::info!(device = ?device.name().ok(), channels, rate, "microphone");
                let err = |e| tracing::warn!(error = %e, "microphone stream error");
                let stream = match config.sample_format() {
                    cpal::SampleFormat::F32 => device.build_input_stream(
                        &config.into(),
                        move |data: &[f32], _| sink(to_mono_16k(data, channels, rate)),
                        err,
                        None,
                    ),
                    cpal::SampleFormat::I16 => device.build_input_stream(
                        &config.into(),
                        move |data: &[i16], _| {
                            let f: Vec<f32> = data.iter().map(|s| *s as f32 / 32768.0).collect();
                            sink(to_mono_16k(&f, channels, rate))
                        },
                        err,
                        None,
                    ),
                    other => return Err(format!("unsupported microphone format {other:?}")),
                }
                .map_err(|e| e.to_string())?;
                stream.pause().map_err(|e| e.to_string())?;
                Ok(stream)
            })();
            match opened {
                Ok(stream) => {
                    let _ = ready_tx.send(Ok(()));
                    // The stream lives as long as the app; switch it on/off on request.
                    for on in ctl_rx {
                        let r = if on {
                            stream.play().map_err(|e| e.to_string())
                        } else {
                            stream.pause().map_err(|e| e.to_string())
                        };
                        if let Err(e) = r {
                            tracing::warn!(error = %e, on, "couldn't switch the microphone");
                        }
                    }
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e));
                }
            }
        })
        .map_err(|e| e.to_string())?;
    ready_rx.recv().map_err(|e| e.to_string())??;
    Ok(Mic { tx: ctl_tx })
}
