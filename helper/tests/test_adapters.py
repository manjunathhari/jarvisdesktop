import io
import json
import os
import subprocess
import sys
import tempfile
import threading

import pytest
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


CHILD_TIMEOUT = 30


def run_child(args, code=None, stdin=b"", close_stdin=True, timeout=CHILD_TIMEOUT):
    """Run the helper (or `-c code`) as a child process and never wait for it forever.

    stdout is a real pipe, read on a thread, as the app reads it; stderr goes to a temporary
    file so a chatty child can never block on a full pipe. The child must exit AND stdout must
    reach end of file, each within `timeout`; otherwise the child is killed and the test fails
    with what it printed, including the stacks it dumps after JARVIS_HELPER_DEBUG_STACKS seconds.
    """
    cmd = [sys.executable, "-c", code] if code else [sys.executable, "-m", "jarvis_voice_helper"]
    env = {**os.environ, "JARVIS_HELPER_DEBUG_STACKS": "20"}
    out = bytearray()
    with tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            [*cmd, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=env
        )
        assert process.stdin is not None and process.stdout is not None

        def drain():
            while chunk := process.stdout.read(4096):
                out.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except OSError:
            pass  # the child already left
        if close_stdin:
            process.stdin.close()
        problem = None
        try:
            process.wait(timeout=timeout)
            reader.join(timeout)
            if reader.is_alive():
                problem = (
                    f"child exited ({process.returncode}) but stdout never reached end of file"
                )
        except subprocess.TimeoutExpired:
            problem = f"child did not exit within {timeout} s"
        if problem:
            process.kill()
            process.wait()
            err.seek(0)
            pytest.fail(
                f"{problem}; it was killed\nstdout: {bytes(out)!r}\n"
                f"stderr:\n{err.read().decode(errors='replace')}"
            )
        if not process.stdin.closed:
            process.stdin.close()
        err.seek(0)
        return subprocess.CompletedProcess(cmd, process.returncode, bytes(out), err.read())


def run_module(*args, stdin=b""):
    return run_child(args, stdin=stdin)


def test_stdout_carries_protocol_lines_only():
    code = (
        "import sys;"
        "from jarvis_voice_helper.main import protect_stdout, make_writer;"
        "out = protect_stdout();"
        "print('a library banner'); sys.stdout.write('more'); sys.stdout.flush();"
        'make_writer(out)(\'{"type":"ready"}\')'
    )
    done = run_child([], code=code)
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
    # stdin stays open: the helper must still exit on its own
    done = run_child([], stdin=hello, close_stdin=False)
    assert done.returncode == 4, done.stderr
    assert [json.loads(line)["code"] for line in done.stdout.decode().splitlines()] == [
        "model_missing"
    ]


def test_selftest_command_line_with_missing_files_exits_1(tmp_path):
    done = run_module(
        "--selftest", str(tmp_path / "o.wav"), "--model", "nope.onnx", "--voices", "nope.bin"
    )
    assert done.returncode == 1 and done.stdout == b""


def test_a_hung_child_is_killed_and_reported_with_its_stderr():
    code = "import sys,time; sys.stderr.write('where I was'); sys.stderr.flush(); time.sleep(60)"
    with pytest.raises(pytest.fail.Exception, match="where I was"):
        run_child([], code=code, timeout=2)
