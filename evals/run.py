#!/usr/bin/env python3
"""Eval runner (§10, "running alongside, from P0").

Without this you cannot tell whether a model swap helped, and you will swap
models constantly. Each task is a directory:

    evals/tasks/<id>/
        task.json     {"prompt": "...", "timeout": 300}
        files/        copied into a fresh workspace before the run
        check.sh      exits 0 if the workspace is correct afterwards

Score is pass/fail plus tokens and wall-clock, written to evals/results.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grandice import loop as agent_loop  # noqa: E402
from grandice.config import Config  # noqa: E402
from grandice.permissions import Gate, always_deny  # noqa: E402
from grandice.session import build as build_session  # noqa: E402

TASKS = Path(__file__).parent / "tasks"
RESULTS = Path(__file__).parent / "results.jsonl"


async def run_one(task_dir: Path, model: str | None) -> dict:
    spec = json.loads((task_dir / "task.json").read_text())
    scratch = Path(tempfile.mkdtemp(prefix=f"eval-{task_dir.name}-"))
    workspace = scratch / "workspace"
    workspace.mkdir()
    if (task_dir / "files").is_dir():
        shutil.copytree(task_dir / "files", workspace, dirs_exist_ok=True)

    config = Config.from_env()
    config = replace(config, workspace=workspace)
    if model:
        config = replace(config, tiers=replace(config.tiers, orchestrator=model))

    session = build_session(config, Gate(always_deny))
    started = time.monotonic()
    reason = "done"

    try:
        async for event in agent_loop.run_turn(session, spec["prompt"]):
            if isinstance(event, agent_loop.Finished):
                reason = event.reason
    except Exception as exc:  # noqa: BLE001
        reason = f"crashed: {exc}"

    elapsed = time.monotonic() - started
    check = subprocess.run(
        ["/bin/bash", str(task_dir / "check.sh")],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    ledger = session.router.ledger
    return {
        "task": task_dir.name,
        "model": config.tiers.orchestrator if config.live else "stub",
        "passed": check.returncode == 0,
        "detail": (check.stdout + check.stderr).strip()[:300],
        "reason": reason,
        "steps": session.steps,
        "seconds": round(elapsed, 1),
        "tokens_in": ledger.prompt_tokens,
        "tokens_out": ledger.completion_tokens,
        "cost_usd": round(ledger.spent_usd, 4),
        "compactions": session.compactions,
        "workspace": str(workspace),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="*", help="Task ids to run. Default: all.")
    parser.add_argument("--model", help="Orchestrator to test. Compare runs by this.")
    args = parser.parse_args()

    dirs = [TASKS / t for t in args.task] if args.task else sorted(d for d in TASKS.iterdir() if d.is_dir())
    if not dirs:
        print(f"No tasks in {TASKS}. Add one — see evals/README.md.")
        return 1

    rows = []
    for task_dir in dirs:
        row = await run_one(task_dir, args.model)
        rows.append(row)
        mark = "PASS" if row["passed"] else "FAIL"
        print(
            f"{mark:4}  {row['task']:<28} {row['steps']:>3} steps  "
            f"{row['seconds']:>6.1f}s  ${row['cost_usd']:.3f}  {row['detail'][:60]}"
        )
        with RESULTS.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), **row}) + "\n")

    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed · ${sum(r['cost_usd'] for r in rows):.3f} total")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
