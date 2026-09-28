from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.common_tools.web_fetch import WebFetchLocalTool
from pydantic_ai.messages import BinaryContent

from ..core.deps import HarnessDeps
from .base import tool


class WebFetchSettings(BaseModel):
    max_content_length: int | None = 50_000
    timeout: int = 30
    allow_local_urls: bool = False
    """SSRF protection: private and cloud-metadata addresses are refused unless this is true."""
    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    headers: dict[str, str] | None = None
    summarize_model: str | None = None
    """If set (e.g. `haiku`), `prompt` is answered against the page by this model, like Claude Code's WebFetch."""


def _domain_subject(args: dict[str, Any]) -> str | None:
    host = urlparse(str(args.get("url", ""))).hostname
    return f"domain:{host}" if host else None


@tool(settings=WebFetchSettings, subject=_domain_subject)
async def WebFetch(ctx: RunContext[HarnessDeps], url: str, prompt: str | None = None) -> Any:
    """Fetch a URL and return its content as markdown. Private and local addresses are blocked.

    Args:
        url: The fully qualified URL to fetch.
        prompt: What to extract from the page. Answered by a small model when one is configured,
            otherwise the page content is returned as-is.
    """
    settings = ctx.deps.settings("WebFetch", WebFetchSettings)
    fetcher = WebFetchLocalTool(
        max_content_length=settings.max_content_length,
        allow_local_urls=settings.allow_local_urls,
        timeout=settings.timeout,
        allowed_domains=settings.allowed_domains,
        blocked_domains=settings.blocked_domains,
        headers=settings.headers,
    )
    page = await fetcher(url)
    if isinstance(page, BinaryContent) or not prompt or not settings.summarize_model:
        return page

    summarizer = Agent(
        ctx.deps.config.resolve_model(settings.summarize_model),
        instructions="Answer the request using only the provided web page content. Quote sparingly.",
    )
    result = await summarizer.run(f"Request: {prompt}\n\nPage ({page['url']}):\n\n{page['content']}")
    return result.output
