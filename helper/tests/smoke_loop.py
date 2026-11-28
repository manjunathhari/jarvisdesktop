"""Loop smoke test of the real helper program (standard library only; not collected by pytest).

    python tests/smoke_loop.py --exe <program and args...> --model M --voices V

Starts the program with `--output null`, sends hello and one say, and requires ready, speaking on,
speaking off, a clean exit 0 on shutdown, and protocol lines only on stdout. Any timeout or wrong
line kills the child, prints its stderr and stdout and exits 1.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

PHRASE = "Good evening, Manj. All systems are ready."
READY_SECS, SPEAK_SECS, EXIT_SECS = 180, 120, 30


def main() -> int:
    # --exe takes everything up to the next --model or --voices (the program and its arguments).
    argv = sys.argv[1:]
    if "--exe" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    start = argv.index("--exe") + 1
    end = start
    while end < len(argv) and argv[end] not in ("--model", "--voices"):
        end += 1
    exe = argv[start:end]
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--voices", required=True)
    args = parser.parse_args(argv[: start - 1] + argv[end:])
    args.exe = exe

    env = {**os.environ, "JARVIS_HELPER_DEBUG_STACKS": "60"}
    child = subprocess.Popen(
        [*args.exe, "--output", "null"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lines: queue.Queue[bytes | None] = queue.Queue()
    stdout_all, stderr_all = bytearray(), bytearray()

    def read_stdout():
        for raw in iter(child.stdout.readline, b""):
            stdout_all.extend(raw)
            lines.put(raw)
        lines.put(None)

    def drain_stderr():
        while chunk := child.stderr.read(4096):
            stderr_all.extend(chunk)

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=drain_stderr, daemon=True).start()

    def fail(why: str) -> int:
        child.kill()
        child.wait()
        time.sleep(0.2)
        print(f"SMOKE FAILED: {why}", file=sys.stderr)
        print(f"stdout: {bytes(stdout_all)!r}", file=sys.stderr)
        print(f"stderr:\n{bytes(stderr_all).decode(errors='replace')}", file=sys.stderr)
        return 1

    def send(message: dict) -> None:
        child.stdin.write(json.dumps(message).encode() + b"\n")
        child.stdin.flush()

    def expect(wanted: dict, secs: float) -> str | None:
        """Wait for a protocol line equal to `wanted` on its keys; return a problem or None."""
        deadline = time.monotonic() + secs
        while True:
            try:
                raw = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return f"no {wanted} within {secs} s"
            if raw is None:
                return f"stdout ended before {wanted}"
            try:
                got = json.loads(raw)
            except ValueError:
                return f"stdout line is not protocol JSON: {raw!r}"
            if not isinstance(got, dict) or "type" not in got:
                return f"stdout line is not a protocol message: {raw!r}"
            if all(got.get(k) == v for k, v in wanted.items()):
                return None
            return f"expected {wanted}, got {got}"

    hello = {
        "type": "hello",
        "protocol": 1,
        "settings": {
            "voice": "bm_george",
            "tts_speed": 1.0,
            "model_path": args.model,
            "voices_path": args.voices,
            "output_device": None,
        },
    }
    send(hello)
    if problem := expect({"type": "ready"}, READY_SECS):
        return fail(problem)
    send({"type": "say", "turn": 1, "text": PHRASE})
    for wanted in ({"type": "speaking", "on": True}, {"type": "speaking", "on": False}):
        if problem := expect(wanted, SPEAK_SECS):
            return fail(problem)
    send({"type": "shutdown"})
    try:
        code = child.wait(timeout=EXIT_SECS)
    except subprocess.TimeoutExpired:
        return fail(f"no exit within {EXIT_SECS} s of shutdown")
    if code != 0:
        return fail(f"exit code {code}, wanted 0")
    time.sleep(0.2)
    rest = []
    while not lines.empty():
        raw = lines.get()
        if raw:
            rest.append(raw)
    for raw in rest:
        try:
            json.loads(raw)
        except ValueError:
            return fail(f"stdout line is not protocol JSON: {raw!r}")
    print("SMOKE OK: ready, speaking on, speaking off, exit 0, stdout is protocol lines only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
