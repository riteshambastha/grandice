"""Tests for the desktop shell's non-GUI logic (§desktop.py) — port
selection and the embedded-server lifecycle. Does not open a real window;
webview.start() blocks on the native GUI event loop and isn't something to
run inside a test process. The window itself was verified manually against
a live macOS session (see the commit message) — this covers what's
automatable."""

from __future__ import annotations

import socket

import httpx
import pytest

from grandice.desktop import _free_port, _start_server


def test_free_port_returns_a_bindable_port():
    port = _free_port()
    assert 0 < port < 65536
    # Prove it's actually free by binding it ourselves right after.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_free_port_returns_different_ports_across_calls():
    # Not guaranteed by the OS, but overwhelmingly likely, and worth
    # catching a bug that always returns some fixed port.
    ports = {_free_port() for _ in range(5)}
    assert len(ports) > 1


async def test_start_server_actually_serves_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANDICE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("GRANDICE_API_KEY", raising=False)
    monkeypatch.delenv("GRANDICE_BASE_URL", raising=False)

    port = _free_port()
    server = _start_server("127.0.0.1", port)
    try:
        assert server.started
        async with httpx.AsyncClient() as client:
            res = await client.get(f"http://127.0.0.1:{port}/api/state")
        assert res.status_code == 200
        assert res.json()["live"] is False
    finally:
        server.should_exit = True


async def test_start_server_raises_if_never_ready(monkeypatch):
    """A server that never flips .started shouldn't hang the caller forever."""
    import grandice.desktop as desktop_mod

    class NeverStartsServer:
        started = False
        should_exit = False

        async def serve(self):
            pass

    monkeypatch.setattr(desktop_mod, "STARTUP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(desktop_mod.uvicorn, "Server", lambda config: NeverStartsServer())

    with pytest.raises(RuntimeError, match="did not start"):
        desktop_mod._start_server("127.0.0.1", _free_port())
