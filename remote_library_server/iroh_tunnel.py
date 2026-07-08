# SPDX-License-Identifier: AGPL-3.0-or-later
"""iroh peer-to-peer tunnel for the Remote Library Server.

Makes this server reachable by its iroh **Library ID** with *no port forwarding*: it dials
outbound to iroh's relay/discovery network, accepts QUIC streams, and pipes each straight to the
server's own local HTTP server (``127.0.0.1:<port>``). The direct server — its protocol, its
bearer-token auth — is completely untouched; this only carries its bytes over iroh.

The server's identity is a **persistent secret key** (stored on its own, never in the settings the
UI reads) so the EndpointId / Library ID is stable across restarts. Only the *public* Library ID is
shared. ``iroh`` is a native dependency, imported lazily — the plugin loads without it, and it is
only needed when the iroh toggle is on.
"""
from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path

ALPN = b"feedback/rls/1"
_IDENTITY_FILE = "iroh_identity.key"


def _iroh():
    """Import iroh lazily so the plugin (and the plain direct server) loads without the native dep."""
    import iroh

    return iroh


def _load_or_create_secret(config_dir: Path) -> bytes:
    """The server's persistent iroh secret key (32 bytes), created once and reused so the Library
    ID stays stable. Kept in its own file — it is a secret and must never reach the settings API."""
    path = Path(config_dir) / _IDENTITY_FILE
    if path.exists():
        try:
            secret = base64.b64decode(path.read_text(encoding="utf-8").strip())
            if len(secret) == 32:
                return secret
        except Exception:
            pass
    secret = _iroh().SecretKey.generate().to_bytes()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(base64.b64encode(secret).decode("ascii"), encoding="utf-8")
    tmp.replace(path)
    return secret


class IrohTunnel:
    """Runs the server's iroh endpoint on its own asyncio loop, piping accepted streams to the
    local HTTP server. Start it after the direct server is up; stop it when the server stops."""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = Path(config_dir)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._endpoint = None
        self._library_id: str | None = None
        self._endpoint_id: str | None = None
        self._local: tuple[str, int] | None = None
        self._stopping = threading.Event()

    # -- lifecycle -------------------------------------------------------

    def start(self, local_host: str, local_port: int) -> dict:
        with self._lock:
            if self._endpoint is not None:
                return self.status()
            self._local = (local_host, int(local_port))
            self._stopping.clear()
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, name="iroh-server-loop", daemon=True).start()
            secret = _load_or_create_secret(self._config_dir)
            self._endpoint = self._run(self._bind(secret), timeout=60)
            try:
                self._run(self._endpoint.online(), timeout=45)
            except Exception:
                pass  # relay can take longer to settle; addr() still yields direct addresses
            iroh = _iroh()
            self._endpoint_id = self._endpoint.id().to_bytes().hex()
            self._library_id = str(iroh.EndpointTicket.from_addr(self._endpoint.addr()))
            self._run_soon(self._accept_loop())
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stopping.set()
            if self._endpoint is not None:
                try:
                    self._run(self._endpoint.close(), timeout=10)
                except Exception:
                    pass
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._endpoint = None
            self._loop = None
            self._library_id = None
            self._endpoint_id = None
            return {"running": False}

    def is_running(self) -> bool:
        return self._endpoint is not None

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "libraryId": self._library_id,
            "endpointId": self._endpoint_id,
        }

    # -- internals -------------------------------------------------------

    def _run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _run_soon(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _bind(self, secret: bytes):
        iroh = _iroh()
        options = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret, alpns=[ALPN])
        return await iroh.Endpoint.bind(options)

    async def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                incoming = await self._endpoint.accept_next()
            except Exception:
                break
            if incoming is None:
                break
            asyncio.ensure_future(self._handle(incoming))

    async def _handle(self, incoming):
        try:
            accepting = await incoming.accept()
            conn = await accepting.connect()
        except Exception:
            return
        # One HTTP request per bidirectional stream; a follower may open many per connection.
        while not self._stopping.is_set():
            try:
                bi = await conn.accept_bi()
            except Exception:
                break
            asyncio.ensure_future(self._pipe(bi))

    async def _pipe(self, bi):
        recv, send = bi.recv(), bi.send()
        host, port = self._local
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except Exception:
            try:
                await send.finish()
            except Exception:
                pass
            return

        async def iroh_to_local():
            try:
                while True:
                    chunk = await recv.read(65536)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            finally:
                try:
                    writer.write_eof()
                except Exception:
                    pass

        async def local_to_iroh():
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    await send.write_all(chunk)
            finally:
                try:
                    await send.finish()
                except Exception:
                    pass

        await asyncio.gather(iroh_to_local(), local_to_iroh(), return_exceptions=True)
        try:
            writer.close()
        except Exception:
            pass
