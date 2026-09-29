"""`WebFetch`: fetch a URL as markdown, text or HTML. Parameters and output follow opencode's webfetch tool;
requests go through pydantic-ai's SSRF-safe downloader (private addresses are refused by default)."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

import httpx2
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai._ssrf import safe_download
from pydantic_ai.messages import BinaryContent, ToolReturn

from ..core.deps import HarnessDeps
from .base import tool
from .read import MAX_ATTACHMENT_BYTES

DESCRIPTION = """- Fetches content from a specified URL
- Takes a URL and optional format as input
- Fetches the URL content, converts to requested format (markdown by default)
- Returns the content in the specified format
- Use this tool when you need to retrieve and analyze web content

Usage notes:
  - IMPORTANT: if another tool is present that offers better web fetching capabilities, is more targeted to the task, or has fewer restrictions, prefer using that tool instead of this one.
  - The URL must be a fully-formed valid URL
  - HTTP URLs will be automatically upgraded to HTTPS
  - Format options: "markdown" (default), "text", or "html"
  - This tool is read-only and does not modify any files
  - Results may be summarized if the content is very large"""

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class WebFetchSettings(BaseModel):
    allow_local_urls: bool = False
    """SSRF protection: private and cloud-metadata addresses are refused unless this is true."""
    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    headers: dict[str, str] | None = None
    """Extra request headers. The model picks the URL, so use `allowed_domains` if these carry credentials."""


def _domain_subject(args: dict[str, Any]) -> str | None:
    host = urlparse(str(args.get("url", ""))).hostname
    return f"domain:{host}" if host else None


@tool(settings=WebFetchSettings, subject=_domain_subject, description=DESCRIPTION)
async def WebFetch(
    ctx: RunContext[HarnessDeps],
    url: str,
    format: Literal["text", "markdown", "html"] = "markdown",
    timeout: float | None = None,
) -> str | ToolReturn:
    """Fetch a URL.

    Args:
        url: The URL to fetch content from
        format: The format to return the content in (text, markdown, or html). Defaults to markdown.
        timeout: Optional timeout in seconds (max 120)
    """
    if not url.startswith(("http://", "https://")):
        raise ModelRetry("URL must start with http:// or https://")
    settings = ctx.deps.settings("WebFetch", WebFetchSettings)
    seconds = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT[format], "Accept-Language": "en-US,en;q=0.9"}
    headers.update(settings.headers or {})

    try:
        response = await _download(url, headers, seconds, settings)
    except httpx2.HTTPStatusError as exc:
        # Cloudflare's bot check rejects the browser user agent over a non-browser TLS fingerprint; retry honestly.
        if exc.response.status_code == 403 and exc.response.headers.get("cf-mitigated") == "challenge":
            response = await _download(url, {**headers, "User-Agent": "ladderframe"}, seconds, settings)
        else:
            raise ModelRetry(f"Request failed with status code: {exc.response.status_code}") from exc

    content_type = response.headers.get("content-type", "")
    mime = content_type.split(";")[0].strip().lower()
    if mime in IMAGE_MIMES:
        if len(response.content) > MAX_ATTACHMENT_BYTES:
            raise ModelRetry(f"Image is too large to attach ({len(response.content)} bytes)")
        return ToolReturn("Image fetched successfully", content=[BinaryContent(data=response.content, media_type=mime)])

    content = response.content.decode("utf-8", errors="replace")
    if "text/html" in content_type and format == "markdown":
        return html_to_markdown(content)
    if "text/html" in content_type and format == "text":
        return html_to_text(content)
    return content


async def _download(url: str, headers: dict[str, str], timeout: int, settings: WebFetchSettings) -> Any:
    try:
        return await safe_download(
            url,
            allow_local=settings.allow_local_urls,
            timeout=timeout,
            headers=headers,
            allowed_domains=settings.allowed_domains,
            blocked_domains=settings.blocked_domains,
            max_bytes=MAX_RESPONSE_SIZE,
        )
    except httpx2.HTTPStatusError:
        raise
    except httpx2.TimeoutException as exc:
        raise ModelRetry("Request timed out") from exc
    except ValueError as exc:
        if "exceeds the maximum size" in str(exc):
            raise ModelRetry("Response too large (exceeds 5MB limit)") from exc
        raise ModelRetry(str(exc)) from exc
    except (httpx2.InvalidURL, httpx2.RequestError) as exc:
        raise ModelRetry(f"Failed to fetch {url}: {exc}") from exc


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "meta", "link"]):
        element.decompose()
    return markdownify(str(soup), heading_style="ATX", bullets="-", code_language="").strip()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "object", "embed"]):
        element.decompose()
    return soup.get_text().strip()
