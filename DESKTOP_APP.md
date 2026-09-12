# Desktop app

A native window around the same dashboard the browser sees — one process,
no separate server to start, no browser tab. `pywebview` opens the OS's own
webview (WKWebView on macOS, WebView2 on Windows, GTK WebKit on Linux) onto
a FastAPI server embedded in a background thread inside the same process.
Nothing in `desktop.py` is platform-specific; pywebview picks the right
native backend itself.

## Try it without packaging anything

```bash
.venv/bin/pip install -e ".[web,desktop]"
.venv/bin/grandice-desktop
```

Same session, same skills, same tools as `grandice-web` — just a real
window instead of a browser tab. `--port 0` (the default) picks a free port
automatically so it never collides with a `grandice-web` instance already
running.

## Building a standalone app

`desktop/grandice.spec` is one PyInstaller spec for both platforms —
PyInstaller adapts its output per host OS from the same file: a `.app`
bundle on macOS, a folder with a `.exe` on Windows.

```bash
# macOS
./desktop/build_mac.sh
open dist/grandice.app

# Windows (run from Windows, in PowerShell)
.\desktop\build_windows.ps1
dist\grandice\grandice.exe
```

**What "verified" means here, precisely.** The macOS build was actually run
end to end in this repo's own development: built with `build_mac.sh`,
launched as the packaged `.app` (not `python -m grandice.desktop` — the real
frozen binary), and checked over real HTTP that it discovered all four
skills, all twelve tools, and the correct sandbox backend from inside the
bundle with no source checkout or venv present at runtime. The Windows
build is **not** verified the same way — this development environment is
macOS-only, so `build_windows.ps1` and the spec's Windows path have not run
against a real Windows machine. The spec is written to be correct there (see
the two frozen-app fixes below), but "written to be correct" and "verified"
are different claims, and only the macOS one has been checked.

If you build on Windows and something breaks, the first two places to look
are exactly the two things packaging changed:

- **`skills.py`'s path resolution.** In a normal checkout, `skills.py` finds
  the canonical `skills/` directory by climbing up from its own file path —
  that breaks once PyInstaller freezes everything into a bundle with a
  different layout. Fixed with a `sys.frozen`/`sys._MEIPASS` check in
  `skills._default_skills_dir()`; the spec's `datas` list bundles `skills/`
  at the bundle root to match. If a frozen build reports zero skills, this
  is where to look first.
- **`server/app.py`'s static files.** `STATIC_DIR = Path(__file__).parent /
  "static"` already resolves correctly under PyInstaller on its own (it's
  relative to the module, not climbing to a repo root) — but only because
  the spec's `datas` list explicitly bundles `server/static/`, since
  PyInstaller does not walk into a package's non-`.py` files unless told to.

## Known limits

- **No code signing, on either platform.** An unsigned macOS app triggers a
  Gatekeeper warning ("this app is from an unidentified developer" — right
  click → Open bypasses it once); an unsigned Windows exe triggers a
  SmartScreen warning. Normal for early-stage software, not something this
  build fixes — real signing needs a paid Apple Developer certificate and a
  Windows code-signing certificate, neither set up here.
- **No app icon.** `BUNDLE(..., icon=None, ...)` in the spec — a generic
  icon today, not a missing feature so much as a not-yet-designed one.
- **The Windows sandbox story is genuinely incomplete**, and this predates
  the desktop app — it's a real gap worth knowing before treating a Windows
  build as more than a first try. `Config.from_env()` defaults to the
  Docker sandbox backend on any non-macOS platform, and Docker Desktop is
  not something a "basic desktop app" should require an end user to install
  first. Setting `GRANDICE_SANDBOX=none` runs unsandboxed instead — no
  isolation at all, workable for a quick local trial, not a production
  posture. A real Windows-native sandbox (AppContainer, Job Objects, or
  similar) would be a comparable amount of work to the original
  `sandbox-exec`/Docker backends and isn't attempted here.
- One window, one session, same as the browser dashboard — closing the
  window shuts down the embedded server; there's no "minimize to tray and
  keep running" behaviour.
