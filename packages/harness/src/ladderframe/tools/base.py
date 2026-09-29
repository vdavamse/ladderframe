"""The `@tool` decorator shared by built-in tools, custom tools in `tools/`, and entry-point packages.

    from ladderframe import tool

    @tool
    def Hello(name: str) -> str:
        \"\"\"Greet someone.

        Args:
            name: Who to greet.
        \"\"\"
        return f"Hello {name}"

A tool may take `ctx: RunContext[HarnessDeps]` as its first argument to reach settings, permissions,
the working directory and the runtime. Settings come from `tool_settings.<Name>` in `etc/ladderframe.yaml`:

    class HelloSettings(BaseModel):
        greeting: str = "Hello"

    @tool(settings=HelloSettings)
    def Hello(ctx: RunContext[HarnessDeps], name: str) -> str:
        return f"{ctx.deps.settings('Hello', HelloSettings).greeting} {name}"
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, overload

from pydantic import BaseModel
from pydantic_ai import Tool
from pydantic_ai.tools import ToolPrepareFunc

if TYPE_CHECKING:
    from ..core.deps import HarnessDeps

F = TypeVar("F", bound=Callable[..., Any])

TOOL_ATTR = "__ladderframe_tool__"

Subject = str | Callable[[dict[str, Any]], str | None] | Callable[[dict[str, Any], "HarnessDeps"], str | None] | None


@dataclass
class ToolMeta:
    name: str
    description: str | None = None
    settings: type[BaseModel] | None = None
    aliases: tuple[str, ...] = ()
    prepare: ToolPrepareFunc[Any] | None = None
    subject: Subject = None
    """Argument name (or function of the arguments) checked against permission specifiers.

    E.g. `command`, so that `Bash(git *)` matches on the command line. A function may also take the
    deps as a second argument, e.g. to normalize a path before it is matched.
    """
    truncates: bool = False
    """The tool bounds its own output; otherwise text results are truncated by the harness (see truncate.py)."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Passed through to `pydantic_ai.Tool` (e.g. `timeout`, `sequential`, `requires_approval`)."""


@overload
def tool(func: F, /) -> F: ...
@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    settings: type[BaseModel] | None = None,
    aliases: tuple[str, ...] = (),
    prepare: ToolPrepareFunc[Any] | None = None,
    subject: Subject = None,
    truncates: bool = False,
    **extra: Any,
) -> Callable[[F], F]: ...


def tool(
    func: F | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    settings: type[BaseModel] | None = None,
    aliases: tuple[str, ...] = (),
    prepare: ToolPrepareFunc[Any] | None = None,
    subject: Subject = None,
    truncates: bool = False,
    **extra: Any,
) -> F | Callable[[F], F]:
    """Mark a function as a ladderframe tool. The tool name defaults to the function name."""

    def decorate(fn: F) -> F:
        meta = ToolMeta(name or fn.__name__, description, settings, aliases, prepare, subject, truncates, extra)
        setattr(fn, TOOL_ATTR, meta)
        return fn

    return decorate(func) if func is not None else decorate


def get_meta(obj: Any) -> ToolMeta | None:
    return getattr(obj, TOOL_ATTR, None)


def subject_of(meta: ToolMeta | None, args: dict[str, Any], deps: HarnessDeps) -> str | None:
    if meta is None or meta.subject is None:
        return None
    if callable(meta.subject):
        if len(inspect.signature(meta.subject).parameters) >= 2:
            return meta.subject(args, deps)  # type: ignore[call-arg]
        return meta.subject(args)  # type: ignore[call-arg]
    value = args.get(meta.subject)
    return None if value is None else str(value)


def to_pydantic_tool(fn: Callable[..., Any]) -> Tool[Any]:
    meta = get_meta(fn)
    if meta is None:
        raise TypeError(f"{fn!r} is not decorated with @tool")
    return Tool(fn, name=meta.name, description=meta.description, prepare=meta.prepare, **meta.extra)
