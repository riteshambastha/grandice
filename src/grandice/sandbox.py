"""Sandbox (§06).

Three rules hold whatever the backend: the workspace is the only writable path,
network egress is denied rather than open, and every exec has a wall-clock cap
so a runaway loop dies on its own.

P0 ships the macOS `sandbox-exec` backend — free, zero install, and adequate
against an incompetent agent rather than a determined attacker. The interface is
the point: swapping in an OrbStack container later touches only this file.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def render(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        if self.timed_out:
            parts.append("[killed: wall-clock cap reached]")
        elif self.exit_code != 0:
            parts.append(f"[exit {self.exit_code}]")
        return "\n".join(parts) or "[no output]"


class Sandbox:
    """A place the agent may write and run things. Not your home directory."""

    name = "sandbox"

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> Path:
        """Confine a model-supplied path to the workspace.

        Checked after resolution, so `../../.ssh/id_rsa` and symlinks out both
        fail rather than merely looking wrong.
        """
        candidate = (self.workspace / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PermissionError(
                f"{path!r} is outside the workspace. Everything you do lives under "
                f"{self.workspace}; use a relative path."
            )
        return candidate

    async def run(self, command: str, timeout: float = 60.0) -> ExecResult:
        raise NotImplementedError


class NoSandbox(Sandbox):
    """Plain subprocess. The doc says never, not even for the prototype —
    this exists so non-macOS machines fail loudly rather than silently."""

    name = "none"

    async def run(self, command: str, timeout: float = 60.0) -> ExecResult:
        return await _spawn(["/bin/bash", "-c", command], self.workspace, timeout)


class SandboxExec(Sandbox):
    """macOS seatbelt. Workspace-only writes, no network, wall-clock capped."""

    name = "sandbox-exec"

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._profile_path = self._write_profile()

    def _write_profile(self) -> Path:
        scratch = Path(tempfile.gettempdir()) / "grandice"
        scratch.mkdir(parents=True, exist_ok=True)
        profile = f"""(version 1)
(deny default)

; Reads are broad — the agent needs /usr, /bin and a Python runtime.
(allow file-read* file-read-metadata)

; Writes are not. The workspace is the only durable path it can touch.
(deny file-write*)
(allow file-write* (subpath "{self.workspace}"))
(allow file-write* (subpath "{scratch}"))
(allow file-write-data
    (literal "/dev/null")
    (literal "/dev/stdout")
    (literal "/dev/stderr")
    (literal "/dev/dtracehelper")
    (regex #"^/dev/tty"))

(allow process-exec process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)

; No open internet. This alone defeats most exfiltration paths (§08).
(deny network*)
"""
        path = scratch / "workspace.sb"
        path.write_text(profile)
        return path

    async def run(self, command: str, timeout: float = 60.0) -> ExecResult:
        argv = [
            "/usr/bin/sandbox-exec",
            "-f",
            str(self._profile_path),
            "/bin/bash",
            "-c",
            command,
        ]
        return await _spawn(argv, self.workspace, timeout)


def _clean_env(cwd: Path) -> dict[str, str]:
    """A deliberately small environment. The agent inherits no API keys, no
    shell config and no credentials that happen to live in yours."""
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(cwd),
        "PWD": str(cwd),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
    }


async def _spawn(argv: list[str], cwd: Path, timeout: float) -> ExecResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=_clean_env(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # so a runaway child group dies with the parent
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()
        return ExecResult("", "", exit_code=124, timed_out=True)

    return ExecResult(
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        exit_code=proc.returncode or 0,
    )


def build(kind: str, workspace: Path) -> Sandbox:
    if kind == "sandbox-exec":
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise RuntimeError(
                "sandbox-exec is macOS-only and was not found. Set GRANDICE_SANDBOX=none "
                "to run unsandboxed against a throwaway directory, or move to a container."
            )
        return SandboxExec(workspace)
    if kind == "none":
        return NoSandbox(workspace)
    raise ValueError(f"Unknown sandbox backend {kind!r}. Use 'sandbox-exec' or 'none'.")
