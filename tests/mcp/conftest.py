"""MCP test fixtures and collection hooks.

When Ollama is not reachable on ``localhost:11434`` we auto-skip the
subprocess-based MCP integration tests so contributors without a running
Ollama can still run the default suite. Non-integration tests in this
directory (e.g. ``test_parse_window.py``) are not affected.
"""

from __future__ import annotations

import httpx
import pytest


def _ollama_up() -> bool:
    """Return ``True`` iff Ollama responds on its default tags endpoint."""
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=1.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip MCP integration tests when Ollama is not reachable."""
    if _ollama_up():
        return
    skip_marker = pytest.mark.skip(
        reason="Ollama not reachable at localhost:11434"
    )
    for item in items:
        path = str(item.path).replace("\\", "/")
        if "tests/mcp/" in path and "test_server_integration" in path:
            item.add_marker(skip_marker)
