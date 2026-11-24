"""The two read-only sidecars really start (first deploy, 24 Nov 2026).

The first deploy failed before any container ran: Compose can't place an inline config in a
read-only container. These tests start the egress proxy and the Tailscale sidecar the way the
rendered compose file says (same image, entrypoint, environment, read-only root, tmpfs, caps)
and check that they come up with our rules. They need a Docker daemon that can pull the pinned
images, so they run only with JARVIS_DOCKER_LIVE=1 (CI's test job sets it).

The render test needs only the `docker compose` CLI and runs wherever it is installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SERVER = Path(__file__).resolve().parents[1]
DEPLOY = SERVER / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
ENTRYPOINT = DEPLOY / "gateway-ts" / "entrypoint.sh"
SQUID = DEPLOY / "egress-proxy" / "squid.conf"
CHOICE = "/tmp/jarvis-iptables"  # noqa: S108 (inside the sidecar: the backend its entrypoint chose)

live = pytest.mark.skipif(
    os.environ.get("JARVIS_DOCKER_LIVE") != "1", reason="needs Docker; set JARVIS_DOCKER_LIVE=1"
)


def _compose_cli() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def rendered() -> dict[str, Any]:
    if not _compose_cli():
        pytest.skip("docker compose CLI not installed")
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        env={**os.environ, "JARVIS_REF": "render-check"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    services: dict[str, Any] = json.loads(out)["services"]
    return _unescape(services)  # type: ignore[no-any-return]


def _unescape(value: Any) -> Any:
    """`compose config` writes each $ back out as $$, so its output can be loaded again; the
    container itself gets the single $. Undo that to see what the container gets."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, list):
        return [_unescape(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape(v) for k, v in value.items()}
    return value


def test_rendered_values_are_the_files(rendered: dict[str, Any]) -> None:
    """What each container gets is byte-for-byte the reviewable file."""
    assert rendered["gateway-ts"]["environment"]["JARVIS_TS_ENTRYPOINT"] == ENTRYPOINT.read_text()
    assert rendered["egress-proxy"]["environment"]["JARVIS_SQUID_CONF"] == SQUID.read_text()
    for name in ("gateway-ts", "egress-proxy"):
        start = rendered[name]["entrypoint"][2]
        assert "$$" not in start and "${JARVIS_" in start, name


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _run_args(service: dict[str, Any], name: str, network: str) -> list[str]:
    """`docker run` flags for the hardening the service declares."""
    args = ["run", "-d", "--name", name, "--network", network, "--read-only"]
    for mount in service["tmpfs"]:
        args += ["--tmpfs", mount]
    args += ["--cap-drop", "ALL"]
    for cap in service["cap_add"]:
        args += ["--cap-add", cap]
    for opt in service["security_opt"]:
        args += ["--security-opt", opt]
    for key, value in service["environment"].items():
        args += ["-e", f"{key}={value}"]
    args += ["--entrypoint", service["entrypoint"][0]]
    return args


@pytest.fixture
def network() -> Iterator[str]:
    name = f"jarvis-live-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", name)
    yield name
    _docker("network", "rm", name, check=False)


def _wait_running(name: str, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = _docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip()
        if state == "running":
            return
        if state == "exited":
            break
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    pytest.fail(f"{name} is not running:\n{logs.stdout}\n{logs.stderr}")


@live
def test_egress_proxy_starts_read_only_and_allows_only_claude(
    rendered: dict[str, Any], network: str
) -> None:
    proxy = rendered["egress-proxy"]
    name = f"jarvis-proxy-{uuid.uuid4().hex[:8]}"
    try:
        _docker(*_run_args(proxy, name, network), proxy["image"], *proxy["entrypoint"][1:])
        time.sleep(3)
        _wait_running(name)
        health = proxy["healthcheck"]["test"]
        assert health[0] == "CMD-SHELL"
        _docker("exec", name, "/bin/sh", "-c", health[1])  # check=True: must pass
        # A client on the same private network: CONNECT to Claude is let through, anything else
        # is refused by squid itself (403), before any connection is made.
        probe = ["run", "--rm", "--network", network, "--entrypoint", "/bin/bash", proxy["image"]]
        probe.append("-c")
        ask = (
            'printf \'CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n\' "$0" "$0" '
            f"| timeout 10 bash -c 'exec 3<>/dev/tcp/{name}/3128; cat >&3; head -n1 <&3'"
        )
        for target, status in (
            ("example.com:443", " 403 "),
            ("api.anthropic.com:80", " 403 "),
            ("api.anthropic.com:443", " 200 "),
        ):
            done = _docker(*probe, ask, target, check=False)
            assert status in done.stdout, f"{target}: {done.stdout!r} {done.stderr!r}"
    finally:
        _docker("rm", "-f", name, check=False)


@live
def test_gateway_sidecar_starts_read_only_and_installs_rules(
    rendered: dict[str, Any], network: str, tmp_path: Path
) -> None:
    """The sidecar runs its entrypoint from /tmp and installs the gateway's egress rules with a
    backend this kernel offers. The auth key is a dummy, so Tailscale never logs in; the rules
    are in place before that."""
    ts = rendered["gateway-ts"]
    name = f"jarvis-gwts-{uuid.uuid4().hex[:8]}"
    key = tmp_path / "tailscale_gateway_authkey"
    key.write_text("tskey-auth-dummy-not-a-real-key\n")
    key.chmod(0o444)
    args = _run_args(ts, name, network)
    args += ["--device", "/dev/net/tun:/dev/net/tun"]
    args += ["-v", f"{key}:/run/secrets/tailscale_gateway_authkey:ro"]
    try:
        _docker(*args, ts["image"], *ts["entrypoint"][1:])
        deadline = time.monotonic() + 30
        chosen = ""
        while time.monotonic() < deadline:  # the entrypoint writes its backend choice last
            chosen = _docker("exec", name, "cat", CHOICE, check=False).stdout
            if chosen.strip():
                break
            time.sleep(1)
        logs = _docker("logs", name, check=False)
        assert chosen.strip(), f"no rules installed:\n{logs.stdout}\n{logs.stderr}"
        ipt, *ip6t = chosen.split()
        rules = _docker("exec", name, ipt, "-S", "JARVIS_GW_OUT").stdout
        pg_ip = ts["environment"]["JARVIS_POSTGRES_IP"]
        assert rules.splitlines()[1:] == [
            "-A JARVIS_GW_OUT -o tailscale0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            f"-A JARVIS_GW_OUT -d {pg_ip}/32 -p tcp -m tcp --dport 5432 -j ACCEPT",
            "-A JARVIS_GW_OUT -j DROP",
        ], rules
        assert "egress rules for every non-root UID in place" in logs.stdout + logs.stderr
        jump = _docker("exec", name, ipt, "-S", "OUTPUT").stdout
        assert "--socket-exists" in jump and "! --uid-owner 0 -j JARVIS_GW_OUT" in jump
        for tool in ip6t:  # IPv6 rules too, wherever ip6tables works
            v6 = _docker("exec", name, tool, "-S", "JARVIS_GW_OUT").stdout
            assert v6.splitlines()[-1] == "-A JARVIS_GW_OUT -j DROP", v6
    finally:
        _docker("rm", "-f", name, check=False)
