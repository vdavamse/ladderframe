"""FastAPI server (`ladderframe serve`, extra: `ladderframe[server]`). See app.py for the routes."""

from .app import create_app

__all__ = ["create_app"]
