"""The read-only sidecars really start (first deploy, 24 Nov 2026).

The first deploy failed before any container ran: Compose can't place an inline config in a
read-only container. These tests start the egress proxy and the Tailscale sidecar the way the
rendered compose file says (same image, entrypoint, environment, read-only root, tmpfs, caps)
and check that they come up with our rules; likewise the improve team's improve-egress,
claude-proxy and sandbox-db (profile "improve"). They need a Docker daemon that can pull the
pinned images, so they run only with JARVIS_DOCKER_LIVE=1 (CI's test job sets it).

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
IMPROVE_SQUID = DEPLOY / "improve" / "squid.conf"
CLAUDE_NGINX = DEPLOY / "improve" / "claude-proxy.conf"
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
    config = ["docker", "compose", "-f", str(COMPOSE), "--profile", "improve", "config"]
    out = subprocess.run(
        [*config, "--format", "json"],
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
    assert rendered["improve-egress"]["environment"]["JARVIS_SQUID_CONF"] == (
        IMPROVE_SQUID.read_text()
    )
    assert rendered["claude-proxy"]["environment"]["JARVIS_NGINX_CONF"] == CLAUDE_NGINX.read_text()
    for name in ("gateway-ts", "egress-proxy", "improve-egress", "claude-proxy"):
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
    for cap in service.get("cap_add", []):
        args += ["--cap-add", cap]
    for opt in service["security_opt"]:
        args += ["--security-opt", opt]
    for key, value in service["environment"].items():
        args += ["-e", f"{key}={value}"]
    if "user" in service:
        args += ["--user", service["user"]]
    for group in service.get("group_add", []):
        args += ["--group-add", group]
    if service.get("entrypoint"):  # compose renders an unset entrypoint as null
        args += ["--entrypoint", service["entrypoint"][0]]
    return args


@pytest.fixture
def network() -> Iterator[str]:
    name = f"jarvis-live-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", name)
    yield name
    _docker("network", "rm", name, check=False)


@pytest.fixture
def internal_network() -> Iterator[str]:
    """No internet: Tailscale's login with the dummy key stalls instead of failing and exiting,
    so the sidecar stays up while the test reads its rules."""
    name = f"jarvis-live-int-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", "--internal", name)
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
    rendered: dict[str, Any], internal_network: str, tmp_path: Path
) -> None:
    """The sidecar runs its entrypoint from /tmp and installs the gateway's egress rules with a
    backend this kernel offers. The auth key is a dummy, so Tailscale never logs in; the rules
    are in place before that."""
    ts = rendered["gateway-ts"]
    name = f"jarvis-gwts-{uuid.uuid4().hex[:8]}"
    key = tmp_path / "tailscale_gateway_authkey"
    key.write_text("tskey-auth-dummy-not-a-real-key\n")
    key.chmod(0o444)
    args = _run_args(ts, name, internal_network)
    # The service's two volumes, as tmpfs here: tailscaled needs both writable on a read-only root.
    args += ["--tmpfs", "/var/lib/tailscale", "--tmpfs", "/ts-socket"]
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
        # Everything read in one exec, so a container that stops later can't split the reads.
        both = _docker(
            "exec", name, "/bin/sh", "-c",
            f"{ipt} -S JARVIS_GW_OUT; echo ===; {ipt} -S OUTPUT"
            + "".join(f"; echo ===; {t} -S JARVIS_GW_OUT" for t in ip6t),
            check=False,
        ).stdout  # fmt: skip
        rules, jump, *v6_rules = both.split("===\n")
        pg_ip = ts["environment"]["JARVIS_POSTGRES_IP"]
        assert rules.splitlines()[1:] == [  # line 0 is "-N JARVIS_GW_OUT"
            "-A JARVIS_GW_OUT -o tailscale0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            f"-A JARVIS_GW_OUT -d {pg_ip}/32 -p tcp -m tcp --dport 5432 -j ACCEPT",
            "-A JARVIS_GW_OUT -j DROP",
        ], rules
        assert "egress rules for every non-root UID in place" in logs.stdout + logs.stderr
        assert "--socket-exists" in jump and "! --uid-owner 0 -j JARVIS_GW_OUT" in jump
        assert len(v6_rules) == len(ip6t)
        for v6 in v6_rules:  # IPv6 rules too, wherever ip6tables works
            assert v6.splitlines()[-1] == "-A JARVIS_GW_OUT -j DROP", v6
    finally:
        _docker("rm", "-f", name, check=False)


def _ask(network: str, image: str, proxy: str, request: str) -> str:
    """The first line of the proxy's answer to one raw request, from a client on `network`."""
    tunnel = f"exec 3<>/dev/tcp/{proxy}/3128; cat >&3; head -n1 <&3"
    script = f"printf \"$0\" | timeout 10 bash -c '{tunnel}'"
    done = _docker(
        "run", "--rm", "--network", network, "--entrypoint", "/bin/bash", image, "-c", script,
        request, check=False,
    )  # fmt: skip
    return done.stdout + done.stderr


@live
def test_improve_egress_allows_public_https_only_and_logs(
    rendered: dict[str, Any], network: str
) -> None:
    proxy = rendered["improve-egress"]
    name = f"jarvis-improve-egress-{uuid.uuid4().hex[:8]}"
    try:
        _docker(*_run_args(proxy, name, network), proxy["image"], *proxy["entrypoint"][1:])
        time.sleep(3)
        _wait_running(name)
        health = proxy["healthcheck"]["test"]
        _docker("exec", name, "/bin/sh", "-c", health[1])  # check=True: must pass
        connect = "CONNECT {0} HTTP/1.1\\r\\nHost: {0}\\r\\n\\r\\n"
        for target, status in (
            ("example.com:443", " 200 "),  # any public host, on 443
            ("example.com:80", " 403 "),
            ("10.214.14.10:443", " 403 "),  # jarvis_internal's Postgres address
            ("169.254.169.254:443", " 403 "),  # cloud metadata
            ("100.100.100.100:443", " 403 "),  # the tailnet range
            ("127.0.0.1:443", " 403 "),
        ):
            answer = _ask(network, proxy["image"], name, connect.format(target))
            assert status in answer, f"{target}: {answer!r}"
        plain = "GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n"
        assert " 403 " in _ask(network, proxy["image"], name, plain)  # CONNECT only
        time.sleep(1)
        logs = _docker("logs", name, check=False).stdout
        assert "TCP_TUNNEL/200" in logs and "CONNECT example.com:443" in logs, logs
        assert "TCP_DENIED/403" in logs and "169.254.169.254:443" in logs, logs
    finally:
        _docker("rm", "-f", name, check=False)


def _start_claude_proxy(
    proxy: dict[str, Any], network: str, key: Path
) -> tuple[str, subprocess.CompletedProcess[str]]:
    name = f"jarvis-claude-proxy-{uuid.uuid4().hex[:8]}"
    args = _run_args(proxy, name, network)
    args += ["-v", f"{key}:/run/secrets/improve_claude_key:ro"]
    return name, _docker(*args, proxy["image"], *proxy["entrypoint"][1:], check=False)


@live
def test_claude_proxy_adds_the_key_and_reaches_claude(
    rendered: dict[str, Any], network: str, tmp_path: Path
) -> None:
    """With a dummy key, Claude answers 401: the request went out over verified TLS to
    api.anthropic.com with our x-api-key. Paths outside /v1/ are not forwarded."""
    proxy = rendered["claude-proxy"]
    key = tmp_path / "improve_claude_key"
    key.write_text("sk-ant-dummy-not-a-real-key\n")
    key.chmod(0o444)
    name, _ = _start_claude_proxy(proxy, network, key)
    try:
        time.sleep(3)
        _wait_running(name)
        _docker("exec", name, "/bin/sh", "-c", proxy["healthcheck"]["test"][1])  # must pass
        conf = _docker("exec", name, "cat", "/tmp/claude-key.conf").stdout  # noqa: S108
        assert conf == 'proxy_set_header x-api-key "sk-ant-dummy-not-a-real-key";\n'

        def status(path: str) -> str:
            # The client's own (placeholder) key must not matter: the proxy replaces it.
            wget = (
                "wget -S -O /dev/null --header 'anthropic-version: 2023-06-01'"
                f" --header 'x-api-key: placeholder-not-a-key' http://{name}:8080{path}"
            )
            done = _docker(
                "run", "--rm", "--network", network, "--entrypoint", "/bin/sh", proxy["image"],
                "-c", f"{wget} 2>&1 || true", check=False,
            )  # fmt: skip
            return done.stdout

        assert "401" in status("/v1/models")
        assert "404" in status("/")
        assert "200" in status("/healthz")
    finally:
        _docker("rm", "-f", name, check=False)


@live
def test_claude_proxy_refuses_a_key_with_other_characters(
    rendered: dict[str, Any], network: str, tmp_path: Path
) -> None:
    proxy = rendered["claude-proxy"]
    key = tmp_path / "improve_claude_key"
    key.write_text('sk-ant"; proxy_pass http://elsewhere;\n')
    key.chmod(0o444)
    name, _ = _start_claude_proxy(proxy, network, key)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = _docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip()
            if state == "exited":
                break
            time.sleep(0.5)
        assert _docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip() == "1"
        logs = _docker("logs", name, check=False)
        assert "improve_claude_key is empty or not a key; refusing to start" in logs.stderr
    finally:
        _docker("rm", "-f", name, check=False)


@live
def test_sandbox_db_starts_read_only_as_postgres(
    rendered: dict[str, Any], internal_network: str
) -> None:
    db = rendered["sandbox-db"]
    name = f"jarvis-sandbox-db-{uuid.uuid4().hex[:8]}"
    try:
        _docker(*_run_args(db, name, internal_network), db["image"])
        deadline = time.monotonic() + 60
        ready = None
        while time.monotonic() < deadline:
            _wait_running(name)
            ready = _docker(
                "exec", name, "/bin/sh", "-c", db["healthcheck"]["test"][1], check=False
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        assert ready is not None and ready.returncode == 0, _docker("logs", name, check=False)
    finally:
        _docker("rm", "-f", name, check=False)
