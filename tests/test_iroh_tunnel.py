from __future__ import annotations

import asyncio
import http.server
import importlib
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remote_library_server.store import RemoteLibraryServerStore  # noqa: E402

# --------------------------------------------------------------- settings (no iroh needed)


def test_store_persists_iroh_enabled(tmp_path):
    store = RemoteLibraryServerStore(tmp_path)
    saved = store.save_settings({"irohEnabled": True})
    assert saved["irohEnabled"] is True
    assert store.load_settings()["irohEnabled"] is True


def test_normalize_settings_includes_iroh(tmp_path):
    routes = importlib.reload(importlib.import_module("routes"))
    routes._store = RemoteLibraryServerStore(tmp_path / "cfg")
    normalized = routes._normalize_settings({"irohEnabled": True})
    assert normalized["irohEnabled"] is True
    assert routes._normalize_settings({})["irohEnabled"] is False


def test_iroh_status_reflects_setting(tmp_path):
    routes = importlib.reload(importlib.import_module("routes"))
    routes._store = RemoteLibraryServerStore(tmp_path / "cfg")
    routes._store.save_settings({"irohEnabled": True})
    status = routes._iroh_status()
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["libraryId"] is None


# -------------------------------------------------------- iroh tunnel (gated on the native dep)


def test_load_or_create_secret_persists(tmp_path):
    pytest.importorskip("iroh")
    from remote_library_server.iroh_tunnel import _load_or_create_secret

    first = _load_or_create_secret(tmp_path)
    second = _load_or_create_secret(tmp_path)
    assert len(first) == 32
    assert first == second  # a stable identity across restarts


def test_tunnel_lifecycle_and_stable_identity(tmp_path):
    pytest.importorskip("iroh")
    from remote_library_server.iroh_tunnel import IrohTunnel

    tunnel = IrohTunnel(tmp_path)
    status = tunnel.start("127.0.0.1", 65000)  # nothing need listen there for bind/status to work
    try:
        assert status["running"] is True
        assert status["endpointId"] and status["libraryId"]
        first_id = status["endpointId"]
    finally:
        tunnel.stop()
    assert tunnel.is_running() is False

    again = IrohTunnel(tmp_path)
    status2 = again.start("127.0.0.1", 65000)
    try:
        assert status2["endpointId"] == first_id  # same persisted key -> same Library ID
    finally:
        again.stop()


def test_tunnel_pipes_http_to_local_server(tmp_path):
    iroh = pytest.importorskip("iroh")
    from remote_library_server.iroh_tunnel import ALPN, IrohTunnel

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"pong": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    stub = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    tunnel = IrohTunnel(tmp_path)
    status = tunnel.start("127.0.0.1", stub_port)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    async def fetch_ping():
        secret = iroh.SecretKey.generate()
        opts = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret.to_bytes(), alpns=[ALPN])
        endpoint = await iroh.Endpoint.bind(opts)
        addr = iroh.EndpointTicket.from_string(status["libraryId"]).endpoint_addr()
        conn = await endpoint.connect(addr, ALPN)
        bi = await conn.open_bi()
        send, recv = bi.send(), bi.recv()
        await send.write_all(b"GET /ping HTTP/1.1\r\nHost: iroh\r\nConnection: close\r\n\r\n")
        await send.finish()
        chunks = []
        while (chunk := await recv.read(65536)):
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        response = run(fetch_ping(), timeout=90)
        assert b"200" in response.split(b"\r\n", 1)[0]
        assert b'{"pong": true}' in response
    finally:
        tunnel.stop()


def test_tunnel_pipes_to_real_asgi_server(tmp_path):
    """Regression guard for the ``write_eof`` bug: the tunnel must NOT half-close the local socket
    after relaying the request. A real ASGI server (uvicorn/h11) treats that early FIN as a client
    disconnect and drops the response without replying — which showed up as an *empty library* even
    though the iroh connection succeeded. The lenient ``http.server`` stub above cannot catch this;
    uvicorn reproduces the exact production behaviour."""
    iroh = pytest.importorskip("iroh")
    uvicorn = pytest.importorskip("uvicorn")
    from remote_library_server.iroh_tunnel import ALPN, IrohTunnel

    async def asgi_app(scope, receive, send):
        assert scope["type"] == "http"
        # A brief pause before responding is essential to the regression: the bug is a race — a
        # tunnel that half-closes the request lets the FIN reach uvicorn *while the handler is still
        # running*, so uvicorn cancels it and replies with nothing. An instant response would win
        # the race and mask the bug (as a real per-request workload never would).
        await asyncio.sleep(0.25)
        body = b'{"pong": true}'
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})

    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    local_port = sk.getsockname()[1]
    sk.close()
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=local_port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", local_port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)

    tunnel = IrohTunnel(tmp_path)
    status = tunnel.start("127.0.0.1", local_port)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    async def fetch():
        secret = iroh.SecretKey.generate()
        opts = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret.to_bytes(), alpns=[ALPN])
        endpoint = await iroh.Endpoint.bind(opts)
        addr = iroh.EndpointTicket.from_string(status["libraryId"]).endpoint_addr()
        conn = await endpoint.connect(addr, ALPN)
        bi = await conn.open_bi()
        send, recv = bi.send(), bi.recv()
        await send.write_all(b"GET /ping HTTP/1.1\r\nHost: iroh\r\nConnection: close\r\n\r\n")
        await send.finish()
        chunks = []
        while (chunk := await recv.read(65536)):
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        response = run(fetch(), timeout=90)
        assert b"200" in response.split(b"\r\n", 1)[0], response[:200]
        assert b'{"pong": true}' in response
    finally:
        server.should_exit = True
        tunnel.stop()
