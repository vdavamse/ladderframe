from ladderframe import tool


@tool
def Echo(text: str) -> str:
    """Echo text back.

    Args:
        text: The text.
    """
    return f"echo: {text}"
