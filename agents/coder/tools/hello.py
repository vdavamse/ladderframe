"""Example custom tool. Any `@tool` function in this folder is discovered at startup;
enable it by listing its name under `tools:` in etc/ladderframe.yaml."""

from pydantic import BaseModel
from pydantic_ai import RunContext

from ladderframe import HarnessDeps, tool


class HelloSettings(BaseModel):
    greeting: str = "Hello"


@tool(settings=HelloSettings)
def Hello(ctx: RunContext[HarnessDeps], name: str) -> str:
    """Greet someone by name.

    Args:
        name: Who to greet.
    """
    settings = ctx.deps.settings("Hello", HelloSettings)
    return f"{settings.greeting}, {name}!"
