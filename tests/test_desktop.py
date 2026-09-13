"""Tests for the desktop shell's non-GUI logic (§desktop.py) — port
selection and the embedded-server lifecycle. Does not open a real window;
webview.start() blocks on the native GUI event loop and isn't something to
run inside a test process. The window itself was verified manually against
a live macOS session (see the commit message) — this covers what's
automatable."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

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


# --- the workspace-default fix -------------------------------------------
#
# Real bug, found by actually launching the packaged app via `open` (not
# just running it from a terminal): Finder/LaunchServices launches a GUI
# app with cwd "/" (confirmed directly with a throwaway probe .app that
# wrote its own `pwd` to a file). Config.from_env()'s workspace default is
# the *relative* string "workspace", which resolves to "/workspace" against
# that cwd — unwritable by a normal account, so session.build() raised
# inside the server's background thread with nowhere to show the error
# (console=False). Running the same binary directly from a terminal masked
# this entirely, since the shell's cwd happened to already contain a
# writable workspace/ directory.

def test_apply_default_workspace_sets_an_absolute_path(monkeypatch):
    from grandice.desktop import DEFAULT_WORKSPACE, _apply_default_workspace

    monkeypatch.delenv("GRANDICE_WORKSPACE", raising=False)
    _apply_default_workspace()
    assert os.environ["GRANDICE_WORKSPACE"] == str(DEFAULT_WORKSPACE)
    assert Path(os.environ["GRANDICE_WORKSPACE"]).is_absolute()


def test_apply_default_workspace_never_overrides_a_users_own_setting(monkeypatch):
    from grandice.desktop import _apply_default_workspace

    monkeypatch.setenv("GRANDICE_WORKSPACE", "/somewhere/the/user/chose")
    _apply_default_workspace()
    assert os.environ["GRANDICE_WORKSPACE"] == "/somewhere/the/user/chose"


def test_default_workspace_is_not_the_relative_path_that_caused_the_bug():
    """Config.from_env()'s own bare default ("workspace") is exactly what
    resolved to the unwritable "/workspace" under a GUI launch — this pins
    down that desktop.py's default can never regress back to it."""
    from grandice.desktop import DEFAULT_WORKSPACE

    assert DEFAULT_WORKSPACE.is_absolute()
    assert DEFAULT_WORKSPACE != Path("workspace").resolve()


# --- the error-visibility fallback ----------------------------------------

def test_main_writes_the_error_log_and_shows_a_window_on_startup_failure(monkeypatch, tmp_path):
    """A failure that reaches this far away from a terminal must never just
    vanish — checked by forcing _start_server to raise and confirming both
    halves of the fallback actually ran, not just that main() didn't crash
    the test process."""
    import grandice.desktop as desktop_mod

    log_path = tmp_path / "desktop-error.log"
    monkeypatch.setattr(desktop_mod, "ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(desktop_mod, "_start_server", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    shown = {}
    monkeypatch.setattr(desktop_mod, "_show_error_window", lambda message: shown.setdefault("message", message))
    monkeypatch.setattr(sys, "argv", ["grandice-desktop"])

    desktop_mod.main()

    assert log_path.exists()
    assert "boom" in log_path.read_text()
    assert "boom" in shown.get("message", "")
