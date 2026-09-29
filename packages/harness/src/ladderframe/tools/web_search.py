"""`WebSearch`: web search through Exa's or Parallel's hosted MCP endpoint, like opencode's websearch tool.

No API key is needed; `EXA_API_KEY` / `PARALLEL_API_KEY` raise the rate limits. The result is the
provider's LLM-oriented text, returned as-is.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal
from urllib.parse import quote

import httpx2
from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from .base import tool

DESCRIPTION = """- Search the web using the session's web search provider - performs real-time web searches and can scrape content from specific URLs
- Provides up-to-date information for current events and recent data
- Supports configurable result counts and returns the content from the most relevant websites
- Use this tool for accessing information beyond knowledge cutoff
- Searches are performed automatically within a single API call

Usage notes:
  - Supports live crawling modes when available: 'fallback' (backup if cached unavailable) or 'preferred' (prioritize live crawling)
  - Search types when available: 'auto' (balanced), 'fast' (quick results), 'deep' (comprehensive search)
  - Configurable context length for optimal LLM integration
  - Domain filtering and advanced search options available

The current year is {year}. You MUST use this year when searching for recent information or current events
- Example: If the current year is 2026 and the user asks for "latest AI news", search for "AI news 2026", NOT "AI news 2025\""""

EXA_URL = "https://mcp.exa.ai/mcp"
PARALLEL_URL = "https://search.parallel.ai/mcp"
NO_RESULTS = "No search results found. Please try a different query."


class WebSearchSettings(BaseModel):
    provider: Literal["exa", "parallel"] = "exa"
    """Overridden by `$LADDERFRAME_WEBSEARCH_PROVIDER`."""
    timeout: float = 25


def _current_year() -> int:
    if "temporalio" in sys.modules:
        from temporalio import workflow

        if workflow.in_workflow():
            return workflow.now().year
    return date.today().year


async def _describe(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition:
    return replace(tool_def, description=DESCRIPTION.format(year=_current_year()))


@tool(settings=WebSearchSettings, subject="query", prepare=_describe)
async def WebSearch(
    ctx: RunContext[HarnessDeps],
    query: str,
    numResults: int | None = None,
    livecrawl: Literal["fallback", "preferred"] | None = None,
    type: Literal["auto", "fast", "deep"] | None = None,
    contextMaxCharacters: int | None = None,
) -> str:
    """Search the web.

    Args:
        query: Websearch query
        numResults: Number of search results to return (default: 8)
        livecrawl: Live crawl mode - 'fallback': use live crawling as backup if cached content unavailable, 'preferred': prioritize live crawling (default: 'fallback')
        type: Search type - 'auto': balanced search (default), 'fast': quick results, 'deep': comprehensive search
        contextMaxCharacters: Maximum characters for context string optimized for LLMs (default: 10000)
    """
    settings = ctx.deps.settings("WebSearch", WebSearchSettings)
    provider = os.environ.get("LADDERFRAME_WEBSEARCH_PROVIDER") or settings.provider
    if provider == "parallel":
        headers = {"User-Agent": f"ladderframe/{_version()}"}
        if key := os.environ.get("PARALLEL_API_KEY"):
            headers["Authorization"] = f"Bearer {key}"
        arguments: dict[str, Any] = {
            "objective": query,
            "search_queries": [query],
            "session_id": ctx.deps.session_id,
        }
        result = await call_mcp(PARALLEL_URL, "web_search", arguments, settings.timeout, headers)
    else:
        url = EXA_URL
        if key := os.environ.get("EXA_API_KEY"):
            url = f"{EXA_URL}?exaApiKey={quote(key)}"
        arguments = {
            "query": query,
            "type": type or "auto",
            "numResults": numResults or 8,
            "livecrawl": livecrawl or "fallback",
        }
        if contextMaxCharacters is not None:
            arguments["contextMaxCharacters"] = contextMaxCharacters
        result = await call_mcp(url, "web_search_exa", arguments, settings.timeout)
    return result or NO_RESULTS


async def call_mcp(
    url: str, tool_name: str, arguments: dict[str, Any], timeout: float, headers: dict[str, str] | None = None
) -> str | None:
    """One stateless MCP `tools/call` over streamable HTTP; returns the first text content."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
    try:
        async with httpx2.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json=request,
                headers={"Accept": "application/json, text/event-stream", **(headers or {})},
            )
            response.raise_for_status()
    except httpx2.TimeoutException as exc:
        raise ModelRetry(f"{tool_name} request timed out") from exc
    except httpx2.HTTPError as exc:
        raise ModelRetry(f"{tool_name} request failed: {exc}") from exc
    return parse_response(response.text)


def parse_response(body: str) -> str | None:
    if text := _parse_payload(body):
        return text
    for line in body.split("\n"):
        if line.startswith("data: ") and (text := _parse_payload(line[6:])):
            return text
    return None


def _parse_payload(payload: str) -> str | None:
    payload = payload.strip()
    if not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    content = ((data.get("result") or {}).get("content")) or []
    return next((item["text"] for item in content if isinstance(item, dict) and item.get("text")), None)


def _version() -> str:
    try:
        return version("ladderframe")
    except PackageNotFoundError:
        return "0"
