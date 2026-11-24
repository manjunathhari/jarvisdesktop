"""The gateway app and the `jarvis-gateway` entry point (spec §4, §4.2).

`create_app` serves one WebSocket route, `/v1/node`, and nothing else: no HTTP routes, no docs.
It connects to Postgres only as `jarvis_gateway` (it refuses to start as any other role) through
a psycopg `AsyncConnectionPool`.

Keys (Task 12): at startup the gateway loads its ACTIVE key and core's ACTIVE public key from
`gw.trusted_key` and its secret, and refuses to start if that fails. It re-reads them at once on
`NOTIFY jarvis_keys`, which core sends when a key activates (R12-28), and every
`key_refresh_interval` (60 s) as a backstop. A failed re-read keeps the keys it has; a missing
secret or key row is logged at ERROR on every re-read (Task 13). A new key
applies to new connections, since each connection signs under the key its challenge announced
(R12-6). A new ACTIVE gateway key also closes the connections still on the old one (`key_rotated`),
so they reconnect under it (fix round 1, I-1).

`main` asks the Tailscale sidecar for its tailnet IPv4 address and binds uvicorn only to that
address, port 8443. It exits non-zero if the address is unknown or isn't a tailnet address, so
it can never bind `0.0.0.0`. uvicorn trusts no proxy headers, so the peer address used for
`whois` is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import uvicorn
from fastapi import FastAPI, WebSocket
from psycopg_pool import AsyncConnectionPool

from jarvis_server import config
from jarvis_server.bootstrap import GATEWAY_ROLE
from jarvis_server.gateway.keys import load_keys
from jarvis_server.gateway.relay import SEND_TIMEOUT_S, TICK_S, Relay
from jarvis_server.gateway.session import GatewayContext, Session
from jarvis_server.gateway.tailscale import LocalApiWhoIs, WhoIs, is_tailnet_address, tailnet_ipv4

log = logging.getLogger(__name__)

PORT = 8443
RECHECK_INTERVAL_S = 60.0  # the revocation re-check (§7.2, P2-E10)
KEY_REFRESH_S = 60.0  # the trusted-key re-read (Task 12)
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


async def _reload_keys(
    ctx: GatewayContext,
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]],
    secrets_dir: Path,
    lock: asyncio.Lock,
) -> None:
    """Re-read the trusted keys; keep the current ones on failure. A new ACTIVE gateway key
    closes the connections still on the old one (`key_rotated`), so they reconnect under it."""
    async with lock:
        try:
            async with pool.connection() as conn:
                keys = await load_keys(conn, secrets_dir)
        except RuntimeError as e:  # a missing secret or key row: loud (Task 13 ruling 4b)
            log.error(  # the message names a secret file or a role, never a key
                "trusted keys not reloaded, still signing with gateway key %s: %s",
                ctx.keys.gw_key_id, e,
            )  # fmt: skip
            return
        except Exception as e:
            log.warning("trusted keys not reloaded: %s", e)
            return
        if keys.ids != ctx.keys.ids:
            log.info("trusted keys reloaded: gateway key %s, core key %s", *keys.ids)
        rotated = keys.gw_key_id != ctx.keys.gw_key_id
        ctx.keys = keys
        if rotated:  # fix round 1, I-1
            closed = ctx.relay.close_rotated(keys.gw_key_id)
            log.info("closed %d connections on the replaced gateway key", closed)


async def _refresh_keys(reload: Callable[[], Awaitable[None]], interval: float) -> None:
    """The backstop: re-read the trusted keys every `interval` real seconds."""
    while True:
        await asyncio.sleep(interval)
        await reload()


def create_app(
    *,
    dsn: str,
    whois: WhoIs,
    secrets_dir: Path,
    bind_check: bool = True,
    clock: Callable[[], datetime] = _utcnow,
    recheck_interval: float = RECHECK_INTERVAL_S,
    tick_interval: float = TICK_S,
    send_timeout: float = SEND_TIMEOUT_S,
    key_refresh_interval: float = KEY_REFRESH_S,
) -> FastAPI:
    """The gateway app. `bind_check` refuses a connection that arrived on a local address that
    isn't a Tailscale address (recorded `unknown_peer`); `clock`, `recheck_interval` and the
    relay's `tick_interval` (how often, in real seconds, it checks the clock for heartbeats
    and the re-send sweep), `send_timeout` (the bound on one socket send, in real seconds) and
    `key_refresh_interval` (the trusted-key re-read, in real seconds) are injectable for
    tests. Keys come from `gw.trusted_key` and `secrets_dir`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            dsn, min_size=1, max_size=10, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        try:
            await _check_role(pool)
            async with pool.connection() as conn:
                keys = await load_keys(conn, secrets_dir)  # refuses to start without them
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
                    send_timeout=send_timeout,
                )
                lock = asyncio.Lock()

                async def reload() -> None:
                    await _reload_keys(app.state.gateway, pool, secrets_dir, lock)

                relay.on_keys = reload  # NOTIFY jarvis_keys: re-read at once (R12-28)
                refresher = asyncio.create_task(_refresh_keys(reload, key_refresh_interval))
                try:
                    yield
                finally:
                    refresher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await refresher
            finally:
                await relay.stop()
        finally:
            await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/v1/node")
    async def node(websocket: WebSocket) -> None:
        await Session(websocket, app.state.gateway).run()

    return app


GATEWAY_UID = 10001  # the image's `jarvis` user and compose's `user:` (final review S-2)


def _build_app(socket_path: str) -> FastAPI:
    return create_app(
        dsn=config.gateway_dsn(),
        whois=LocalApiWhoIs(socket_path),
        secrets_dir=config.SECRETS_DIR,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if os.getuid() != GATEWAY_UID:
        # The sidecar's egress rules (R12-33) are written for this UID; never run outside them.
        log.error("the gateway must run as UID %d (it is %d); refusing to start", GATEWAY_UID,
                  os.getuid())  # fmt: skip
        return 2
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
