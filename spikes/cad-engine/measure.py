#!/usr/bin/env python3
"""Spike: measure the CADCraft headless tool behind an MCP stdio connection.

Throwaway measurement code. It starts `cadcraft-cli mcp` in a locked-down container, draws a
small fictional floor plan over MCP, renders and saves it, and writes the measurements to a JSON
file and a Markdown summary. It never fails on a measurement: a step that goes wrong is recorded
and the run carries on, so one run tells us as much as possible.

Standard library only. Run by .github/workflows/spike-cad-engine.yml.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path

CONTAINER = "cadspike"
ROOT_FLAGS = ["--automation-read-root", "/work", "--automation-write-root", "/work"]

# Pass marks agreed with Manj on 27 Nov 2026 (claude/cad-board-spike-note.md).
MARKS = {
    "image_mb": 150.0,
    "cold_start_s": 2.0,
    "draw_render_s": 5.0,
    "peak_memory_mb": 500.0,
}

GRAPHICS_SOUND_LIBS = ("vulkan", "libEGL", "libGL", "libX11", "libxcb", "wayland", "xkbcommon", "asound")


class Mcp:
    """A newline-delimited JSON-RPC client over a child process's stdin and stdout."""

    def __init__(self, cmd: list[str]):
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.next_id = 0

    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip()[:300])
            del self.stderr_tail[:-40]

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict | None = None, timeout: float = 60.0) -> tuple[dict | None, float]:
        """Send one request and wait for its reply. Returns (reply or None, seconds taken)."""
        self.next_id += 1
        wanted = self.next_id
        began = time.monotonic()
        message: dict = {"jsonrpc": "2.0", "id": wanted, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
        except (BrokenPipeError, OSError):
            return None, time.monotonic() - began
        deadline = began + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, time.monotonic() - began
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None, time.monotonic() - began
            if line is None:
                return None, time.monotonic() - began
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == wanted:
                return reply, time.monotonic() - began

    def close(self) -> int | None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None


def tool_text(reply: dict | None) -> tuple[bool, str]:
    """(worked, text) for a tools/call reply."""
    if reply is None:
        return False, "no reply"
    if "error" in reply:
        return False, json.dumps(reply["error"])[:400]
    result = reply.get("result") or {}
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return (not result.get("isError", False)), "\n".join(parts)[:4000]


def as_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def entity_total(inspect_text: str) -> int | None:
    """Best-effort count of entities from an inspect_drawing reply; None if the shape is unknown."""
    data = as_json(inspect_text)
    if not isinstance(data, dict):
        return None
    for key in ("entityCount", "entity_count", "entities_total", "total"):
        if isinstance(data.get(key), int):
            return data[key]
    counts = data.get("counts") or data.get("entityCounts") or data.get("entity_counts")
    if isinstance(counts, dict):
        numbers = [v for v in counts.values() if isinstance(v, int)]
        return sum(numbers) if numbers else None
    if isinstance(data.get("entities"), list):
        return len(data["entities"])
    return None


def container_file(path: str) -> int | None:
    """Read one number from a file inside the running container (its own cgroup files)."""
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "cat", path], capture_output=True, text=True, timeout=15, check=False
        )
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def docker_cmd(image: str, work: Path, with_roots: bool) -> list[str]:
    cmd = [
        "docker", "run", "--rm", "-i", "--name", CONTAINER,
        "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--memory", "768m", "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-e", "HOME=/tmp", "-e", "XDG_CONFIG_HOME=/tmp", "-e", "XDG_CACHE_HOME=/tmp",
        "-e", "XDG_DATA_HOME=/tmp", "-e", "XDG_STATE_HOME=/tmp",
        "-v", f"{work}:/work", image, "mcp",
    ]  # fmt: skip
    return cmd + (ROOT_FLAGS if with_roots else [])


def start(server_cmd: list[str] | None, image: str, work: Path, results: dict) -> Mcp | None:
    """Start the server and initialise it. Tries with the folder limits first, then without."""
    attempts = [(server_cmd, None)] if server_cmd else [(docker_cmd(image, work, True), True), (docker_cmd(image, work, False), False)]
    for cmd, with_roots in attempts:
        if not server_cmd:
            subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
        client = Mcp(cmd)
        reply, _ = client.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "jarvis-cad-spike", "version": "0"}},
            timeout=30,
        )
        if reply is not None and "result" in reply:
            results["cold_start_s"] = round(time.monotonic() - client.started, 3)
            results["folder_limits_used"] = with_roots
            results["server_info"] = reply["result"].get("serverInfo")
            results["protocol_version"] = reply["result"].get("protocolVersion")
            client.notify("notifications/initialized")
            return client
        results.setdefault("start_failures", []).append(
            {"folder_limits": with_roots, "exit": client.close(), "stderr": client.stderr_tail[-8:]}
        )
    return None


def run(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o777)
    results: dict = {"steps": []}

    def step(name: str, worked: bool, seconds: float | None = None, note: str = "") -> None:
        results["steps"].append({"step": name, "worked": worked, "seconds": None if seconds is None else round(seconds, 3), "note": note[:500]})

    client = start(shlex.split(args.server_cmd) if args.server_cmd else None, args.image, work.resolve(), results)
    if client is None:
        step("start and initialise", False, note="the server did not answer initialize")
        return results
    step("start and initialise", True, results["cold_start_s"])

    def call(name: str, arguments: dict, label: str | None = None, timeout: float = 60.0) -> tuple[bool, str, dict | None]:
        reply, seconds = client.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        worked, text = tool_text(reply)
        step(label or name, worked, seconds, "" if worked else text)
        return worked, text, reply

    reply, seconds = client.request("tools/list")
    tools = (reply or {}).get("result", {}).get("tools", [])
    names = [t.get("name") for t in tools]
    results["tools"] = names
    (out / "tools-list.json").write_text(json.dumps(tools, indent=2))
    step("tools/list", bool(tools), seconds, f"{len(tools)} tools")

    if not args.server_cmd:
        idle = container_file("/sys/fs/cgroup/memory.current")
        results["idle_memory_mb"] = None if idle is None else round(idle / 1048576, 1)

    call("new_drawing", {"metric": True})
    _, before_text, _ = call("inspect_drawing", {}, "inspect (empty drawing)")
    before = entity_total(before_text)

    # The fictional plan: a 8 m by 6 m flat, one partition wall, one round table (millimetres).
    began = time.monotonic()
    geometry_ok = []
    geometry_ok.append(call("execute", {"command": "line", "params": {"points": [[0, 0], [8000, 0], [8000, 6000], [0, 6000], [0, 0]]}}, "draw outer walls")[0])
    geometry_ok.append(call("execute", {"command": "line", "params": {"points": [[5000, 0], [5000, 6000]]}}, "draw partition wall")[0])
    geometry_ok.append(call("execute", {"command": "circle", "params": {"center": [2500, 3000], "radius": 600}}, "draw round table")[0])
    # Text and a dimension are typed at the prompt, since their JSON parameters are not known here.
    text_ok = call("script", {"text": "text\n600,5200\n250\n0\nLIVING ROOM\n\n"}, "type a text label")[0]
    call("cancel", {}, "cancel after text")
    dim_ok = call("script", {"text": "dimlinear\n0,0\n8000,0\n4000,-900\n"}, "type a dimension")[0]
    call("cancel", {}, "cancel after dimension")
    worked, _, render_reply = call("render", {"width": 1600, "height": 1000, "background": "white"}, "render to PNG", timeout=120)
    results["draw_render_s"] = round(time.monotonic() - began, 3)
    png = b""
    if worked and render_reply:
        for item in render_reply.get("result", {}).get("content", []):
            if item.get("type") == "image":
                png = base64.b64decode(item.get("data", ""))
    if png:
        (out / "plan.png").write_bytes(png)
    results["render_png_bytes"] = len(png)
    results["render_is_png"] = png[:8] == b"\x89PNG\r\n\x1a\n"
    if results["render_is_png"] and len(png) >= 24:
        results["render_pixels"] = f"{int.from_bytes(png[16:20], 'big')} x {int.from_bytes(png[20:24], 'big')}"

    _, after_text, _ = call("inspect_drawing", {"entities": True, "limit": 50}, "inspect (after drawing)")
    (out / "inspect-after.json").write_text(after_text)
    after = entity_total(after_text)
    results["inspect_after_excerpt"] = " ".join(after_text.split())[:900]
    if "query_entities" in names:
        _, listed_text, _ = call("query_entities", {"limit": 200}, "list entities")
        (out / "entities.json").write_text(listed_text)
        listed = as_json(listed_text)
        rows = listed if isinstance(listed, list) else (listed.get("entities") if isinstance(listed, dict) else None)
        if isinstance(rows, list):
            kinds: dict[str, int] = {}
            for row in rows:
                kind = str(row.get("type") or row.get("kind") or "?") if isinstance(row, dict) else "?"
                kinds[kind] = kinds.get(kind, 0) + 1
            results["entity_kinds"] = kinds
        else:
            results["entities_excerpt"] = " ".join(listed_text.split())[:600]
    results["entities_before"] = before
    results["entities_after"] = after
    results["geometry_calls_ok"] = sum(geometry_ok)
    results["text_typed_ok"] = text_ok
    results["dimension_typed_ok"] = dim_ok

    if "list_commands" in names:
        for word in ("text", "dim"):
            _, listing, _ = call("list_commands", {"filter": word}, f"list commands matching '{word}'")
            (out / f"commands-{word}.json").write_text(listing)
            found = as_json(listing)
            if isinstance(found, list):
                results.setdefault("command_ids", {})[word] = [str(c.get("id")) for c in found if isinstance(c, dict)][:25]

    saved = {}
    for ext in ("dxf", "svg", "pdf"):
        call("save", {"path": f"/work/plan.{ext}"}, f"save as {ext}", timeout=120)
        target = work / f"plan.{ext}"
        saved[ext] = target.stat().st_size if target.exists() else 0
    results["saved_bytes"] = saved

    if saved.get("dxf"):
        opened, opened_text, _ = call("open", {"path": "/work/plan.dxf"}, "open the saved DXF again")
        results["reopen_reply"] = opened_text[:600]
        if opened:
            _, again_text, _ = call("inspect_drawing", {}, "inspect (reopened)")
            results["entities_reopened"] = entity_total(again_text)

    # An attempt to write outside the allowed folder should be refused when the limits are on.
    outside_ok, outside_text, _ = call("save", {"path": "/tmp/outside.dxf"}, "a save outside the allowed folder is refused")
    results["outside_write_refused"] = not outside_ok
    results["outside_write_reply"] = outside_text[:300]
    # For this step a refusal is the good outcome, so record it that way round.
    results["steps"][-1]["worked"] = not outside_ok
    results["steps"][-1]["note"] = ("refused: " + outside_text[:200]) if not outside_ok else "ACCEPTED: the folder limit did not hold"

    if not args.server_cmd:
        peak = container_file("/sys/fs/cgroup/memory.peak")
        results["peak_memory_mb"] = None if peak is None else round(peak / 1048576, 1)
    results["exit_code"] = client.close()
    results["stderr_tail"] = client.stderr_tail[-12:]
    return results


def shown(value, unit: str) -> str:
    return "not measured" if value is None else f"{value} {unit}"


def verdict(value, mark: float) -> str:
    if value is None:
        return "not measured"
    return "pass" if value <= mark else "FAIL"


def summary(results: dict, facts: dict) -> str:
    needed = facts.get("needed_libs", [])
    heavy = [lib for lib in needed if any(word.lower() in lib.lower() for word in GRAPHICS_SOUND_LIBS)]
    ran = results.get("cold_start_s") is not None
    connects = facts.get("network_connect_attempts")
    rows = [
        ("Container image size", shown(facts.get("image_mb"), "MB"), f"{MARKS['image_mb']:.0f} MB or less", verdict(facts.get("image_mb"), MARKS["image_mb"])),
        ("Runs with no GPU, display or sound libraries", "ran in a bare image" if ran else "did not run", "Yes", "pass" if ran and not heavy else "FAIL"),
        ("Runs with the network disabled", "ran with no network" if ran else "did not run", "Yes", "pass" if ran else "FAIL"),
        ("Makes no connection attempt", "not measured" if connects is None else f"{connects} attempts", "0", "not measured" if connects is None else ("pass" if connects == 0 else "FAIL")),
        ("Cold start to first reply", shown(results.get("cold_start_s"), "s"), f"{MARKS['cold_start_s']:.0f} s or less", verdict(results.get("cold_start_s"), MARKS["cold_start_s"])),
        ("Simple plan drawn and rendered to PNG", shown(results.get("draw_render_s"), "s"), f"{MARKS['draw_render_s']:.0f} s or less", verdict(results.get("draw_render_s"), MARKS["draw_render_s"]) if results.get("render_is_png") else "FAIL (no PNG)"),
        ("Peak memory", shown(results.get("peak_memory_mb"), "MB"), f"{MARKS['peak_memory_mb']:.0f} MB or less", verdict(results.get("peak_memory_mb"), MARKS["peak_memory_mb"])),
        ("Memory when idle", shown(results.get("idle_memory_mb"), "MB"), "recorded", "recorded"),
    ]
    lines = ["## CAD engine spike: measurements", "", f"CADCraft {facts.get('version', '?')} headless tool, pinned by checksum. Fictional drawing only.", ""]
    lines += ["| Measure | Result | Pass mark | Verdict |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    lines += ["", "### Other facts", ""]
    lines.append(f"- Headless tool size on disk: {facts.get('cli_mb', 'not measured')} MB")
    lines.append(f"- Newest C library version it asks for: {facts.get('glibc_needed', 'not measured')}")
    lines.append(f"- Shared libraries it needs: {', '.join(needed) if needed else 'not measured'}")
    lines.append(f"- Graphics or sound libraries among them: {', '.join(heavy) if heavy else 'none'}")
    lines.append(f"- Folder limits (read and write roots) accepted by this version: {results.get('folder_limits_used')}")
    lines.append(f"- A save outside the allowed folder was refused: {results.get('outside_write_refused')}")
    lines.append(f"- Tools offered: {', '.join(str(n) for n in results.get('tools', [])) or 'none'}")
    lines.append(f"- Geometry calls that worked: {results.get('geometry_calls_ok')} of 3; text typed: {results.get('text_typed_ok')}; dimension typed: {results.get('dimension_typed_ok')}")
    lines.append(f"- Entities before, after, reopened: {results.get('entities_before')}, {results.get('entities_after')}, {results.get('entities_reopened')}")
    lines.append(f"- Entity kinds listed after drawing: {results.get('entity_kinds', results.get('entities_excerpt', 'not listed'))}")
    lines.append(f"- Rendered PNG: {results.get('render_png_bytes')} bytes, {results.get('render_pixels', 'size unknown')} pixels")
    lines.append(f"- Command ids matching 'text' and 'dim': {results.get('command_ids', 'not listed')}")
    lines.append(f"- Reply when the saved DXF was reopened: {' '.join(str(results.get('reopen_reply', '')).split())[:300]}")
    lines.append(f"- Drawing summary after drawing (start of the reply): {results.get('inspect_after_excerpt', '')}")
    for failure in results.get("start_failures", []):
        lines.append(f"- Start with folder limits = {failure.get('folder_limits')} failed, exit {failure.get('exit')}: {' / '.join(failure.get('stderr', []))[:500]}")
    lines.append(f"- Saved file sizes in bytes: {results.get('saved_bytes')}")
    lines.append(f"- One-shot sample render under trace: {facts.get('oneshot_note', 'not run')}")
    lines += ["", "### Steps", "", "| Step | Worked | Seconds | Note |", "|---|---|---|---|"]
    for s in results.get("steps", []):
        note = str(s["note"]).replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {s['step']} | {'yes' if s['worked'] else 'NO'} | {s['seconds'] if s['seconds'] is not None else ''} | {note} |")
    lines += ["", "Not measured here: how reliably Claude drives the commands (needs an API key and Manj's approval), DWG files, and a security review."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="out", help="folder for results")
    parser.add_argument("--image", default="cad-engine-spike", help="container image holding cadcraft-cli")
    parser.add_argument("--facts", default="", help="JSON file of facts gathered by the workflow (sizes, libraries)")
    parser.add_argument("--server-cmd", default="", help="run this command instead of the container (for testing this script)")
    args = parser.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    facts = json.loads(Path(args.facts).read_text()) if args.facts and Path(args.facts).exists() else {}
    results = run(args)
    Path(args.out, "measurements.json").write_text(json.dumps({"facts": facts, "results": results}, indent=2))
    text = summary(results, facts)
    Path(args.out, "summary.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
