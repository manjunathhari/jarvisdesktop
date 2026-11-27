import asyncio
import re
import struct
import sys
import time
import types

import pytest
from loguru import logger
from pipecat.frames.frames import OutputAudioRawFrame, StartFrame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from jarvis_voice_helper.pipeline import PipecatSpeaker
from jarvis_voice_helper.protocol import ModelMissing, NoOutputDevice, Settings

SETTINGS = Settings("bm_george", 1.0, "/m/k.onnx", "/m/v.bin", None)
RATE = 24000
LEVELS = (3000, 6000, 9000, 12000, 15000)


class FakeTTS(TTSService):
    """A real Pipecat TTS service that yields `seconds` of square wave per sentence.

    Sentence n uses the level LEVELS[n], so the order is visible in the audio.
    """

    def __init__(self, seconds=0.5, scale=1.0, silent_word=None):
        self.silent_word = silent_word
        self.seconds = seconds
        self.scale = scale
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            stop_frame_timeout_s=0.3,
            settings=TTSSettings(model=None, voice="fake", language=None),
        )
        self.sentences = []

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text, context_id):
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            if self.silent_word and self.silent_word in sentence:
                continue  # a sentence the voice cannot speak: no audio at all
            level = int(LEVELS[len(self.sentences) % len(LEVELS)] * self.scale)
            self.sentences.append(sentence)
            half = [level if (i // 24) % 2 else -level for i in range(int(RATE * self.seconds))]
            yield TTSAudioRawFrame(
                audio=struct.pack(f"<{len(half)}h", *half),
                sample_rate=RATE,
                num_channels=1,
                context_id=context_id,
            )


class CollectingSink(BaseOutputTransport):
    """Stands in for the sound device: records each chunk with its arrival time."""

    def __init__(self, seconds=0.5, scale=1.0, silent_word=None):
        self.silent_word = silent_word
        self.seconds = seconds
        self.scale = scale
        super().__init__(TransportParams(audio_out_enabled=True, audio_out_sample_rate=RATE))
        self.chunks = []

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        self.chunks.append((time.monotonic(), frame.audio))
        await asyncio.sleep(len(frame.audio) / 2 / RATE)  # a sound device blocks while it plays
        return True

    def levels(self):
        """The sequence of distinct tone levels that reached the sink, in order."""
        seen = []
        for _, audio in self.chunks:
            peak = max(abs(v) for v in struct.unpack(f"<{len(audio) // 2}h", audio))
            if peak and (not seen or seen[-1] != peak):
                seen.append(peak)
        return seen


async def start_speaker(events):
    sink = CollectingSink()
    speaker = PipecatSpeaker(
        lambda on, turn: events.append((on, turn)),
        tts_factory=lambda _settings: FakeTTS(),
        sink_factory=lambda _settings: sink,
    )
    await speaker.start(SETTINGS)
    return speaker, sink


async def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_three_sentences_play_in_order_with_one_start_and_one_stop():
    events = []
    speaker, sink = await start_speaker(events)
    await speaker.say(1, "First one. Second one. Third one.")
    await wait_for(lambda: (False, 1) in events)
    await speaker.close()
    assert sink.levels() == [LEVELS[0], LEVELS[1], LEVELS[2]]
    assert events == [(True, 1), (False, 1)]
    played = sum(len(audio) for _, audio in sink.chunks) / 2 / RATE
    assert played >= 1.4


async def test_say_returns_at_once():
    speaker, _ = await start_speaker([])
    began = time.monotonic()
    await speaker.say(1, "One. Two. Three.")
    await speaker.stop()
    assert time.monotonic() - began < 0.25
    await speaker.close()


async def test_stop_silences_within_300_ms_and_drops_the_queue():
    events = []
    speaker, sink = await start_speaker(events)
    await speaker.say(1, "One.")
    await speaker.say(1, "Two.")
    await speaker.say(1, "Three.")
    await asyncio.sleep(0.1)
    await wait_for(lambda: sink.chunks)
    stopped_at = time.monotonic()
    await speaker.stop()
    await wait_for(lambda: (False, 1) in events)
    await asyncio.sleep(1.0)
    await speaker.close()
    late = [t for t, _ in sink.chunks if t > stopped_at + 0.3]
    assert late == []
    assert LEVELS[1] not in sink.levels() and LEVELS[2] not in sink.levels()
    assert events == [(True, 1), (False, 1)]


async def test_say_after_stop_plays():
    events = []
    speaker, sink = await start_speaker(events)
    await speaker.say(1, "One. Two.")
    await wait_for(lambda: sink.chunks)
    await speaker.stop()
    await wait_for(lambda: (False, 1) in events)
    before = len(sink.chunks)
    await speaker.say(2, "Again.")
    await wait_for(lambda: (False, 2) in events)
    await speaker.close()
    assert len(sink.chunks) > before and (True, 2) in events


async def test_turn_reported_is_that_of_the_most_recent_say():
    events = []
    speaker, _ = await start_speaker(events)
    await speaker.say(1, "One.")
    await wait_for(lambda: (False, 1) in events)
    await speaker.say(5, "Two.")
    await wait_for(lambda: (False, 5) in events)
    await speaker.close()
    assert events == [(True, 1), (False, 1), (True, 5), (False, 5)]


async def test_what_is_said_never_reaches_the_info_log():
    phrase = "Zanzibar quokka umbrella"
    info, debug = [], []
    ids = [logger.add(info.append, level="INFO"), logger.add(debug.append, level="DEBUG")]
    try:
        events = []
        speaker, _ = await start_speaker(events)
        await speaker.say(1, f"{phrase}.")
        await wait_for(lambda: (False, 1) in events)
        await speaker.close()
    finally:
        for sink_id in ids:
            logger.remove(sink_id)
    assert any(phrase in str(m) for m in debug), "the guard is only meaningful if DEBUG sees it"
    assert not any(phrase in str(m) for m in info)


async def test_missing_model_file_raises_model_missing(tmp_path):
    voices = tmp_path / "voices.bin"
    voices.write_bytes(b"x")
    speaker = PipecatSpeaker(lambda on, turn: None)
    missing_model = Settings("bm_george", 1.0, str(tmp_path / "no.onnx"), str(voices), None)
    with pytest.raises(ModelMissing):
        await speaker.start(missing_model)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"x")
    missing_voices = Settings("bm_george", 1.0, str(model), str(tmp_path / "no.bin"), None)
    with pytest.raises(ModelMissing):
        await speaker.start(missing_voices)
    assert not (tmp_path / "no.onnx").exists() and not (tmp_path / "no.bin").exists()


def _fake_pyaudio(monkeypatch, open_error):
    class FakePyAudio:
        def open(self, **_kwargs):
            raise open_error

        def terminate(self):
            pass

    fake = types.ModuleType("pyaudio")
    fake.PyAudio = FakePyAudio
    monkeypatch.setitem(sys.modules, "pyaudio", fake)


def test_output_open_failure_raises_no_output_device(monkeypatch, tmp_path):
    _fake_pyaudio(monkeypatch, OSError("no device"))
    (tmp_path / "m.onnx").write_bytes(b"x")
    (tmp_path / "v.bin").write_bytes(b"x")
    settings = Settings("bm_george", 1.0, str(tmp_path / "m.onnx"), str(tmp_path / "v.bin"), 7)

    async def attempt():
        speaker = PipecatSpeaker(lambda on, turn: None, tts_factory=lambda _s: FakeTTS())
        await speaker.start(settings)

    with pytest.raises(NoOutputDevice):
        asyncio.run(attempt())
    sys.modules.pop("pipecat.transports.local.audio", None)


def test_missing_pyaudio_raises_no_output_device(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyaudio", None)

    async def attempt():
        speaker = PipecatSpeaker(lambda on, turn: None, tts_factory=lambda _s: FakeTTS())
        await speaker.start(SETTINGS)

    with pytest.raises(NoOutputDevice):
        asyncio.run(attempt())


def test_pyaudio_is_not_imported_with_the_module():
    import subprocess

    code = (
        "import sys, jarvis_voice_helper.pipeline, jarvis_voice_helper.selftest;"
        "assert 'pyaudio' not in sys.modules and "
        "'pipecat.transports.local.audio' not in sys.modules"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def _selftest(tmp_path, **fake):
    from jarvis_voice_helper import selftest

    seen = []
    code = selftest.run(
        tmp_path / "out.wav",
        "m",
        "v",
        tts_factory=lambda _s: seen.append(FakeTTS(**fake)) or seen[0],
    )
    return code, seen[0]


def test_selftest_writes_a_wav_and_passes_when_long_and_loud_enough(tmp_path):
    import wave

    code, tts = _selftest(tmp_path, seconds=1.0)
    assert code == 0
    assert " ".join(tts.sentences) == "Good evening, Manj. All systems are ready."
    with wave.open(str(tmp_path / "out.wav")) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, RATE)
        assert wav.getnframes() / RATE == pytest.approx(2.0)


def test_selftest_fails_when_too_short(tmp_path, capsys):
    code, _ = _selftest(tmp_path, seconds=0.5)
    assert code == 1 and "needs more than 1.5 s" in capsys.readouterr().err


def test_selftest_fails_when_too_quiet(tmp_path, capsys):
    code, _ = _selftest(tmp_path, seconds=1.0, scale=0.001)
    assert code == 1 and "peak" in capsys.readouterr().err


def test_selftest_with_missing_model_fails_without_downloading(tmp_path, capsys):
    from jarvis_voice_helper import selftest

    code = selftest.run(tmp_path / "o.wav", tmp_path / "no.onnx", tmp_path / "no.bin")
    assert code == 1 and "not found" in capsys.readouterr().err
    assert not (tmp_path / "o.wav").exists() and not (tmp_path / "no.onnx").exists()


async def test_a_sentence_with_no_audio_is_not_logged_at_warning_or_above():
    from io import StringIO

    from jarvis_voice_helper.main import configure_logging

    phrase = "Zanzibar quokka umbrella"
    production, raw = StringIO(), []
    configure_logging(production)  # the same level and filter as the helper uses
    raw_id = logger.add(raw.append, level="WARNING")
    events = []
    sink = CollectingSink()
    speaker = PipecatSpeaker(
        lambda on, turn: events.append((on, turn)),
        tts_factory=lambda _s: FakeTTS(silent_word="Zanzibar"),
        sink_factory=lambda _s: sink,
    )
    try:
        await speaker.start(SETTINGS)
        await speaker.say(1, f"{phrase}.")
        await asyncio.sleep(1.5)
        await speaker.say(2, "Spoken sentence.")
        await wait_for(lambda: (False, 2) in events)
        await speaker.close()
    finally:
        logger.remove(raw_id)
    assert any(phrase in str(m) for m in raw), "unfiltered, Pipecat does log it at WARNING"
    assert phrase not in production.getvalue()


def test_pipecat_error_records_are_scrubbed_but_other_errors_are_kept():
    from io import StringIO

    from jarvis_voice_helper.main import configure_logging

    out = StringIO()
    configure_logging(out)
    secret = "the secret sentence"
    from_pipecat = logger.patch(lambda r: r.update(name="pipecat.services.ai_service"))
    from_pipecat.error(f"exception processing TTSSpeakFrame(text={secret})")
    from_pipecat.warning(secret)
    from_pipecat.debug(secret)
    logger.error("helper's own error")
    text = out.getvalue()
    assert secret not in text and "helper's own error" in text
    assert text.count("message withheld") == 2  # the pipecat error and warning, not the debug


class RaisingRunner:
    def __init__(self, **_kwargs):
        pass

    async def add_workers(self, *_workers):
        pass

    async def run(self):
        raise RuntimeError("the runner died")


class StuckRunner(RaisingRunner):
    async def run(self):
        await asyncio.Event().wait()


async def _lost_task_errors(monkeypatch, **patches):
    import gc

    from jarvis_voice_helper import pipeline

    for name, value in patches.items():
        monkeypatch.setattr(pipeline, name, value)
    lost = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: lost.append(ctx))
    speaker = PipecatSpeaker(
        lambda on, turn: None,
        tts_factory=lambda _s: FakeTTS(),
        sink_factory=lambda _s: CollectingSink(),
    )
    with pytest.raises(RuntimeError):
        await speaker.start(SETTINGS)
    gc.collect()
    await asyncio.sleep(0.1)
    gc.collect()
    return lost


async def test_failing_start_leaves_no_unretrieved_task_exception(monkeypatch):
    lost = await _lost_task_errors(monkeypatch, WorkerRunner=RaisingRunner)
    assert lost == []


async def test_start_timeout_leaves_no_unretrieved_task_exception(monkeypatch):
    lost = await _lost_task_errors(monkeypatch, WorkerRunner=StuckRunner, START_TIMEOUT_SECS=0.2)
    assert lost == []


async def test_runner_dying_after_ready_makes_the_loop_report_and_return_1():
    import json

    from jarvis_voice_helper import protocol
    from jarvis_voice_helper.main import run
    from jarvis_voice_helper.protocol import Hello

    holder, out = [], []

    class OpenReader:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield protocol.encode(Hello(protocol=1, settings=SETTINGS)) + "\n"
            await holder[0]._worker.cancel()  # Pipecat ends the pipeline under the helper
            await asyncio.Event().wait()

    def factory(on_speaking):
        holder.append(
            PipecatSpeaker(
                on_speaking,
                tts_factory=lambda _s: FakeTTS(),
                sink_factory=lambda _s: CollectingSink(),
            )
        )
        return holder[0]

    code = await asyncio.wait_for(run(OpenReader(), out.append, factory), 20)
    assert code == 1
    lines = [json.loads(line) for line in out]
    assert [m["type"] for m in lines] == ["ready", "error"]
    assert lines[1] == {"type": "error", "code": "internal", "message": "PipelineEnded"}


def test_selftest_require_audio_lib_passes_when_pyaudio_imports(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyaudio", types.ModuleType("pyaudio"))
    from jarvis_voice_helper import selftest

    code = selftest.run(
        tmp_path / "out.wav",
        "m",
        "v",
        tts_factory=lambda _s: FakeTTS(seconds=1.0),
        require_audio_lib=True,
    )
    assert code == 0


def test_selftest_require_audio_lib_fails_when_pyaudio_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pyaudio", None)  # makes `import pyaudio` raise ImportError
    from jarvis_voice_helper import selftest

    code = selftest.run(
        tmp_path / "out.wav",
        "m",
        "v",
        tts_factory=lambda _s: FakeTTS(seconds=1.0),
        require_audio_lib=True,
    )
    assert code == 1
    assert "pyaudio" in capsys.readouterr().err
    assert not (tmp_path / "out.wav").exists()
