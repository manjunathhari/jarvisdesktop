"""The deployable stack (plan Task 14; spec §4.2, §4.3; P2-S5 and P2-S7 static checks).

`deploy/compose.yaml` is the only file the server fetches, so the sidecar's entrypoint and the
proxy's config travel inside it as inline `configs`. Their reviewable copies are
`deploy/gateway-ts/entrypoint.sh` and `deploy/egress-proxy/squid.conf`; the inline copies must
match them (compose escapes `$` as `$$`).

The init smoke (`jarvis-core smoke`) runs database-only Phase 2 rows: no node, no gateway
process, no brain. Everything here is fictional.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from jarvis_server import config, core_main, smoke

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]
SERVER = Path(__file__).resolve().parents[1]
DEPLOY = SERVER / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
ENTRYPOINT = DEPLOY / "gateway-ts" / "entrypoint.sh"
SQUID = DEPLOY / "egress-proxy" / "squid.conf"
HARDEN = DEPLOY / "harden" / "harden.sh"

TAILSCALE_IMAGE = (
    "tailscale/tailscale:stable"
    "@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c"
)
SQUID_IMAGE = (
    "ubuntu/squid:6.6-24.04_edge"
    "@sha256:d9c34c5ab74c6dd41cf1070d9b4221aac072e67e983527ca2d41512958f77506"
)
P2_ROWS = ["P2-E01", "P2-E04", "P2-E06", "P2-E11", "P2-S1", "P2-S6-db", "P2-B-idx", "P2-fence"]


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    with COMPOSE.open() as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
    return loaded


def _services(stack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return stack["services"]  # type: ignore[no-any-return]


def _secrets(service: dict[str, Any]) -> set[str]:
    return {s if isinstance(s, str) else s["source"] for s in service.get("secrets", [])}


def _volume_sources(service: dict[str, Any]) -> set[str]:
    return {v.split(":", 1)[0] for v in service.get("volumes", [])}


def _networks(service: dict[str, Any]) -> list[str]:
    networks = service.get("networks", [])
    return sorted(networks if isinstance(networks, list) else networks.keys())


# --- the stack ---------------------------------------------------------------------------------


def test_no_service_publishes_ports(stack: dict[str, Any]) -> None:
    """P2-S5 static (and P0-8): no host port, ever."""
    for name, service in _services(stack).items():
        assert "ports" not in service, name
    assert not re.search(r"^\s*ports\s*:", COMPOSE.read_text(), re.MULTILINE)


def test_gateway_shares_sidecar_namespace(stack: dict[str, Any]) -> None:
    gateway = _services(stack)["gateway"]
    assert gateway["network_mode"] == "service:gateway-ts"
    assert "networks" not in gateway  # it has only the sidecar's interfaces
    assert gateway["entrypoint"] == ["jarvis-gateway"] and "profiles" not in gateway
    assert _secrets(gateway) == {"pg_gateway_password", "gateway_signing_key"}
    assert gateway["volumes"] == ["ts_socket:/var/run/tailscale:ro"]
    assert gateway["environment"]["JARVIS_TS_SOCKET"] == "/var/run/tailscale/tailscaled.sock"
    assert gateway["depends_on"]["gateway-ts"] == {"condition": "service_healthy"}


def test_sidecar_runs_tailscale_in_kernel_mode(stack: dict[str, Any]) -> None:
    ts = _services(stack)["gateway-ts"]
    assert ts["devices"] == ["/dev/net/tun:/dev/net/tun"] and ts["cap_add"] == ["NET_ADMIN"]
    assert _networks(ts) == ["jarvis_internal", "jarvis_ts_egress"]
    assert _volume_sources(ts) == {"gateway_ts_state", "ts_socket"}
    assert _secrets(ts) == {"tailscale_gateway_authkey"}
    env = ts["environment"]
    assert env["TS_USERSPACE"] == "false" and env["TS_HOSTNAME"] == "jarvis-gw"
    assert env["TS_EXTRA_ARGS"] == "--advertise-tags=tag:jarvis-gateway"
    assert env["TS_AUTH_ONCE"] == "true"  # a non-reusable key is used once; state keeps it
    assert env["TS_SOCKET"] == "/var/run/tailscale/tailscaled.sock"
    assert ts["entrypoint"] == ["/bin/sh", "/etc/jarvis/gateway-ts-entrypoint.sh"]
    assert ts["configs"] == [
        {"source": "gateway_ts_entrypoint", "target": "/etc/jarvis/gateway-ts-entrypoint.sh"}
    ]
    assert "JARVIS_GW_OUT" in " ".join(ts["healthcheck"]["test"])  # healthy only with the rules


def test_core_has_no_egress_network(stack: dict[str, Any]) -> None:
    """P2-S7 static: core is on the internal network only, and reaches Claude by the proxy."""
    services = _services(stack)
    core = services["core"]
    assert _networks(core) == ["jarvis_internal"]
    assert core["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert stack["networks"]["jarvis_internal"] == {"internal": True}
    proxy = services["egress-proxy"]
    assert _networks(proxy) == ["jarvis_egress", "jarvis_internal"]
    assert proxy["configs"] == [{"source": "egress_proxy_squid", "target": "/etc/squid/squid.conf"}]
    egress = {"jarvis_egress", "jarvis_ts_egress"}
    for name, service in services.items():  # only the sidecar and the proxy reach the internet
        if name not in ("gateway-ts", "egress-proxy", "backup", "postgres"):
            assert egress.isdisjoint(_networks(service)), name
    for name in ("backup", "postgres", "init", "scheduler", "workers"):
        assert _networks(services[name]) == ["jarvis_internal"], name


def test_only_core_mounts_memory_and_api_key(stack: dict[str, Any]) -> None:
    for name, service in _services(stack).items():
        memory = {"memory", "memory_backups"} & _volume_sources(service)
        assert (name == "core") == bool(memory), name
        assert (name == "core") == ("anthropic_api_key" in _secrets(service)), name
        assert (name == "core") == ("core_signing_key" in _secrets(service)), name


def test_core_and_init_secrets(stack: dict[str, Any]) -> None:
    services = _services(stack)
    # core: Claude, its signing key, and the gateway's public key for `keys init`.
    assert _secrets(services["core"]) == {
        "pg_app_password", "anthropic_api_key", "core_signing_key", "gateway_signing_key_pub"
    }  # fmt: skip
    pub = [s for s in services["core"]["secrets"] if isinstance(s, dict)]
    assert pub == [{"source": "gateway_signing_key_pub", "target": "gateway_signing_key.pub"}]
    assert "pg_gateway_password" in _secrets(services["init"])  # bootstrap and the P2 smoke
    files = {k: v["file"] for k, v in stack["secrets"].items()}
    assert all(f.startswith("/etc/jarvis/secrets/") for f in files.values())
    assert files["gateway_signing_key_pub"] == "/etc/jarvis/secrets/gateway_signing_key.pub"
    used = set().union(*(_secrets(s) for s in services.values()))
    assert used == set(files)


def test_all_third_party_images_pinned(stack: dict[str, Any]) -> None:
    services = _services(stack)
    assert services["gateway-ts"]["image"] == TAILSCALE_IMAGE
    assert services["egress-proxy"]["image"] == SQUID_IMAGE
    for name in ("gateway-ts", "egress-proxy"):
        assert re.search(r"@sha256:[0-9a-f]{64}$", services[name]["image"]), name


# --- the sidecar entrypoint and the proxy config -----------------------------------------------


@pytest.mark.parametrize(
    ("config_name", "path"), [("gateway_ts_entrypoint", ENTRYPOINT), ("egress_proxy_squid", SQUID)]
)
def test_inline_configs_match_their_files(
    stack: dict[str, Any], config_name: str, path: Path
) -> None:
    inline = stack["configs"][config_name]["content"]
    assert inline.replace("$$", "$") == path.read_text()
    assert "$" not in re.sub(r"\$\$", "", inline)  # every $ escaped from compose


def test_sidecar_entrypoint_blocks_gateway_egress() -> None:
    """§4.2: UID 10001 may leave only on tailscale0, to the jarvis_internal subnet, or to
    loopback (Docker's DNS); everything else from it is dropped. Then containerboot."""
    script = ENTRYPOINT.read_text()
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)
    lines = [ln.strip() for ln in script.splitlines()]
    assert "set -eu" in lines
    assert lines[-1] == "exec /usr/local/bin/containerboot"
    rules = [ln for ln in lines if ln.startswith('iptables -A "$CHAIN"')]
    assert rules == [
        'iptables -A "$CHAIN" -o lo -d 127.0.0.0/8 -j ACCEPT',
        'iptables -A "$CHAIN" -o tailscale0 -j ACCEPT',
        'iptables -A "$CHAIN" -o "$dev" -d "$subnet" -j ACCEPT',
        'iptables -A "$CHAIN" -j DROP',
    ]
    assert '-m owner --uid-owner "$GW_UID" -j "$CHAIN"' in script
    assert 'GW_UID="${JARVIS_GATEWAY_UID:-10001}"' in lines
    v6 = [ln for ln in lines if ln.startswith('ip6tables -A "$CHAIN"')]
    assert v6[-1] == 'ip6tables -A "$CHAIN" -j DROP' and len(v6) == 3


def test_proxy_allows_only_connect_to_claude() -> None:
    directives = [
        ln.split()
        for ln in SQUID.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    access = [d[1:] for d in directives if d[0] == "http_access"]
    assert access == [
        ["allow", "jarvis_internal", "connect_method", "claude", "tls_port"],
        ["deny", "all"],
    ]
    acls = {d[1]: d[2:] for d in directives if d[0] == "acl"}
    assert acls["claude"] == ["dstdomain", "-n", "api.anthropic.com"]  # no reverse lookups
    assert acls["tls_port"] == ["port", "443"]
    assert acls["connect_method"] == ["method", "CONNECT"]
    assert ["cache", "deny", "all"] in directives
    assert not any(d[0] in ("cache_dir", "cache_peer") for d in directives)


# --- harden.sh ---------------------------------------------------------------------------------


def test_harden_script_is_strict() -> None:
    subprocess.run(["bash", "-n", str(HARDEN)], check=True)
    assert "set -euo pipefail" in HARDEN.read_text().splitlines()
    assert os.access(HARDEN, os.X_OK)


@pytest.mark.skipif(os.geteuid() != 0, reason="harden.sh sets root:10001 ownership")
def test_harden_secrets_step(tmp_path: Path) -> None:
    """`harden.sh --secrets-only`: the passwords and the two Ed25519 keys (PEM) plus the raw
    32-byte gateway public key, all root:10001 0440; never a private key on its output; and a
    second run keeps everything."""
    secrets = tmp_path / "secrets"
    env = {**os.environ, "JARVIS_HARDEN_SECRETS_DIR": str(secrets)}

    def run() -> str:
        done = subprocess.run(
            ["bash", str(HARDEN), "--secrets-only"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
        )  # fmt: skip
        return done.stdout + done.stderr

    out = run()
    made = {p.name: p for p in secrets.iterdir()}
    generated = {"pg_superuser_password", "pg_owner_password", "pg_app_password",
                 "pg_gateway_password", "core_signing_key", "gateway_signing_key",
                 "gateway_signing_key.pub"}  # fmt: skip
    assert set(made) == generated  # the provided secrets are not invented
    for p in made.values():
        st = p.stat()
        assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (0, 10001, 0o440), p.name
    for name in ("core_signing_key", "gateway_signing_key"):
        pem = made[name].read_text()
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert pem.strip().splitlines()[1] not in out  # the key body never printed
    assert "PRIVATE KEY" not in out
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from jarvis_server.signing import load_private_key

    gw = load_private_key(made["gateway_signing_key"])
    raw = gw.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert made["gateway_signing_key.pub"].read_bytes() == raw
    assert "tailscale_gateway_authkey" in out and "anthropic_api_key" in out  # told to place
    before = {n: p.read_bytes() for n, p in made.items()}
    again = run()
    assert {n: p.read_bytes() for n, p in made.items()} == before
    assert "kept existing core_signing_key" in again and "PRIVATE KEY" not in again


# --- the init smoke (database-only Phase 2 rows) -----------------------------------------------


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


def test_smoke_lists_phase2_rows(app: Conn, gateway: Conn) -> None:
    """Exactly the database-only rows, all passing, twice in a row (every deploy runs them)."""
    for _ in range(2):
        results = smoke.run_phase2(app, gateway)
        assert [name.split()[0] for name, _, _ in results] == P2_ROWS
        assert [name for name, ok, _ in results if not ok] == [], results
    # It leaves no live smoke state: nodes revoked, connections closed, items terminal.
    assert app.execute(
        "SELECT count(*) FROM gw.node n JOIN ledger.person p ON p.id = n.person_id"
        " WHERE p.source_ref = 'smoke-check' AND n.revoked_at IS NULL"
    ).fetchone() == (0,)
    assert app.execute(
        "SELECT count(*) FROM comm.outbox o JOIN ledger.person p ON p.id = o.person_id"
        " WHERE p.source_ref = 'smoke-check'"
        " AND o.state NOT IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED')"
    ).fetchone() == (0,)
    assert app.execute(
        "SELECT count(*) FROM ledger.approval WHERE summary LIKE 'Smoke%'"
    ).fetchone() == (0,)  # the expired approval was rolled back
    assert app.execute(
        "SELECT count(*) FROM ops.job WHERE status IN ('QUEUED', 'RUNNING')"
        " AND kind = 'system.event'"
    ).fetchone() == (0,)


def test_a_failing_smoke_row_is_a_fail_line(
    app: Conn, gateway: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that raises reports FAIL with the error; the other rows run, and the tidy-up still
    revokes the smoke node."""

    def broken(_: Any) -> tuple[bool, str]:
        raise RuntimeError("fictional failure")

    checks = list(smoke.P2_CHECKS)
    checks[1] = (checks[1][0], broken)
    monkeypatch.setattr(smoke, "P2_CHECKS", checks)
    results = smoke.run_phase2(app, gateway)
    assert [(n.split()[0], ok) for n, ok, _ in results] == [
        (row, row != "P2-E04") for row in P2_ROWS
    ]
    assert results[1][2] == "RuntimeError: fictional failure"
    assert app.execute(
        "SELECT count(*) FROM gw.node n JOIN ledger.person p ON p.id = n.person_id"
        " WHERE p.source_ref = 'smoke-check' AND n.revoked_at IS NULL"
    ).fetchone() == (0,)


def test_smoke_prints_the_phase2_rows(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`jarvis-core smoke` (and init) prints one SMOKE PASS line per P2 row, after P0 and M."""
    monkeypatch.setattr(config, "app_dsn", lambda: dsns.app)
    monkeypatch.setattr(config, "gateway_dsn", lambda: dsns.gateway)
    assert core_main.main(["smoke"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("SMOKE ")]
    p2 = [ln.split()[2] for ln in lines if ln.split()[2].startswith("P2-")]
    assert p2 == P2_ROWS
    assert all(ln.startswith("SMOKE PASS") for ln in lines)
    assert lines[0].split()[2] == "P0-1" and lines[-1].split()[2] == "P2-fence"


def test_smoke_imports_no_gateway_or_brain() -> None:
    """In a fresh interpreter, importing the smoke loads neither the gateway nor the brain."""
    code = (
        "import sys, jarvis_server.smoke\n"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('jarvis_server.gateway', 'jarvis_server.brain', 'httpx', 'fastapi')))\n"
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


# --- CI ----------------------------------------------------------------------------------------


def test_ci_fails_on_published_ports() -> None:
    workflow = (SERVER.parent / ".github" / "workflows" / "server.yml").read_text()
    step = workflow.split("- name: No service publishes a port", 1)[1].split("- name:", 1)[0]
    assert "docker compose -f deploy/compose.yaml" in step and "config" in step
    assert "ports:" in step and "exit 1" in step


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_compose_config_is_valid() -> None:
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "*", "config"],
        env={**os.environ, "JARVIS_REF": "test"}, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert not re.search(r"^\s*ports\s*:", done.stdout, re.MULTILINE)
    assert "api.anthropic.com" in done.stdout and "JARVIS_GW_OUT" in done.stdout
