from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from .base import tool


class WebSearchSettings(BaseModel):
    backend: Literal["duckduckgo", "tavily"] = "duckduckgo"
    max_results: int = 8
    tavily_api_key: str | None = None
    """Defaults to `$TAVILY_API_KEY`."""


@tool(settings=WebSearchSettings, subject="query")
async def WebSearch(
    ctx: RunContext[HarnessDeps],
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search the web and return result titles, URLs and snippets.

    Args:
        query: The search query.
        allowed_domains: Only return results from these domains.
        blocked_domains: Never return results from these domains.
    """
    settings = ctx.deps.settings("WebSearch", WebSearchSettings)
    if settings.backend == "tavily":
        results = await _tavily(query, settings, allowed_domains, blocked_domains)
    else:
        results = await _duckduckgo(query, settings)
    return [r for r in results if _domain_ok(r, allowed_domains, blocked_domains)]


async def _duckduckgo(query: str, settings: WebSearchSettings) -> list[dict[str, Any]]:
    try:
        from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
    except ImportError as exc:
        raise ModelRetry("WebSearch backend 'duckduckgo' needs `pip install 'ladderframe[search]'`.") from exc
    search = duckduckgo_search_tool(max_results=settings.max_results)
    return [dict(r) for r in await search.function(query)]  # type: ignore[call-arg]


async def _tavily(
    query: str, settings: WebSearchSettings, allowed: list[str] | None, blocked: list[str] | None
) -> list[dict[str, Any]]:
    try:
        from pydantic_ai.common_tools.tavily import tavily_search_tool
    except ImportError as exc:
        raise ModelRetry("WebSearch backend 'tavily' needs `pip install 'ladderframe[tavily]'`.") from exc
    api_key = settings.tavily_api_key or os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise ModelRetry("WebSearch backend 'tavily' needs TAVILY_API_KEY.")
    search = tavily_search_tool(
        api_key, max_results=settings.max_results, include_domains=allowed, exclude_domains=blocked
    )
    return [dict(r) for r in await search.function(query)]  # type: ignore[call-arg]


def _domain_ok(result: dict[str, Any], allowed: list[str] | None, blocked: list[str] | None) -> bool:
    host = (urlparse(str(result.get("href") or result.get("url") or "")).hostname or "").lower()

    def under(domain: str) -> bool:
        domain = domain.lower().lstrip(".")
        return host == domain or host.endswith("." + domain)

    if blocked and any(under(d) for d in blocked):
        return False
    return not allowed or any(under(d) for d in allowed)
