"""Built-in tools, named like Claude Code's."""

from collections.abc import Callable
from typing import Any

from .agent import Agent
from .base import ToolMeta, get_meta, tool
from .bash import Bash
from .glob import Glob
from .grep import Grep
from .read import Read
from .skill import Skill
from .web_fetch import WebFetch
from .web_search import WebSearch

BUILTIN_TOOLS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn for fn in (Read, Bash, Glob, Grep, WebFetch, WebSearch, Agent, Skill)
}

__all__ = ["BUILTIN_TOOLS", "ToolMeta", "get_meta", "tool"]
