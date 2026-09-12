# Build the Windows desktop app.
#
# NOT verified against a real Windows machine — this repo's own dev
# environment is macOS-only. The spec itself (grandice.spec) is the same
# file used for the macOS build and PyInstaller is designed to adapt to
# the host platform automatically, but that adaptation is untested here.
# See DESKTOP_APP.md before relying on this for anything beyond a first try.
#
# Prerequisites: Python 3.12+, and Microsoft Edge WebView2 Runtime (ships
# with Windows 11 and most current Windows 10 installs already; if
# pywebview's window fails to open, install it from
# https://developer.microsoft.com/microsoft-edge/webview2/).

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) {
    Write-Host "No .venv found — run this from a checkout with the venv already set up:"
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\pip install -e `".[dev,web,desktop]`""
    exit 1
}

.venv\Scripts\pip install -q pyinstaller
.venv\Scripts\pyinstaller desktop\grandice.spec --noconfirm --clean

Write-Host ""
Write-Host "Built: dist\grandice\grandice.exe"
Write-Host "Run it: dist\grandice\grandice.exe"
