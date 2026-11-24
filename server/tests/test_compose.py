"""The deployable stack (plan Task 14; spec §4.2, §4.3; P2-S5 and P2-S7 static checks).

`deploy/compose.yaml` is the only file the server fetches, so the sidecar's entrypoint and the
proxy's config travel inside it as inline blocks (x-gateway-ts-entrypoint, x-egress-proxy-squid),
passed to their read-only containers as environment values and written to /tmp at start: Compose
can place only `file` configs in a read-only container (first deploy, 24 Nov 2026). Their
reviewable copies are `deploy/gateway-ts/entrypoint.sh` and `deploy/egress-proxy/squid.conf`;
the inline copies must match them (compose escapes `$` as `$$`).

The init smoke (`jarvis-core smoke`) runs database-only Phase 2 rows: no node, no gateway
process, no brain. Everything here is fictional.
"""

from __future__ import annotations

import ipaddress
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
    assert gateway["user"] == "10001:10001"  # the UID the sidecar's rules and the image use (S-2)
    assert _secrets(gateway) == {"pg_gateway_password", "gateway_signing_key"}
    assert gateway["volumes"] == ["ts_socket:/ts-socket:ro"]
    assert gateway["environment"]["JARVIS_TS_SOCKET"] == "/ts-socket/tailscaled.sock"
    assert gateway["depends_on"]["gateway-ts"] == {"condition": "service_healthy"}


def test_gateway_reaches_postgres_by_its_fixed_address(stack: dict[str, Any]) -> None:
    """R12-33: no DNS for the gateway. Postgres has a fixed address on jarvis_internal, outside
    the range Docker hands out, and the gateway's DSN and the sidecar's rules both use it."""
    services = _services(stack)
    ipam = stack["networks"]["jarvis_internal"]["ipam"]["config"]
    assert len(ipam) == 1
    subnet = ipaddress.ip_network(ipam[0]["subnet"])
    dynamic = ipaddress.ip_network(ipam[0]["ip_range"])
    pg = services["postgres"]["networks"]["jarvis_internal"]["ipv4_address"]
    address = ipaddress.ip_address(pg)
    assert address in subnet and address not in dynamic and dynamic.subnet_of(subnet)
    assert subnet.is_private
    assert services["gateway"]["environment"]["JARVIS_DB_HOST"] == pg
    assert services["gateway-ts"]["environment"]["JARVIS_POSTGRES_IP"] == pg
    for name in ("init", "core", "scheduler"):  # the others still use the name
        assert services[name]["environment"]["JARVIS_DB_HOST"] == "postgres", name


def test_sidecar_runs_tailscale_in_kernel_mode(stack: dict[str, Any]) -> None:
    ts = _services(stack)["gateway-ts"]
    assert ts["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert ts["cap_drop"] == ["ALL"] and ts["cap_add"] == ["NET_ADMIN", "NET_RAW"]
    assert ts["read_only"] is True and "/run" in ts["tmpfs"]
    assert _networks(ts) == ["jarvis_internal", "jarvis_ts_egress"]
    assert _volume_sources(ts) == {"gateway_ts_state", "ts_socket"}
    assert _secrets(ts) == {"tailscale_gateway_authkey"}
    env = ts["environment"]
    assert env["TS_USERSPACE"] == "false" and env["TS_HOSTNAME"] == "jarvis-gw"
    assert env["TS_EXTRA_ARGS"] == "--advertise-tags=tag:jarvis-gateway"
    assert env["TS_AUTH_ONCE"] == "true"  # a non-reusable key is used once; state keeps it
    assert env["TS_SOCKET"] == "/ts-socket/tailscaled.sock"
    assert ts["entrypoint"][:2] == ["/bin/sh", "-c"]
    start = ts["entrypoint"][2]
    assert '[ -n "$${JARVIS_TS_ENTRYPOINT:-}" ] || {' in start and "exit 1; }" in start
    assert start.endswith("exec /bin/sh /tmp/gateway-ts-entrypoint.sh")
    assert env["JARVIS_TS_ENTRYPOINT"] == stack["x-gateway-ts-entrypoint"]
    assert "configs" not in ts
    health = " ".join(ts["healthcheck"]["test"])  # healthy only with both rule jumps in place
    jump = "OUTPUT -m owner --socket-exists ! --uid-owner 0 -j JARVIS_GW_OUT"
    assert f"iptables -C {jump}" in health and f"ip6tables -C {jump}" in health
    assert "--uid-owner 10001" not in health
    assert "JARVIS_GATEWAY_UID" not in env  # the rules no longer name one UID (S-2)


def test_core_has_no_egress_network(stack: dict[str, Any]) -> None:
    """P2-S7 static: core is on the internal network only, and reaches Claude by the proxy."""
    services = _services(stack)
    core = services["core"]
    assert _networks(core) == ["jarvis_internal"]
    assert core["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert stack["networks"]["jarvis_internal"]["internal"] is True
    assert core["depends_on"]["egress-proxy"] == {"condition": "service_healthy"}
    proxy = services["egress-proxy"]
    assert _networks(proxy) == ["jarvis_egress", "jarvis_internal"]
    assert "configs" not in proxy
    assert proxy["entrypoint"][:2] == ["/bin/sh", "-c"] and proxy["command"] == []
    start = proxy["entrypoint"][2]
    assert '[ -n "$${JARVIS_SQUID_CONF:-}" ] || {' in start and "exit 1; }" in start
    assert start.endswith("exec /usr/sbin/squid -f /tmp/squid.conf -NYC")
    assert proxy["environment"]["JARVIS_SQUID_CONF"] == stack["x-egress-proxy-squid"]
    assert "/tmp" in proxy["tmpfs"]  # noqa: S108 (a container path, not ours)
    assert proxy["cap_drop"] == ["ALL"] and proxy["cap_add"] == ["SETUID", "SETGID", "CHOWN"]
    assert proxy["read_only"] is True
    # Healthy only with our config mounted, so a lost mount can't fall back to the default.
    health = " ".join(proxy["healthcheck"]["test"])
    assert "acl claude dstdomain -n api.anthropic.com' /tmp/squid.conf" in health
    assert "http_access deny all' /tmp/squid.conf" in health
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


def test_no_inline_configs_in_read_only_services(stack: dict[str, Any]) -> None:
    """Compose refuses an inline (`content`) config in a read-only service: only `file` configs
    can be placed there. The first deploy failed on exactly this (24 Nov 2026)."""
    assert "configs" not in stack
    for name, service in _services(stack).items():
        assert "configs" not in service, name


@pytest.mark.parametrize(
    ("block", "path"),
    [("x-gateway-ts-entrypoint", ENTRYPOINT), ("x-egress-proxy-squid", SQUID)],
)
def test_inline_copies_match_their_files(stack: dict[str, Any], block: str, path: Path) -> None:
    raw = COMPOSE.read_text()  # yaml loads the escaped text; compose turns $$ into $
    inline = stack[block]
    assert f"\n{block}: &" in raw
    assert inline.replace("$$", "$") == path.read_text()
    assert "$" not in re.sub(r"\$\$", "", inline)  # every $ escaped from compose


def test_sidecar_entrypoint_blocks_gateway_egress() -> None:
    """§4.2, R12-33: UID 10001 may open only TCP to Postgres's fixed address on 5432, and send
    only established or related packets on tailscale0. Nothing else: no loopback, no DNS, no
    other internal host or port, no new tailnet connection. IPv6 alike, failing closed when it
    is on and can't be ruled. Then containerboot. Final review S-2: the rules hold for every
    socket not owned by root (tailscaled), so a gateway run under any other UID is still
    closed; packets with no owning socket are left as before."""
    script = ENTRYPOINT.read_text()
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)
    lines = [ln.strip() for ln in script.splitlines()]
    code = [ln for ln in lines if ln and not ln.startswith("#")]
    assert "set -eu" in lines
    assert lines[-1] == "exec /usr/local/bin/containerboot"
    rules = [ln for ln in lines if ln.startswith('iptables -A "$CHAIN"')]
    assert rules == [
        'iptables -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
        'iptables -A "$CHAIN" -d "$PG_IP" -p tcp --dport 5432 -j ACCEPT',
        'iptables -A "$CHAIN" -j DROP',
    ]
    v6 = [ln for ln in lines if ln.startswith('ip6tables -A "$CHAIN"')]
    assert v6 == [
        'ip6tables -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
        'ip6tables -A "$CHAIN" -j DROP',
    ]
    jump = '-m owner --socket-exists ! --uid-owner 0 -j "$CHAIN"'
    for tool in ("iptables", "ip6tables"):
        assert f"{tool} -I OUTPUT 1 {jump}" in script
        assert f"{tool} -C OUTPUT {jump}" in script
    assert "GW_UID" not in script and "--uid-owner 10001" not in script
    assert not [ln for ln in code if "--uid-owner" in ln and "! --uid-owner 0" not in ln]
    assert 'PG_IP="${JARVIS_POSTGRES_IP:-}"' in lines
    # No name lookups, no loopback: nothing that needs or allows DNS.
    for word in ("getent", "nslookup", " lo ", "-o lo", "127.0.0", "::1", "postgres "):
        assert not any(word in ln for ln in code), word
    assert 'fail "IPv6 is on but ip6tables does not work"' in script


@pytest.mark.parametrize("value", ["", "postgres", "10.0.0.1; reboot"])
def test_sidecar_refuses_a_bad_postgres_address(value: str) -> None:
    """The entrypoint stops before touching any rule unless JARVIS_POSTGRES_IP is an address."""
    done = subprocess.run(
        [shutil.which("sh") or "/bin/sh", str(ENTRYPOINT)],
        env={"PATH": "/nonexistent", "JARVIS_POSTGRES_IP": value},  # no iptables reachable
        capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 1
    assert "JARVIS_POSTGRES_IP is not an IPv4 address; refusing to start" in done.stderr


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
    """`harden.sh --secrets-only`: the passwords and the two Ed25519 keys (PEM) plus their raw
    32-byte public keys, all root:10001 0440; never a private key on its output; a second run
    keeps everything; and the override directory's parent is left exactly as it was."""
    tmp_path.chmod(0o755)
    parent_before = tmp_path.stat()
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
                 "core_signing_key.pub", "gateway_signing_key.pub"}  # fmt: skip
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

    for name in ("core_signing_key", "gateway_signing_key"):
        key = load_private_key(made[name])
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert made[f"{name}.pub"].read_bytes() == raw
    st = secrets.stat()
    assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (0, 10001, 0o750)
    after = tmp_path.stat()  # M4: never the parent of an override
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        parent_before.st_uid,
        parent_before.st_gid,
        parent_before.st_mode,
    )
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


# --- first-deploy keys: init records them (M8) -------------------------------------------------


def _pub_secrets(directory: Path) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from jarvis_server.signing import public_bytes

    core, gw = (public_bytes(Ed25519PrivateKey.generate()) for _ in range(2))
    (directory / "core_signing_key.pub").write_bytes(core)
    (directory / "gateway_signing_key.pub").write_bytes(gw)
    return core, gw


def _trusted(conn: Conn) -> list[tuple[str, str, bytes]]:
    rows = conn.execute(
        "SELECT role, state, public_key FROM gw.trusted_key ORDER BY role, state"
    ).fetchall()
    return [(str(r[0]), str(r[1]), bytes(r[2])) for r in rows]


def test_init_records_the_bootstrap_keys_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The first deploy's init records core_signing_key.pub and gateway_signing_key.pub as the
    ACTIVE keys; a later deploy changes nothing; after a rotation the refusal
    (`active_key_differs`) is logged and the deploy goes on."""
    import logging

    core, gw = _pub_secrets(tmp_path)
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    with fresh_database() as d:
        monkeypatch.setattr(config, "app_dsn", lambda: d.app)
        core_main.record_bootstrap_keys()
        with psycopg.connect(d.app, autocommit=True) as conn:
            first = _trusted(conn)
            assert first == [("CORE", "ACTIVE", core), ("GATEWAY", "ACTIVE", gw)]
            core_main.record_bootstrap_keys()  # every later deploy: idempotent
            assert _trusted(conn) == first
        with psycopg.connect(d.owner, autocommit=True) as owner:  # a rotated core key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            from jarvis_server.signing import fingerprint, public_bytes

            new = public_bytes(Ed25519PrivateKey.generate())
            owner.execute("UPDATE gw.trusted_key SET state = 'RETIRED' WHERE role = 'CORE'")
            owner.execute(
                "INSERT INTO gw.trusted_key (key_id, role, public_key, state, activated_at)"
                " VALUES (%s, 'CORE', %s, 'ACTIVE', now())",
                (fingerprint(new), new),
            )
            rotated = _trusted(owner)
        with caplog.at_level(logging.INFO, logger="jarvis.core"):
            core_main.record_bootstrap_keys()  # refused, and not a failure
        warned = [r for r in caplog.records if "active_key_differs" in r.getMessage()]
        assert [r.levelno for r in warned] == [logging.WARNING]  # one line: the CORE key only
        message = warned[0].getMessage()
        assert f"ACTIVE CORE key {fingerprint(new)} is not core_signing_key.pub" in message
        assert "key rotation" in message and "restored onto a re-hardened server" in message
        with psycopg.connect(d.app, autocommit=True) as conn:
            assert _trusted(conn) == rotated


def test_init_runs_the_key_step_and_needs_the_public_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(core_main, "_bootstrap", lambda: called.append("bootstrap"))
    monkeypatch.setattr(core_main.migrate, "upgrade", lambda _dsn: called.append("migrate"))
    monkeypatch.setattr(config, "owner_dsn", lambda: "owner")
    monkeypatch.setattr(core_main, "record_bootstrap_keys", lambda: called.append("keys"))
    monkeypatch.setenv("JARVIS_SMOKE_ON_INIT", "0")
    assert core_main.main(["init"]) == 0
    assert called == ["bootstrap", "migrate", "keys"]
    monkeypatch.undo()
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)  # no .pub files: init fails, loudly
    with pytest.raises(SystemExit, match=r"core_signing_key\.pub is missing"):
        core_main.record_bootstrap_keys()


def test_compose_gives_init_the_public_keys_only(stack: dict[str, Any]) -> None:
    init = _services(stack)["init"]
    pubs = sorted((s["source"], s["target"]) for s in init["secrets"] if isinstance(s, dict))
    assert pubs == [("core_signing_key_pub", "core_signing_key.pub"),
                    ("gateway_signing_key_pub", "gateway_signing_key.pub")]  # fmt: skip
    assert stack["secrets"]["core_signing_key_pub"] == {
        "file": "/etc/jarvis/secrets/core_signing_key.pub"
    }
    assert {"core_signing_key", "gateway_signing_key"}.isdisjoint(_secrets(init))


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
