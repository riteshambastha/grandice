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
import socket
import threading
import time

import uvicorn
import webview

from .server.app import create_app

STARTUP_TIMEOUT_SECONDS = 10


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run grandice as a native desktop window.")
    parser.add_argument("--port", type=int, default=0, help="0 (default) picks a free port automatically.")
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--height", type=int, default=800)
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
