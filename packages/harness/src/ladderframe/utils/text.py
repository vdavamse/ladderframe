from __future__ import annotations


def truncate(text: str, limit: int, label: str = "output") -> str:
    """Keep the head and tail of long text so tool results stay within the model's (and Temporal's) budget."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n[... {omitted} characters of {label} truncated ...]\n\n{text[-tail:]}"
