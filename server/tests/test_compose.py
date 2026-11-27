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
IMPROVE = DEPLOY / "improve"
IMPROVE_SQUID = IMPROVE / "squid.conf"
CLAUDE_NGINX = IMPROVE / "claude-proxy.conf"
SANDBOX_DOCKERFILE = IMPROVE / "sandbox.Dockerfile"
WORKFLOWS = SERVER.parent / ".github" / "workflows"

TAILSCALE_IMAGE = (
    "tailscale/tailscale:stable"
    "@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c"
)
SQUID_IMAGE = (
    "ubuntu/squid:6.6-24.04_edge"
    "@sha256:d9c34c5ab74c6dd41cf1070d9b4221aac072e67e983527ca2d41512958f77506"
)
NGINX_IMAGE = (
    "nginxinc/nginx-unprivileged:1.30.6-alpine"
    "@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03"
)
SANDBOX_DB_IMAGE = (
    "postgres:17.12-trixie@sha256:97fb53376be3e4fb1a8a61421daa69dcc3bf2fa39bda3259f5245f9a455af1a9"
)
AGENT_SDK = "claude-agent-sdk==0.2.189"
IMPROVE_SERVICES = {"improver", "sandbox", "claude-proxy", "improve-egress", "sandbox-db"}
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
    assert "read -r IPT IP6T < /tmp/jarvis-iptables && " in health  # the chosen backend
    assert f"$$IPT -C {jump}" in health and f"$$IP6T -C {jump}" in health
    assert '[ -z "$$IP6T" ] ||' in health  # no IPv6 backend: only when IPv6 is off
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
    [
        ("x-gateway-ts-entrypoint", ENTRYPOINT),
        ("x-egress-proxy-squid", SQUID),
        ("x-improve-egress-squid", IMPROVE_SQUID),
        ("x-claude-proxy-nginx", CLAUDE_NGINX),
    ],
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
    rules = [ln for ln in lines if ln.startswith('"$IPT" -A "$CHAIN"')]
    assert rules == [
        '"$IPT" -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
        '"$IPT" -A "$CHAIN" -d "$PG_IP" -p tcp --dport 5432 -j ACCEPT',
        '"$IPT" -A "$CHAIN" -j DROP',
    ]
    v6 = [ln for ln in lines if ln.startswith('"$IP6T" -A "$CHAIN"')]
    assert v6 == [
        '"$IP6T" -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
        '"$IP6T" -A "$CHAIN" -j DROP',
    ]
    jump = '-m owner --socket-exists ! --uid-owner 0 -j "$CHAIN"'
    for tool in ('"$IPT"', '"$IP6T"'):
        assert f"{tool} -I OUTPUT 1 {jump}" in script
        assert f"{tool} -C OUTPUT {jump}" in script
    # The backend is the one the kernel offers, nftables first (24 Nov 2026: Ubuntu 24.04 has
    # no legacy tables), and the health check reads the same choice.
    assert 'for b in "$1-nft" "$1-legacy" "$1"; do' in lines
    assert 'IPT="$(pick iptables)" || fail "no working iptables backend (nft or legacy)"' in lines
    assert 'IP6T="$(pick ip6tables)" || IP6T=""' in lines
    assert 'printf \'%s %s\\n\' "$IPT" "$IP6T" > /tmp/jarvis-iptables' in lines
    assert not [ln for ln in code if ln.startswith(("iptables ", "ip6tables "))]
    # tailscaled's own rules use the same backend (re-review I-1).
    assert "*-nft) TS_DEBUG_FIREWALL_MODE=nftables ;;" in lines
    assert "*) TS_DEBUG_FIREWALL_MODE=iptables ;;" in lines
    assert "export TS_DEBUG_FIREWALL_MODE" in lines
    assert code.index("export TS_DEBUG_FIREWALL_MODE") < code.index(
        "exec /usr/local/bin/containerboot"
    )
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


# --- the self-improvement team (profile "improve") ---------------------------------------------
#
# Five services, none started by default: the orchestrator (improver), the sandbox the Claude
# Agent SDK works in, the proxy that adds the team's own Claude key (claude-proxy), a squid for
# the team's web access (improve-egress) and a throwaway Postgres for the sandbox's DB tests
# (sandbox-db). The sandbox holds no key of any kind.


def _directives(path: Path) -> list[list[str]]:
    return [
        ln.split()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _members(stack: dict[str, Any], network: str) -> set[str]:
    return {n for n, s in _services(stack).items() if network in _networks(s)}


def test_improve_services_only_in_the_improve_profile(stack: dict[str, Any]) -> None:
    services = _services(stack)
    in_profile = {n for n, s in services.items() if "improve" in s.get("profiles", [])}
    assert in_profile == IMPROVE_SERVICES
    for name in IMPROVE_SERVICES:
        assert services[name]["profiles"] == ["improve"], name
        assert "ports" not in services[name] and "expose" not in services[name], name
        assert "network_mode" not in services[name] and "privileged" not in services[name], name
        assert "devices" not in services[name], name


def test_sandbox_holds_no_secret(stack: dict[str, Any]) -> None:
    sandbox = _services(stack)["sandbox"]
    assert "secrets" not in sandbox
    assert sandbox["volumes"] == ["improve_work:/work"]
    assert not [v for v in sandbox["volumes"] if "/etc/jarvis" in v or "secrets" in v]
    assert _networks(sandbox) == ["jarvis_improve"]
    env = sandbox["environment"]
    for name, value in env.items():
        if "KEY" in name.upper():
            assert (name, value) == ("ANTHROPIC_API_KEY", "placeholder-not-a-key"), name
        assert "sk-ant" not in str(value).lower(), name
    assert env["ANTHROPIC_BASE_URL"] == "http://claude-proxy:8080"
    assert env["HTTPS_PROXY"] == "http://improve-egress:3128"
    assert env["JARVIS_TEST_SUPERUSER_DSN"].startswith("postgresql://postgres:")
    assert env["JARVIS_TEST_SUPERUSER_DSN"].endswith("@sandbox-db:5432/postgres")
    assert sandbox["restart"] == "always"  # each stage starts on a clean root and /tmp
    assert sandbox["read_only"] is True and sandbox["cap_drop"] == ["ALL"]
    assert "cap_add" not in sandbox
    assert sandbox["security_opt"] == ["no-new-privileges:true"]
    assert sandbox["user"] == "10001:10001"
    tmpfs = [t.split(":", 1)[0] for t in sandbox["tmpfs"]]
    assert tmpfs == ["/tmp", "/cache", "/home/jarvis"]  # noqa: S108 (container paths)
    for cache in ("CARGO_HOME", "UV_CACHE_DIR", "npm_config_cache"):  # on the tmpfs, not /work
        assert env[cache].startswith("/cache/"), cache
    assert env["HOME"] == "/home/jarvis"
    assert sandbox["image"].startswith("ghcr.io/manjunathhari/jarvis-sandbox:${JARVIS_REF:?")
    assert sandbox["depends_on"] == {
        "claude-proxy": {"condition": "service_healthy"},
        "improve-egress": {"condition": "service_healthy"},
        "sandbox-db": {"condition": "service_healthy"},
    }


def test_only_claude_proxy_mounts_the_improve_claude_key(stack: dict[str, Any]) -> None:
    for name, service in _services(stack).items():
        assert (name == "claude-proxy") == ("improve_claude_key" in _secrets(service)), name
        assert (name == "improver") == ("improve_github_app_key" in _secrets(service)), name
    proxy = _services(stack)["claude-proxy"]
    assert _secrets(proxy) == {"improve_claude_key"}
    assert not [k for k in proxy["environment"] if "KEY" in k.upper()]  # read from the file
    files = {k: v["file"] for k, v in stack["secrets"].items()}
    assert files["improve_claude_key"] == "/etc/jarvis/secrets/improve_claude_key"
    assert files["improve_github_app_key"] == "/etc/jarvis/secrets/improve_github_app_key"


def test_improver_gets_no_claude_key(stack: dict[str, Any]) -> None:
    services = _services(stack)
    improver = services["improver"]
    # Postgres (its app password) and the GitHub App's private key; never a Claude key.
    assert _secrets(improver) == {"pg_app_password", "improve_github_app_key"}
    assert {"anthropic_api_key", "improve_claude_key"}.isdisjoint(_secrets(improver))
    assert not [k for k in improver["environment"] if "ANTHROPIC" in k or "CLAUDE" in k]
    assert improver["image"] == services["core"]["image"]
    assert improver["entrypoint"] == ["jarvis-improver"] and improver["command"] == ["run"]
    assert _networks(improver) == ["jarvis_improve", "jarvis_internal"]
    assert improver["environment"]["HTTPS_PROXY"] == "http://improve-egress:3128"
    assert improver["environment"]["JARVIS_DB_HOST"] == "postgres"
    assert improver["read_only"] is True and improver["cap_drop"] == ["ALL"]
    assert improver["volumes"] == ["improve_work:/work"]  # writes the inbox, reads the outbox
    assert improver["depends_on"]["improve-egress"] == {"condition": "service_healthy"}
    users = {n for n, s in services.items() if "improve_work" in _volume_sources(s)}
    assert users == {"improver", "sandbox"}
    # Both images own an empty /work as 10001, so whichever container mounts the new volume
    # first, Docker gives the volume to 10001 (sandbox.Dockerfile does the same).
    owned = (SERVER / "Dockerfile").read_text().split("install -d -o 10001 -g 10001", 1)[1]
    assert "/work" in owned.split("\n", 1)[0]


def test_improve_networks_keep_core_closed(stack: dict[str, Any]) -> None:
    """jarvis_improve has no internet route. Only improve-egress and claude-proxy have one, each
    on a network of its own, and neither is on jarvis_internal: core's way out is unchanged."""
    services = _services(stack)
    networks = stack["networks"]
    assert networks["jarvis_improve"]["internal"] is True
    assert _members(stack, "jarvis_improve") == IMPROVE_SERVICES
    assert _members(stack, "jarvis_improve_egress") == {"improve-egress"}
    assert _members(stack, "jarvis_claude_egress") == {"claude-proxy"}
    for name in ("jarvis_improve_egress", "jarvis_claude_egress"):
        assert not networks[name].get("internal"), name
    assert _networks(services["improve-egress"]) == ["jarvis_improve", "jarvis_improve_egress"]
    assert _networks(services["claude-proxy"]) == ["jarvis_claude_egress", "jarvis_improve"]
    assert _networks(services["sandbox-db"]) == ["jarvis_improve"]
    assert _members(stack, "jarvis_internal") & IMPROVE_SERVICES == {"improver"}
    assert _members(stack, "jarvis_egress") == {"egress-proxy"}
    assert _members(stack, "jarvis_ts_egress") == {"gateway-ts"}


def test_improve_images_pinned(stack: dict[str, Any]) -> None:
    services = _services(stack)
    assert services["claude-proxy"]["image"] == NGINX_IMAGE
    assert services["improve-egress"]["image"] == SQUID_IMAGE
    assert services["sandbox-db"]["image"] == SANDBOX_DB_IMAGE
    for name in ("claude-proxy", "improve-egress", "sandbox-db"):
        assert re.search(r"@sha256:[0-9a-f]{64}$", services[name]["image"]), name


def test_sandbox_image_is_pinned_and_keyless() -> None:
    text = SANDBOX_DOCKERFILE.read_text()
    lines = [ln.strip() for ln in text.splitlines()]
    sources = [ln.split()[1] for ln in lines if ln.startswith("FROM ")]
    sources += re.findall(r"COPY --from=(\S+)", text)
    stages = {ln.split()[-1] for ln in lines if ln.startswith("FROM ") and " AS " in ln}
    external = [s for s in sources if s not in stages]
    assert external and all(re.search(r"@sha256:[0-9a-f]{64}$", s) for s in external), external
    assert sources[0].startswith("node:22.") and any(s.startswith("python:3.12.") for s in sources)
    assert f"pip install --no-cache-dir {AGENT_SDK}" in text  # exact, verified on PyPI
    assert "--profile minimal" in text and "--default-toolchain stable" in text
    assert "sha256sum -c" in text  # rustup-init is checked before it runs
    assert (
        'ENTRYPOINT ["/opt/agent-sdk/bin/python", "-m", "jarvis_server.improve.sandbox_runner"]'
        in lines
    )
    assert "COPY src /opt/jarvis/src" in lines
    assert [ln for ln in lines if ln.startswith("USER ")] == ["USER 10001:10001"]
    assert "sk-ant" not in text and not re.search(r"^ENV .*KEY", text, re.MULTILINE)
    # The Linux libraries the desktop CI installs, so the sandbox can build the Tauri app.
    ci = (WORKFLOWS / "ci.yml").read_text()
    wanted = re.search(r"apt-get install -y (lib[^\n]+)", ci)
    assert wanted is not None
    for package in wanted.group(1).split():
        assert re.search(rf"\b{re.escape(package)}\b", text), package
    for tool in ("git", "build-essential", "pkg-config", "ca-certificates"):
        assert re.search(rf"\s{re.escape(tool)}\s", text), tool
    assert "COPY --from=node /usr/local/bin/node /usr/local/bin/node" in lines  # the UI build


def test_claude_proxy_adds_the_key_and_forwards_only_to_claude(stack: dict[str, Any]) -> None:
    proxy = _services(stack)["claude-proxy"]
    assert proxy["read_only"] is True and proxy["cap_drop"] == ["ALL"]
    assert "cap_add" not in proxy and "volumes" not in proxy and "configs" not in proxy
    assert proxy["group_add"] == ["10001"]  # reads the root:10001 0440 secret file
    assert proxy["environment"]["JARVIS_NGINX_CONF"] == stack["x-claude-proxy-nginx"]
    assert proxy["entrypoint"][:2] == ["/bin/sh", "-c"] and proxy["command"] == []
    start = proxy["entrypoint"][2]
    assert '[ -n "$${JARVIS_NGINX_CONF:-}" ] || {' in start
    assert "$$(cat /run/secrets/improve_claude_key)" in start
    assert '*[!A-Za-z0-9_-]*) echo "claude-proxy:' in start  # no quote can reach the config
    assert "umask 077;" in start and "> /tmp/claude-key.conf" in start
    assert start.endswith('exec nginx -e /dev/stderr -c /tmp/nginx.conf -g "daemon off;"')
    assert "/tmp" in proxy["tmpfs"]  # noqa: S108 (a container path)
    health = " ".join(proxy["healthcheck"]["test"])
    assert "test -s /tmp/claude-key.conf" in health and "127.0.0.1:8080/healthz" in health
    conf = CLAUDE_NGINX.read_text()
    code = [ln.strip() for ln in conf.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "listen 8080;" in code
    assert code.count("proxy_pass $claude;") == 1  # one upstream, and only under /v1/
    assert "set $claude https://api.anthropic.com;" in code
    assert "location /v1/ {" in code and "return 404;" in code
    assert "proxy_ssl_verify on;" in code and "proxy_ssl_server_name on;" in code
    assert "proxy_ssl_name api.anthropic.com;" in code
    assert "proxy_set_header Host api.anthropic.com;" in code
    assert 'proxy_set_header Authorization "";' in code  # the sandbox's own header never passes
    assert "include /tmp/claude-key.conf;" in code
    assert not [ln for ln in code if "x-api-key" in ln]  # the key line is written at start only
    assert "server_tokens off;" in code and "access_log off;" in code


def test_improve_egress_allows_only_public_https(stack: dict[str, Any]) -> None:
    """The team has web access (it has a Researcher): CONNECT to any public host on 443, logged.
    Never to a private, link-local, loopback or tailnet address, never another port."""
    directives = _directives(IMPROVE_SQUID)
    access = [d[1:] for d in directives if d[0] == "http_access"]
    assert access == [
        ["deny", "!connect_method"],
        ["deny", "!tls_port"],
        ["deny", "internal_dst"],
        ["allow", "jarvis_improve", "connect_method", "tls_port"],
        ["deny", "all"],
    ]
    acls = {d[1]: d[2:] for d in directives if d[0] == "acl"}
    assert acls["tls_port"] == ["port", "443"]
    assert acls["connect_method"] == ["method", "CONNECT"]
    assert acls["internal_dst"][0] == "dst"
    refused = [ipaddress.ip_network(r) for r in acls["internal_dst"][1:]]
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "0.0.0.0/8",
                "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"):  # fmt: skip
        assert ipaddress.ip_network(net) in refused, net
    for name, network in stack["networks"].items():  # every jarvis subnet is refused
        for pool in (network or {}).get("ipam", {}).get("config", []):
            subnet = ipaddress.ip_network(pool["subnet"])
            assert any(subnet.subnet_of(r) for r in refused if r.version == subnet.version), name
    assert ["access_log", "stdio:/dev/stdout"] in directives  # every request is logged
    assert not any(d[0] == "access_log" and d[1] == "none" for d in directives)
    assert ["cache", "deny", "all"] in directives
    assert not any(d[0] in ("cache_dir", "cache_peer") for d in directives)
    assert [d for d in directives if d[0] == "http_port"] == [["http_port", "3128"]]
    proxy = _services(stack)["improve-egress"]
    assert proxy["environment"]["JARVIS_SQUID_CONF"] == stack["x-improve-egress-squid"]
    assert proxy["entrypoint"][:2] == ["/bin/sh", "-c"] and proxy["command"] == []
    start = proxy["entrypoint"][2]
    assert '[ -n "$${JARVIS_SQUID_CONF:-}" ] || {' in start and "exit 1; }" in start
    assert start.endswith("exec /usr/sbin/squid -f /tmp/squid.conf -NYC")
    assert proxy["read_only"] is True and proxy["cap_drop"] == ["ALL"]
    # Runs as the image's proxy user from the start, so Docker hands it the log pipe (squid
    # opens access_log after dropping root, and could not reopen a root-owned stdout).
    assert proxy["user"] == "13:13" and "cap_add" not in proxy
    assert "secrets" not in proxy and "configs" not in proxy
    health = " ".join(proxy["healthcheck"]["test"])
    assert "http_access deny internal_dst' /tmp/squid.conf" in health
    assert "http_access deny all' /tmp/squid.conf" in health


def test_sandbox_db_is_throwaway(stack: dict[str, Any]) -> None:
    services = _services(stack)
    db = services["sandbox-db"]
    assert "secrets" not in db and "volumes" not in db
    assert _networks(db) == ["jarvis_improve"]
    assert db["read_only"] is True and db["cap_drop"] == ["ALL"] and db["user"] == "999:999"
    assert "/var/lib/postgresql/data:mode=1777" in db["tmpfs"]
    env = db["environment"]
    assert env["PGDATA"].startswith("/var/lib/postgresql/data/")
    dsn = services["sandbox"]["environment"]["JARVIS_TEST_SUPERUSER_DSN"]
    assert dsn == f"postgresql://postgres:{env['POSTGRES_PASSWORD']}@sandbox-db:5432/postgres"
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])


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


def _workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    loaded["on"] = loaded.pop(True, loaded.get("on"))  # YAML 1.1 reads the key `on` as true
    return loaded


def test_desktop_ci_reads_only() -> None:
    assert _workflow("ci.yml")["permissions"] == {"contents": "read"}


def test_installer_publishes_only_from_main() -> None:
    release = _workflow("release.yml")
    assert release["on"]["push"]["branches"] == ["main"]
    # A manual run from another branch builds nothing either: only main publishes.
    assert release["jobs"]["build"]["if"] == "github.ref == 'refs/heads/main'"


def test_server_ci_builds_the_sandbox_image_without_pushing() -> None:
    import fnmatch

    workflow = _workflow("server.yml")
    for event in ("push", "pull_request"):
        paths = workflow["on"][event]["paths"]
        assert any(fnmatch.fnmatch("server/deploy/improve/sandbox.Dockerfile", p) for p in paths)
    steps = workflow["jobs"]["test"]["steps"]
    runs = [s.get("run", "") for s in steps]
    build = [r for r in runs if "deploy/improve/sandbox.Dockerfile" in r]
    assert build == ["docker build -f deploy/improve/sandbox.Dockerfile --tag jarvis-sandbox:ci ."]
    text = (WORKFLOWS / "server.yml").read_text()
    assert "jarvis-sandbox" not in text.split("publish:", 1)[1]  # never published from here
