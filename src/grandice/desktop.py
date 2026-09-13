"""A native desktop shell around the same dashboard the browser sees.

Wraps server/app.py's FastAPI app in a background thread and opens a native
OS window onto it with pywebview — WKWebView on macOS, WebView2 on Windows,
GTK WebKit on Linux. Nothing here is platform-specific; pywebview picks the
right native backend itself. Verified on macOS, the only OS this dev
environment can actually run — see DESKTOP_APP.md for the Windows build,
which is documented but not verified here for the same reason.

Optional install: pip install -e ".[desktop]"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import threading
import time
import traceback
from pathlib import Path

import uvicorn
import webview

from .server.app import create_app

STARTUP_TIMEOUT_SECONDS = 10

# A desktop app has no cwd a user chose — Finder/LaunchServices launches one
# with cwd "/" (confirmed directly: a probe .app written its own `pwd` to a
# file and read "/" back), and Config.from_env()'s workspace default is the
# *relative* string "workspace". Resolved against "/", that's "/workspace" —
# unwritable by a normal account, so session.build() raises. Worse, that
# raise happens inside a background thread under a console=False build,
# where an uncaught exception has no terminal to print to and would
# otherwise just vanish. Setting an absolute default here — only if the user
# hasn't already set one — fixes the real cause; ERROR_LOG_PATH is the net
# for whatever this doesn't anticipate.
DEFAULT_WORKSPACE = Path.home() / "Documents" / "grandice" / "workspace"
ERROR_LOG_PATH = Path.home() / ".grandice" / "desktop-error.log"


def _free_port() -> int:
    """Ask the OS for an unused port rather than guessing one — avoids
    colliding with a `grandice-web` instance (or anything else) already
    running on the usual 8000."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(host: str, port: int) -> uvicorn.Server:
    """Runs the same app grandice-web serves, on a background thread inside
    this same process — one process, one window, no separate server to
    manage or leave running after the window closes."""
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _serve() -> None:
        asyncio.run(server.serve())

    threading.Thread(target=_serve, daemon=True).start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"Server did not start within {STARTUP_TIMEOUT_SECONDS}s.")
        time.sleep(0.05)
    return server


def _show_error_window(message: str) -> None:
    """The fallback when startup fails before a real window ever opens.
    console=False means there is no terminal to print to — without this, a
    startup failure is just a process that silently never appears."""
    html = f"""
    <body style="background:#0e131a;color:#dee5ee;font-family:-apple-system,sans-serif;
                  padding:32px;line-height:1.5">
      <h2 style="color:#ee8880">grandice failed to start</h2>
      <pre style="white-space:pre-wrap;background:#161d26;padding:14px;border-radius:6px;
                   font-size:12px">{message}</pre>
      <p>Full details were also written to: {ERROR_LOG_PATH}</p>
    </body>"""
    webview.create_window("grandice — startup error", html=html, width=700, height=500)
    webview.start()


def _apply_default_workspace() -> None:
    """An absolute default, not Config.from_env()'s relative "workspace" —
    see DEFAULT_WORKSPACE's module comment for why that matters specifically
    for a GUI-launched app. Only applied if the user hasn't set one
    themselves — a real GRANDICE_WORKSPACE (e.g. from a shell) always wins."""
    os.environ.setdefault("GRANDICE_WORKSPACE", str(DEFAULT_WORKSPACE))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run grandice as a native desktop window.")
    parser.add_argument("--port", type=int, default=0, help="0 (default) picks a free port automatically.")
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--height", type=int, default=800)
    args = parser.parse_args()

    _apply_default_workspace()

    try:
        port = args.port or _free_port()
        server = _start_server("127.0.0.1", port)

        webview.create_window(
            "grandice",
            f"http://127.0.0.1:{port}",
            width=args.width,
            height=args.height,
            min_size=(800, 600),
        )
        webview.start()  # blocks until the window is closed

        server.should_exit = True
    except Exception:  # noqa: BLE001 — the last resort: never fail silently
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG_PATH.write_text(traceback.format_exc())
        _show_error_window(traceback.format_exc())


if __name__ == "__main__":
    main()
