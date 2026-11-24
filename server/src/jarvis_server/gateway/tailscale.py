"""The Tailscale sidecar's local API, over the socket shared only with `gateway-ts` (§4.2).

`whois` gives a peer's identity, `"ts:" + Node.StableID` (a per-device ID, never a name), or
None for an address the tailnet doesn't know. `tailnet_ipv4` gives the sidecar's own tailnet
IPv4 address, the only address the gateway binds.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import urllib.parse
from typing import Any, Protocol

LOCAL_API_HOST = "local-tailscaled.sock"
TIMEOUT_S = 5.0
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
MAX_IDENTITY = 200


class WhoIs(Protocol):
    async def identity(self, peer_ip: str) -> str | None: ...


def is_tailnet_address(host: str | None) -> bool:
    """True only for a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48)."""
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return addr in (TAILNET_V4 if addr.version == 4 else TAILNET_V6)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__(LOCAL_API_HOST, timeout=TIMEOUT_S)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        sock.connect(self._socket_path)
        self.sock = sock


def _get_json(socket_path: str, path: str) -> Any | None:
    """GET a local API path; the JSON body on 200, None on any other status."""
    conn = _UnixHTTPConnection(socket_path)
    try:
        conn.request("GET", path, headers={"Host": LOCAL_API_HOST})
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            return None
        return json.loads(body)
    finally:
        conn.close()


def identity_from_whois(data: Any) -> str | None:
    node = data.get("Node") if isinstance(data, dict) else None
    stable = node.get("StableID") if isinstance(node, dict) else None
    if not isinstance(stable, str) or not stable:
        return None
    identity = f"ts:{stable}"
    return identity if len(identity) <= MAX_IDENTITY else None


def tailnet_ipv4(socket_path: str) -> str | None:
    """The sidecar's tailnet IPv4 address, or None if it can't be read."""
    try:
        status = _get_json(socket_path, "/localapi/v0/status")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    ips = status.get("Self", {}).get("TailscaleIPs", []) if isinstance(status, dict) else []
    for ip in ips if isinstance(ips, list) else []:
        if isinstance(ip, str) and is_tailnet_address(ip) and ":" not in ip:
            return ip
    return None


class LocalApiWhoIs:
    """`whois` through the local API socket at `JARVIS_TS_SOCKET`."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    async def identity(self, peer_ip: str) -> str | None:
        path = "/localapi/v0/whois?" + urllib.parse.urlencode({"addr": peer_ip})
        data = await asyncio.to_thread(_get_json, self.socket_path, path)
        return identity_from_whois(data)
