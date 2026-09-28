"""ladderframe command line.

    ladderframe [--root DIR] [--profile NAME] run "prompt" [--session ID]
    ladderframe [--root DIR] repl [--session ID]
    ladderframe [--root DIR] check
    ladderframe [--root DIR] boot [--dry-run]
    ladderframe [--root DIR] cron
    ladderframe [--root DIR] serve        (phase 2)
    ladderframe [--root DIR] worker       (phase 2)

The root is `--root`, else `$LADDERFRAME_ROOT`, else the current directory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterable
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from .core.deps import HarnessDeps


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ladderframe", description="File-driven agent harness on pydantic-ai.")
    parser.add_argument("--root", help="agent root directory (default: $LADDERFRAME_ROOT or cwd)")
    parser.add_argument("--profile", help="config profile overlay, e.g. prod -> etc/ladderframe.prod.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run one prompt and print the answer")
    run.add_argument("prompt", nargs="?", help="prompt text; read from stdin when omitted")
    run.add_argument("--session", help="continue (or create) this session id")
    run.add_argument("--quiet", action="store_true", help="print only the final answer")

    repl = sub.add_parser("repl", help="interactive session")
    repl.add_argument("--session", help="continue (or create) this session id")

    sub.add_parser("check", help="validate the agent root")

    boot = sub.add_parser("boot", help="run etc/init.d scripts")
    boot.add_argument("--dry-run", action="store_true")

    sub.add_parser("cron", help="exec supercronic with the merged etc/cron.d crontab")
    sub.add_parser("serve", help="HTTP server (phase 2)")
    sub.add_parser("worker", help="Temporal worker (phase 2)")
    return parser


async def _print_events(_ctx: RunContext[HarnessDeps], events: AsyncIterable[AgentStreamEvent]) -> None:
    async for event in events:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            sys.stdout.write(event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            sys.stdout.write(event.delta.content_delta)
        elif isinstance(event, FunctionToolCallEvent):
            sys.stderr.write(f"\n  ⎿ {event.part.tool_name}({_short_args(event.part.args)})\n")
        sys.stdout.flush()


def _short_args(args: Any, limit: int = 80) -> str:
    text = args if isinstance(args, str) else ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _load(args: argparse.Namespace):  # noqa: ANN202
    from .runtime.runtime import Runtime

    return Runtime.load(args.root, args.profile)


async def _run(args: argparse.Namespace) -> int:
    from .runtime.inline import InlineExecutor

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    executor = InlineExecutor(_load(args))
    if args.quiet:
        _, output = await executor.send(prompt, args.session)
        print(output)
    else:
        session_id, _ = await executor.send(prompt, args.session, event_stream_handler=_print_events)
        print(f"\n\n[session: {session_id}]", file=sys.stderr)
    return 0


async def _repl(args: argparse.Namespace) -> int:
    from .runtime.inline import InlineExecutor

    runtime = _load(args)
    executor = InlineExecutor(runtime)
    session_id = args.session
    print(f"{runtime.name} — {runtime.config.resolve_model()}  (Ctrl-D to exit)", file=sys.stderr)
    while True:
        try:
            prompt = input("\n› ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not prompt:
            continue
        if prompt.startswith("/") and (name := prompt[1:].split(" ", 1)[0]) in runtime.skills:
            skill_args = prompt[len(name) + 1 :].strip()
            prompt = await runtime.render_skill(runtime.skills[name], skill_args, runtime.new_deps(session_id))
        session_id, _ = await executor.send(prompt, session_id, event_stream_handler=_print_events)
        print()


def _check(args: argparse.Namespace) -> int:
    from .core.check import check_runtime

    report = check_runtime(_load(args))
    for line in report.info:
        print(f"  {line}")
    for line in report.warnings:
        print(f"warning: {line}")
    for line in report.errors:
        print(f"error: {line}")
    print("ok" if report.ok else f"{len(report.errors)} error(s)")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    command = args.command or "repl"
    if command == "repl" and not hasattr(args, "session"):
        args.session = None

    if command == "run":
        return asyncio.run(_run(args))
    if command == "repl":
        return asyncio.run(_repl(args))
    if command == "check":
        return _check(args)
    if command in ("boot", "cron"):
        runtime = _load(args)
        if command == "boot":
            from .system.init import run_init

            return run_init(runtime.root, runtime.share, dry_run=args.dry_run)
        from .system.cron import build_crontab, exec_supercronic

        exec_supercronic(build_crontab(runtime.root, runtime.share), runtime.root)
        return 0
    print(f"`ladderframe {command}` is planned for phase 2 (FastAPI server + Temporal worker).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
