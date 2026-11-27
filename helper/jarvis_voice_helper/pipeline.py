"""Pipecat pipeline that speaks text with the Kokoro voice through the local sound output."""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from jarvis_voice_helper.protocol import ModelMissing, NoOutputDevice, Settings

OnSpeaking = Callable[[bool, int], None]

SAMPLE_RATE = 24000  # Kokoro's native rate
MIN_SPEED, MAX_SPEED = 0.5, 2.0  # the range Kokoro accepts
START_TIMEOUT_SECS = 60.0
CLOSE_TIMEOUT_SECS = 5.0


class Reporter(FrameProcessor):
    """First processor in the pipeline: turns the output's speaking frames into `on_speaking`."""

    def __init__(self, on_speaking: OnSpeaking):
        super().__init__()
        self._on_speaking = on_speaking
        self.turn = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._on_speaking(True, self.turn)
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._on_speaking(False, self.turn)
        await self.push_frame(frame, direction)


def build_pipeline(
    tts: FrameProcessor, sink: FrameProcessor, reporter: Reporter | None = None
) -> PipelineWorker:
    """Build the speaking pipeline: reporter, then the TTS service, then the output."""
    reporter = reporter or Reporter(lambda _on, _turn: None)
    worker = PipelineWorker(
        Pipeline([reporter, tts, sink]),
        params=PipelineParams(audio_out_sample_rate=SAMPLE_RATE),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        cancel_on_idle_timeout=False,
    )
    return worker


def kokoro_tts(settings: Settings) -> FrameProcessor:
    """The Kokoro voice. Never downloads: missing files raise ModelMissing first."""
    for label, path in (("model", settings.model_path), ("voices", settings.voices_path)):
        if not Path(path).is_file():
            raise ModelMissing(f"speech {label} file not found: {path}")

    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.transcriptions.language import Language

    speed = min(MAX_SPEED, max(MIN_SPEED, float(settings.tts_speed)))
    return KokoroTTSService(
        model_path=settings.model_path,
        voices_path=settings.voices_path,
        settings=KokoroTTSService.Settings(
            voice=settings.voice, language=Language.EN_GB, speed=speed
        ),
    )


def local_output(settings: Settings) -> FrameProcessor:
    """The sound output. PyAudio is imported here so the module loads without it."""
    try:
        import pyaudio

        audio = pyaudio.PyAudio()
        try:
            probe = audio.open(
                format=getattr(pyaudio, "paInt16", 8),
                channels=1,
                rate=SAMPLE_RATE,
                output=True,
                output_device_index=settings.output_device,
            )
            probe.close()
        finally:
            terminate = getattr(audio, "terminate", None)
            if terminate:
                terminate()
        from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

        return LocalAudioTransport(
            LocalAudioTransportParams(
                audio_out_enabled=True, output_device_index=settings.output_device
            )
        ).output()
    except Exception as exc:
        raise NoOutputDevice(f"cannot open the sound output ({type(exc).__name__})") from exc


async def _reap(task: asyncio.Task[None]) -> None:
    """Cancel a runner task and retrieve its outcome so asyncio never reports it as lost."""
    task.cancel()
    await asyncio.wait({task}, timeout=CLOSE_TIMEOUT_SECS)
    if task.done() and not task.cancelled():
        task.exception()


class PipecatSpeaker:
    """Speaks queued text. `say` and `stop` only queue frames and return at once."""

    def __init__(
        self,
        on_speaking: OnSpeaking,
        *,
        tts_factory: Callable[[Settings], FrameProcessor] = kokoro_tts,
        sink_factory: Callable[[Settings], FrameProcessor] = local_output,
    ):
        self._reporter = Reporter(on_speaking)
        self._tts_factory = tts_factory
        self._sink_factory = sink_factory
        self._worker: PipelineWorker | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self, settings: Settings) -> None:
        tts = self._tts_factory(settings)
        sink = self._sink_factory(settings)
        worker = build_pipeline(tts, sink, self._reporter)
        started = asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def _on_started(_worker: Any, _frame: Any) -> None:
            started.set()

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        runner_task = asyncio.create_task(runner.run())
        waiter = asyncio.create_task(started.wait())
        try:
            await asyncio.wait(
                {runner_task, waiter},
                timeout=START_TIMEOUT_SECS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
        if not started.is_set():
            await _reap(runner_task)
            raise RuntimeError("speech pipeline did not start")
        self._worker, self._runner_task = worker, runner_task

    async def wait_failure(self) -> str:
        """Return the reason (an exception class name, never text) once the pipeline has ended
        without `close()` having been called. Never returns if `close()` ends it."""
        task = self._runner_task
        if task is None:
            await asyncio.Event().wait()
        await asyncio.wait({task})
        if self._closing:
            await asyncio.Event().wait()
        exc = None if task.cancelled() else task.exception()
        return type(exc).__name__ if exc else "PipelineEnded"

    async def say(self, turn: int, text: str) -> None:
        if self._worker is None:
            return
        self._reporter.turn = turn
        await self._worker.queue_frame(TTSSpeakFrame(text))

    async def stop(self) -> None:
        if self._worker is not None:
            await self._worker.queue_frame(InterruptionFrame())

    async def close(self) -> None:
        worker, task = self._worker, self._runner_task
        self._closing = True
        self._worker = self._runner_task = None
        if worker is None or task is None:
            return
        await worker.cancel()
        try:
            await asyncio.wait_for(task, CLOSE_TIMEOUT_SECS)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception as exc:
            print(f"speech pipeline ended with {type(exc).__name__}", file=sys.stderr)
