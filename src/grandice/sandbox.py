"""Sandbox (§06).

Three rules hold whatever the backend: the workspace is the only writable path,
network egress is denied rather than open, and every exec has a wall-clock cap
so a runaway loop dies on its own.

Two backends ship: `sandbox-exec` for local macOS development (free, zero
install, adequate against an incompetent agent rather than a determined
attacker) and `docker` for everywhere else, including EC2 — sandbox-exec is a
macOS seatbelt API and does not exist on Linux. `Config.from_env()` picks
between them by platform unless GRANDICE_SANDBOX overrides it. The interface is
the point: both live behind `Sandbox`, so nothing above this file cares which
one is running.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
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
    shell config and no credentials that happen to live in yours.

    One addition: the harness's own interpreter directory goes first on PATH,
    so a bare `python3` inside a sandbox-exec/none exec resolves to the venv
    grandice itself runs under — the one with openpyxl/python-pptx installed
    for the document skills (§07) — rather than the system Python, which has
    neither. This only matters for sandbox-exec and none: DockerSandbox sets
    its own PATH pointing at whatever is baked into the sandbox image, since
    a host env var cannot reach inside a container anyway.
    """
    python_bin = str(Path(sys.executable).parent)
    return {
        "PATH": f"{python_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(cwd),
        "PWD": str(cwd),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
    }


async def _spawn(
    argv: list[str],
    cwd: Path,
    timeout: float,
    on_timeout: Callable[[], Awaitable[None]] | None = None,
) -> ExecResult:
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
        if on_timeout is not None:
            await on_timeout()  # e.g. `docker kill` — the local process alone may not stop it
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


class DockerSandbox(Sandbox):
    """A container per exec, for Linux hosts (EC2 included) where sandbox-exec
    does not exist. Talks to the host's Docker daemon rather than nesting one —
    the harness itself need not run inside a container for this to work.

    The three rules from the module docstring hold the same way: the workspace
    bind mount is the only writable path, `--network none` denies egress, and a
    named container lets the timeout handler actually stop it — killing the
    local `docker run` client alone can otherwise leave the container running
    on the daemon.
    """

    name = "docker"

    def __init__(self, workspace: Path, image: str = "python:3.12-slim") -> None:
        super().__init__(workspace)
        self.image = image
        # Match the host user so writes through the bind mount land with usable
        # permissions, without granting the container root.
        self._uid = os.getuid() if hasattr(os, "getuid") else 1000
        self._gid = os.getgid() if hasattr(os, "getgid") else 1000

    async def run(self, command: str, timeout: float = 60.0) -> ExecResult:
        container = f"grandice-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker", "run", "--rm",
            "--name", container,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", "1g",
            "--cpus", "1",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{self.workspace}:/workspace:rw",
            "-w", "/workspace",
            "-e", "HOME=/tmp",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin",
            "--user", f"{self._uid}:{self._gid}",
            self.image,
            "bash", "-c", command,
        ]
        return await _spawn(argv, self.workspace, timeout, on_timeout=lambda: _docker_kill(container))


async def _docker_kill(container: str) -> None:
    """Best-effort: stop the named container server-side. Killing the local
    `docker run` client does not reliably stop it on the daemon."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", container,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def build(kind: str, workspace: Path, image: str = "python:3.12-slim") -> Sandbox:
    if kind == "sandbox-exec":
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise RuntimeError(
                "sandbox-exec is macOS-only and was not found. On Linux (including "
                "EC2) set GRANDICE_SANDBOX=docker instead — from_env() does this "
                "automatically when not on macOS."
            )
        return SandboxExec(workspace)
    if kind == "docker":
        if not shutil.which("docker"):
            raise RuntimeError(
                "GRANDICE_SANDBOX=docker but no `docker` binary was found. Install "
                "Docker (see DEPLOY_AWS.md), or set GRANDICE_SANDBOX=none to run "
                "unsandboxed against a throwaway directory."
            )
        return DockerSandbox(workspace, image=image)
    if kind == "none":
        return NoSandbox(workspace)
    raise ValueError(f"Unknown sandbox backend {kind!r}. Use 'sandbox-exec', 'docker' or 'none'.")
