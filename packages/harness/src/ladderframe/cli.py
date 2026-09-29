"""ladderframe command line.

    ladderframe [--root DIR] [--profile NAME] run "prompt" [--session ID]
    ladderframe [--root DIR] repl [--session ID]
    ladderframe [--root DIR] check
    ladderframe [--root DIR] eval [DATASET ...] [--min-pass-rate 0.9]
    ladderframe [--root DIR] boot [--dry-run]
    ladderframe [--root DIR] cron
    ladderframe [--root DIR] serve [--host H] [--port P] [--no-worker]
    ladderframe [--root DIR] worker

The root is `--root`, else `$LADDERFRAME_ROOT`, else the current directory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)


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

    evals = sub.add_parser("eval", help="run the pydantic-evals datasets in <root>/evals")
    evals.add_argument("datasets", nargs="*", help="dataset names (file stems); default: all")
    evals.add_argument("--min-pass-rate", type=float, default=1.0, help="fail below this fraction (default 1.0)")
    evals.add_argument("--concurrency", type=int, default=4)

    boot = sub.add_parser("boot", help="run etc/init.d scripts")
    boot.add_argument("--dry-run", action="store_true")

    sub.add_parser("cron", help="exec supercronic with the merged etc/cron.d crontab")
    serve = sub.add_parser("serve", help="HTTP server (+ Temporal worker when runtime.executor is temporal)")
    serve.add_argument("--host", help="default: server.host")
    serve.add_argument("--port", type=int, help="default: server.port")
    serve.add_argument("--no-worker", action="store_true", help="do not run the Temporal worker in this process")
    sub.add_parser("worker", help="Temporal worker only")
    return parser


def _print_event(event: Any) -> None:
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
    from .observability import configure
    from .runtime.runtime import Runtime

    runtime = Runtime.load(args.root, args.profile)
    configure(runtime)
    return runtime


def _executor(runtime: Any) -> Any:
    """The configured executor: in-process, or a Temporal session (needs a running worker)."""
    if runtime.config.runtime.executor == "temporal":
        from .runtime.temporal.executor import TemporalExecutor

        return TemporalExecutor(runtime)
    from .runtime.inline import InlineExecutor

    return InlineExecutor(runtime)


async def _turn(executor: Any, session_id: str, prompt: str, quiet: bool) -> int:
    from .runtime.executor import TurnFailed

    # The CLI is the operator: it may continue any user's session.
    turn = await executor.submit(session_id, prompt, user=await executor.owner(session_id))
    if quiet:
        state = await executor.wait(session_id, turn.turn_id)
        print(state.output if state.status == "done" else f"error: {state.error}")
        return 0 if state.status == "done" else 1
    try:
        async for event in executor.events(session_id, turn.turn_id):
            _print_event(event)
    except TurnFailed as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    return 0


async def _run(args: argparse.Namespace) -> int:
    from .runtime.executor import new_id

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    executor = _executor(_load(args))
    await executor.start()
    session_id = args.session or new_id()
    code = await _turn(executor, session_id, prompt, args.quiet)
    if not args.quiet:
        print(f"\n\n[session: {session_id}]", file=sys.stderr)
    await executor.aclose()
    return code


async def _repl(args: argparse.Namespace) -> int:
    from .runtime.executor import new_id

    runtime = _load(args)
    executor = _executor(runtime)
    await executor.start()
    session_id = args.session or new_id()
    print(
        f"{runtime.name} — {runtime.config.resolve_model()} — session {session_id}  (Ctrl-D to exit)", file=sys.stderr
    )
    while True:
        try:
            prompt = input("\n› ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            await executor.aclose()
            return 0
        if not prompt:
            continue
        if prompt.startswith("/") and (name := prompt[1:].split(" ", 1)[0]) in runtime.skills:
            skill_args = prompt[len(name) + 1 :].strip()
            prompt = await runtime.render_skill(runtime.skills[name], skill_args, runtime.new_deps(session_id))
        await _turn(executor, session_id, prompt, False)
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
    if command == "eval":
        return asyncio.run(_eval(args))
    if command == "serve":
        return _serve(args)
    if command == "worker":
        from .server.serve import run_worker

        asyncio.run(run_worker(_load(args)))
        return 0
    return 2


async def _eval(args: argparse.Namespace) -> int:
    from .evals import find_datasets, run_dataset
    from .observability import configure
    from .runtime.runtime import Runtime

    # Evals always run in-process: fresh turns, no sessions, no Temporal.
    runtime = Runtime.load(args.root, args.profile, durable=False)
    configure(runtime)
    paths = find_datasets(runtime, args.datasets)
    if not paths:
        print(f"no datasets in {runtime.root.path / 'evals'}", file=sys.stderr)
        return 1
    passed = total = 0
    for path in paths:
        result = await run_dataset(runtime, path, args.concurrency)
        result.report.print(include_input=True, include_output=True)
        print(f"{path.name}: {result.passed}/{result.total} cases passed\n")
        passed, total = passed + result.passed, total + result.total
    rate = passed / total if total else 1.0
    print(f"overall: {passed}/{total} passed ({rate:.0%}), required {args.min_pass_rate:.0%}")
    return 0 if rate >= args.min_pass_rate else 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server.serve import build_app

    runtime = _load(args)
    server = runtime.config.server
    app = build_app(runtime, with_worker=not args.no_worker)
    uvicorn.run(app, host=args.host or server.host, port=args.port or server.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
