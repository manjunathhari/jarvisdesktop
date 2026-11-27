"""Self-test: speak one fixed phrase with the real voice into a WAV file and check it is audible."""

import array
import asyncio
import sys
import wave
from collections.abc import Callable
from pathlib import Path

from pipecat.frames.frames import Frame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from jarvis_voice_helper.pipeline import build_pipeline, kokoro_tts
from jarvis_voice_helper.protocol import ModelMissing, Settings

PHRASE = "Good evening, Manj. All systems are ready."
VOICE = "bm_george"
MIN_SECONDS = 1.5
MIN_PEAK = 0.05


class AudioCollector(FrameProcessor):
    """Takes the place of the sound output: keeps the audio and passes every frame on."""

    def __init__(self):
        super().__init__()
        self.rates: set[int] = set()
        self.pcm = bytearray()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.rates.add(frame.sample_rate)
            self.pcm += frame.audio
        await self.push_frame(frame, direction)


async def _speak(tts: FrameProcessor) -> AudioCollector:
    collector = AudioCollector()
    worker = build_pipeline(tts, collector)
    await worker.queue_frame(TTSSpeakFrame(PHRASE))
    await worker.stop_when_done()
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()
    return collector


def _fail(why: str) -> int:
    print(f"selftest failed: {why}", file=sys.stderr)
    return 1


def run(
    out_wav: Path,
    model_path: str | Path,
    voices_path: str | Path,
    tts_factory: Callable[[Settings], FrameProcessor] = kokoro_tts,
) -> int:
    """Speak PHRASE to `out_wav` (16-bit mono). Returns 0 if audible and long enough, else 1."""
    settings = Settings(VOICE, 1.0, str(model_path), str(voices_path), None)
    try:
        tts = tts_factory(settings)
        collector = asyncio.run(_speak(tts))
    except ModelMissing as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"{type(exc).__name__} while speaking")

    if not collector.pcm:
        return _fail("no audio was produced")
    if len(collector.rates) != 1:
        return _fail(f"audio frames carry different sample rates: {sorted(collector.rates)}")
    (rate,) = collector.rates
    pcm = bytes(collector.pcm[: len(collector.pcm) // 2 * 2])  # 16-bit little-endian
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()

    out_wav = Path(out_wav)
    with wave.open(str(out_wav), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)

    seconds = len(samples) / rate
    peak = max(abs(v) for v in samples) / 32768
    print(f"selftest: {seconds:.2f} s, peak {peak:.3f}, wrote {out_wav}", file=sys.stderr)
    if seconds <= MIN_SECONDS:
        return _fail(f"audio is {seconds:.2f} s, needs more than {MIN_SECONDS} s")
    if peak <= MIN_PEAK:
        return _fail(f"peak level is {peak:.3f}, needs more than {MIN_PEAK}")
    return 0
