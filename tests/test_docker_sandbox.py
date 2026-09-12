"""The Docker sandbox backend (for Linux/EC2, where sandbox-exec doesn't
exist). No Docker daemon runs in this dev environment, so these tests verify
the command it *would* run rather than exercising a real container — the
three isolation rules (§06) are checked here as "is the flag present", and
re-verified as actual behaviour once this runs on the EC2 host (see
DEPLOY_AWS.md's smoke test)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from grandice import sandbox as sb


@pytest.fixture
def docker_box(tmp_path: Path) -> sb.DockerSandbox:
    return sb.DockerSandbox(tmp_path, image="python:3.12-slim")


def _captured_argv(mock_spawn) -> list[str]:
    args, kwargs = mock_spawn.call_args
    return args[0]


async def test_container_has_no_network(docker_box):
    with patch("grandice.sandbox._spawn", new=AsyncMock(return_value=sb.ExecResult("", "", 0))) as m:
        await docker_box.run("echo hi")
    argv = _captured_argv(m)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


async def test_only_the_workspace_is_writable(docker_box):
    with patch("grandice.sandbox._spawn", new=AsyncMock(return_value=sb.ExecResult("", "", 0))) as m:
        await docker_box.run("echo hi")
    argv = _captured_argv(m)
    assert "--read-only" in argv  # root fs is read-only
    mount = argv[argv.index("-v") + 1]
    assert mount == f"{docker_box.workspace}:/workspace:rw"  # only the workspace is writable


async def test_runs_as_host_uid_not_root(docker_box):
    with patch("grandice.sandbox._spawn", new=AsyncMock(return_value=sb.ExecResult("", "", 0))) as m:
        await docker_box.run("echo hi")
    argv = _captured_argv(m)
    user = argv[argv.index("--user") + 1]
    assert user != "0:0" and ":" in user


async def test_capabilities_are_dropped_and_privilege_escalation_denied(docker_box):
    with patch("grandice.sandbox._spawn", new=AsyncMock(return_value=sb.ExecResult("", "", 0))) as m:
        await docker_box.run("echo hi")
    argv = _captured_argv(m)
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv


async def test_timeout_kills_the_named_container_server_side(docker_box, monkeypatch):
    killed = []

    async def fake_docker_kill(container: str) -> None:
        killed.append(container)

    monkeypatch.setattr(sb, "_docker_kill", fake_docker_kill)

    async def fake_spawn(argv, cwd, timeout, on_timeout=None):
        # Simulate _spawn's own timeout branch calling the hook it was given.
        if on_timeout is not None:
            await on_timeout()
        return sb.ExecResult("", "", 124, timed_out=True)

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    result = await docker_box.run("sleep 30", timeout=1)

    assert result.timed_out
    assert len(killed) == 1
    assert killed[0].startswith("grandice-")


def test_build_reports_a_clear_error_when_docker_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker"):
        sb.build("docker", tmp_path)
