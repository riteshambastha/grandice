"""CLI — P0's only client.

Deliberately thin. Everything interesting is in the loop; this just renders
events and asks the questions the permission gate needs answered.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from . import loop as agent_loop
from .config import Config
from .permissions import Gate
from .session import Session, build as build_session, start_connectors, stop_connectors

console = Console()


def _banner(session: Session) -> None:
    cfg = session.config
    mode = "live" if cfg.live else "stub router (no GRANDICE_API_KEY)"
    connectors = ", ".join(c.spec.name for c in session.connectors) or "none"
    console.print(
        Panel(
            f"[bold]model[/]      {cfg.tiers.orchestrator}  ([dim]{mode}[/])\n"
            f"[bold]workspace[/]  {cfg.workspace}\n"
            f"[bold]sandbox[/]    {session.sandbox.name}\n"
            f"[bold]tools[/]      {', '.join(session.registry.names())}\n"
            f"[bold]connectors[/] {connectors}\n"
            f"[bold]cost cap[/]   ${cfg.cost_cap_usd:.2f}\n"
            f"[bold]log[/]        {session.log_path}",
            title="grandice",
            border_style="blue",
        )
    )


async def _ask(payload: str) -> bool:
    console.print(Panel(payload, title="approve?", border_style="yellow"))
    answer = await asyncio.get_event_loop().run_in_executor(
        None, lambda: console.input("[yellow]allow this? [y/N][/] ")
    )
    return answer.strip().lower() in {"y", "yes"}


async def _drive(session: Session, message: str) -> None:
    streaming = False
    async for event in agent_loop.run_turn(session, message):
        match event:
            case agent_loop.TextDelta(text):
                console.print(text, end="", markup=False, highlight=False)
                streaming = True
            case agent_loop.ToolStarted(name, arguments):
                if streaming:
                    console.print()
                    streaming = False
                brief = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in arguments.items())
                console.print(f"[cyan]→ {name}[/]([dim]{brief}[/])")
            case agent_loop.ToolFinished(_, ok, preview):
                colour = "green" if ok else "red"
                console.print(f"  [{colour}]{preview}[/]", markup=False if not ok else True)
            case agent_loop.Finished(reason, steps, cost):
                if streaming:
                    console.print()
                console.print(
                    f"\n[dim]{reason} · {steps} steps · ~${cost:.3f} · "
                    f"{session.todos.summary() or 'no plan'}[/]"
                )


async def _repl(session: Session) -> None:
    _banner(session)
    console.print(
        "[dim]Type a task. /plan shows the plan, /cost the ledger, "
        "/tasks background tasks, /quit exits.[/]\n"
    )
    while True:
        try:
            message = await asyncio.get_event_loop().run_in_executor(
                None, lambda: console.input("[bold blue]›[/] ")
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/]")
            return

        command = message.strip()
        if command in {"/quit", "/exit"}:
            return
        if command == "/plan":
            console.print(session.todos.render())
            continue
        if command == "/cost":
            led = session.router.ledger
            console.print(
                f"${led.spent_usd:.4f} of ${led.cap_usd:.2f} · {led.calls} calls · "
                f"{led.prompt_tokens:,} in / {led.completion_tokens:,} out"
            )
            continue
        if command == "/tasks":
            tasks = session.tasks.list()
            if not tasks:
                console.print("[dim]No background tasks yet.[/]")
            for t in tasks:
                console.print(f"{t.id} [{t.status.value}] {t.description[:70]}")
            continue
        if not command:
            continue

        try:
            await _drive(session, command)
        except KeyboardInterrupt:
            session.cancelled = True
            console.print("\n[yellow]interrupted[/]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grandice", description=__doc__)
    parser.add_argument("task", nargs="?", help="Run one task and exit. Omit for a REPL.")
    parser.add_argument("--workspace", type=Path, help="Directory the agent may write to.")
    parser.add_argument("--sandbox", choices=["sandbox-exec", "docker", "none"], help="Isolation backend.")
    parser.add_argument("--model", help="Override the orchestrator model id.")
    parser.add_argument("--cost-cap", type=float, help="Per-session ceiling in USD.")
    parser.add_argument("--yes", action="store_true", help="Approve outward actions without asking.")
    args = parser.parse_args(argv)

    config = Config.from_env()
    overrides = {}
    if args.workspace:
        overrides["workspace"] = args.workspace.resolve()
    if args.sandbox:
        overrides["sandbox"] = args.sandbox
    if args.cost_cap is not None:
        overrides["cost_cap_usd"] = args.cost_cap
    if args.model:
        from dataclasses import replace as dc_replace

        overrides["tiers"] = dc_replace(config.tiers, orchestrator=args.model)
    if overrides:
        from dataclasses import replace as dc_replace

        config = dc_replace(config, **overrides)

    try:
        session = build_session(config, Gate(_ask, auto_approve=args.yes))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 2

    async def _run() -> None:
        try:
            # Inside the try, not before it: if a second connector fails to
            # start, the first one already succeeded and would otherwise
            # leak its subprocess with no cleanup ever reached.
            await start_connectors(session)  # a no-op with no connectors configured
            if args.task:
                _banner(session)
                await _drive(session, args.task)
            else:
                await _repl(session)
        finally:
            await stop_connectors(session)
            session.tasks.close()

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
