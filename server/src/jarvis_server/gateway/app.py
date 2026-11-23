"""The gateway app and the `jarvis-gateway` entry point (spec §4, §4.2).

`create_app` serves one WebSocket route, `/v1/node`, and nothing else: no HTTP routes, no docs.
It connects to Postgres only as `jarvis_gateway` (it refuses to start as any other role) through
a psycopg `AsyncConnectionPool`.

`main` asks the Tailscale sidecar for its tailnet IPv4 address and binds uvicorn only to that
address, port 8443. It exits non-zero if the address is unknown or isn't a tailnet address, so
it can never bind `0.0.0.0`. uvicorn trusts no proxy headers, so the peer address used for
`whois` is the real one.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, WebSocket
from psycopg_pool import AsyncConnectionPool

from jarvis_server import config
from jarvis_server.bootstrap import GATEWAY_ROLE
from jarvis_server.gateway.keys import GatewayKeys, load_keys
from jarvis_server.gateway.relay import TICK_S, Relay
from jarvis_server.gateway.session import GatewayContext, Session
from jarvis_server.gateway.tailscale import LocalApiWhoIs, WhoIs, is_tailnet_address, tailnet_ipv4

log = logging.getLogger(__name__)

PORT = 8443
RECHECK_INTERVAL_S = 60.0  # the revocation re-check (§7.2, P2-E10)
# uvicorn's frame ceiling sits above the gateway's own 65,536-byte limit, so a frame between
# the two gets `error too_large` and the connection stays open.
WS_MAX_SIZE = 1 << 20


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _check_role(pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]]) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT current_user")
        row = await cur.fetchone()
    if row is None or row[0] != GATEWAY_ROLE:
        raise RuntimeError(f"the gateway connects only as {GATEWAY_ROLE}")


def create_app(
    *,
    dsn: str,
    whois: WhoIs,
    gw_key: Ed25519PrivateKey,
    gw_key_id: str,
    core_public_keys: Mapping[str, bytes],
    bind_check: bool = True,
    clock: Callable[[], datetime] = _utcnow,
    recheck_interval: float = RECHECK_INTERVAL_S,
    tick_interval: float = TICK_S,
) -> FastAPI:
    """The gateway app. `bind_check` refuses a connection that arrived on a local address that
    isn't a Tailscale address (recorded `unknown_peer`); `clock`, `recheck_interval` and the
    relay's `tick_interval` (how often, in real seconds, it checks the clock for heartbeats
    and the re-send sweep) are injectable for tests."""
    keys = GatewayKeys(gw_key=gw_key, gw_key_id=gw_key_id, core_public_keys=dict(core_public_keys))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            dsn, min_size=1, max_size=10, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        try:
            await _check_role(pool)
            relay = Relay(pool=pool, dsn=dsn, clock=clock, tick_interval=tick_interval)
            await relay.start()
            try:
                app.state.gateway = GatewayContext(
                    pool=pool,
                    whois=whois,
                    keys=keys,
                    clock=clock,
                    recheck_interval=recheck_interval,
                    bind_check=bind_check,
                    relay=relay,
                )
                yield
            finally:
                await relay.stop()
        finally:
            await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/v1/node")
    async def node(websocket: WebSocket) -> None:
        await Session(websocket, app.state.gateway).run()

    return app


def _build_app(socket_path: str) -> FastAPI:
    keys = load_keys(config.SECRETS_DIR)
    return create_app(
        dsn=config.gateway_dsn(),
        whois=LocalApiWhoIs(socket_path),
        gw_key=keys.gw_key,
        gw_key_id=keys.gw_key_id,
        core_public_keys=keys.core_public_keys,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    socket_path = os.environ.get("JARVIS_TS_SOCKET")
    if not socket_path:
        log.error("JARVIS_TS_SOCKET is not set; refusing to start")
        return 2
    host = tailnet_ipv4(socket_path)
    if host is None or not is_tailnet_address(host):
        log.error("the sidecar's tailnet IPv4 address is unknown; refusing to start")
        return 2
    app = _build_app(socket_path)
    uvicorn.run(
        app,
        host=host,
        port=PORT,
        proxy_headers=False,
        server_header=False,
        ws_max_size=WS_MAX_SIZE,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
