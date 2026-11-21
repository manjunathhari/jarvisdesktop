//! Plays audio through the default output device (rodio).

use crate::{Audio, AudioOut};
use std::time::Duration;

pub struct Speaker {
    // The stream must stay alive while the sink plays.
    _stream: rodio::OutputStream,
    sink: rodio::Sink,
}

impl Speaker {
    pub fn open() -> Result<Self, String> {
        let (stream, handle) = rodio::OutputStream::try_default().map_err(|e| e.to_string())?;
        let sink = rodio::Sink::try_new(&handle).map_err(|e| e.to_string())?;
        Ok(Self {
            _stream: stream,
            sink,
        })
    }
}

impl AudioOut for Speaker {
    fn play(&mut self, audio: Audio, cancelled: &dyn Fn() -> bool) {
        self.sink.append(rodio::buffer::SamplesBuffer::new(
            1,
            audio.sample_rate,
            audio.samples,
        ));
        while !self.sink.empty() {
            if cancelled() {
                self.sink.stop();
                return;
            }
            std::thread::sleep(Duration::from_millis(15));
        }
    }
}
