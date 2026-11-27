import io
import json
import subprocess
import sys

from loguru import logger

from jarvis_voice_helper import protocol
from jarvis_voice_helper.main import StdinLines, configure_logging, make_writer
from jarvis_voice_helper.protocol import Hello, Settings


async def collect(data: bytes) -> list[str]:
    return [line async for line in StdinLines(io.BytesIO(data))]


async def test_lines_split_on_newline_only_and_keep_unicode_separators():
    text = "a\u2028b\u2029c\u0085d \U0001f642"
    lines = await collect(f'{{"t":"{text}"}}\n'.encode() + b"second\n")
    assert lines == [f'{{"t":"{text}"}}\n', "second\n"]


async def test_invalid_utf8_line_is_ignored_and_the_last_line_needs_no_newline():
    lines = await collect(b"one\n\xff\xfe broken\ntwo\nlast")
    assert lines == ["one\n", "two\n", "last"]


async def test_end_of_input_ends_the_iteration():
    assert await collect(b"") == []


def test_writer_sends_utf8_plus_newline_and_flushes_every_line():
    class Recording(io.BytesIO):
        flushes = 0

        def flush(self):
            self.flushes += 1

    stream = Recording()
    write = make_writer(stream)
    write("héllo \U0001f642")
    write("two")
    assert stream.getvalue() == "héllo \U0001f642\ntwo\n".encode()
    assert stream.flushes == 2


def test_writer_survives_a_closed_pipe():
    stream = io.BytesIO()
    stream.close()
    make_writer(stream)("x")


def test_logging_is_info_and_above_on_the_given_stream():
    stream = io.StringIO()
    configure_logging(stream)
    logger.debug("spoken words")
    logger.info("starting")
    assert stream.getvalue().count("starting") == 1 and "spoken words" not in stream.getvalue()


def run_module(*args, stdin=b"", timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "jarvis_voice_helper", *args],
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_stdout_carries_protocol_lines_only():
    code = (
        "import sys;"
        "from jarvis_voice_helper.main import protect_stdout, make_writer;"
        "out = protect_stdout();"
        "print('a library banner'); sys.stdout.write('more'); sys.stdout.flush();"
        'make_writer(out)(\'{"type":"ready"}\')'
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert done.stdout == b'{"type":"ready"}\n'
    assert b"a library banner" in done.stderr


def test_no_arguments_runs_the_loop_and_a_bad_hello_exits_2():
    done = run_module(stdin=b"not json\xff\n" + b'{"type":"say","turn":1,"text":"x"}\n')
    assert done.returncode == 2
    lines = done.stdout.decode().splitlines()
    assert [json.loads(line)["code"] for line in lines] == ["bad_hello"]


def test_missing_model_exits_4_and_stdout_holds_just_the_error_line(tmp_path):
    settings = Settings("bm_george", 1.0, str(tmp_path / "a.onnx"), str(tmp_path / "b.bin"), None)
    hello = protocol.encode(Hello(protocol=1, settings=settings)).encode() + b"\n"
    process = subprocess.Popen(
        [sys.executable, "-m", "jarvis_voice_helper"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(hello)
    process.stdin.flush()  # stdin stays open: the helper must still exit on its own
    out = process.stdout.read()
    process.wait(timeout=60)
    process.stdin.close()
    assert process.returncode == 4
    assert [json.loads(line)["code"] for line in out.decode().splitlines()] == ["model_missing"]


def test_selftest_command_line_with_missing_files_exits_1(tmp_path):
    done = run_module(
        "--selftest", str(tmp_path / "o.wav"), "--model", "nope.onnx", "--voices", "nope.bin"
    )
    assert done.returncode == 1 and done.stdout == b""
