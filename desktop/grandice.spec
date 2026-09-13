# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the grandice desktop shell.

Build (run from the repo root, in the venv that has pyinstaller + all of
grandice's own extras installed):
    pyinstaller desktop/grandice.spec --noconfirm

Produces dist/grandice.app on macOS, dist/grandice/ (with grandice.exe) on
Windows — same spec, PyInstaller adapts per platform. Only the macOS output
is verified in this repo's own dev environment; see ../DESKTOP_APP.md for
the Windows build and what "verified" does and doesn't mean there.

Bundles two data directories skills.py and server/app.py otherwise expect to
find via source-tree-relative paths that don't exist once frozen:
  - skills/          -> skills.py's _default_skills_dir() looks for this at
                        sys._MEIPASS/skills when sys.frozen is set.
  - server/static/    -> already resolves correctly on its own (relative to
                        __file__, which PyInstaller preserves inside the
                        bundle's package structure) — included explicitly
                        anyway since PyInstaller does not walk into a
                        package's non-.py files without being told to.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(SPECPATH).parent  # SPECPATH is injected by PyInstaller itself

a = Analysis(
    ["launcher.py"],
    pathex=[str(REPO_ROOT / "src")],
    binaries=[],
    datas=[
        (str(REPO_ROOT / "skills"), "skills"),
        (str(REPO_ROOT / "src" / "grandice" / "server" / "static"), "grandice/server/static"),
    ],
    hiddenimports=[
        "grandice.tools.fs", "grandice.tools.shell", "grandice.tools.todo",
        "grandice.tools.skills", "grandice.tools.search", "grandice.tools.subagent",
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="grandice",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # a GUI app: no terminal window behind the native window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="grandice",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="grandice.app",
        icon=str(REPO_ROOT / "desktop" / "icon" / "grandice.icns"),
        bundle_identifier="com.grandice.desktop",
    )
