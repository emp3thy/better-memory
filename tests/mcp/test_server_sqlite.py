"""MCP server boot + roundtrip test for the sqlite (FTS5) backend.

Verifies that ``create_server()`` runs without constructing any embedder
even with Ollama unreachable. Task 2 (remove-ollama-embeddings) deleted
the ``embeddings_backend == "ollama"`` branch entirely, so this no longer
needs to force ``BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite`` — the point now
holds regardless of that config value. ``memory.observe`` and
``memory.retrieve`` must round-trip without any HTTP call to Ollama, and
``cleanup()`` must not blow up on a ``None`` embedder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def test_create_server_without_ollama(
    tmp_memory_db: Path,
    tmp_knowledge_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server should start AND serve observe/retrieve without Ollama.

    The OLLAMA_HOST is pointed at an unresolvable address so any code
    path that still tries to embed via HTTP would surface as a clean
    failure on the first tool call. No such path exists any more —
    create_server never constructs an embedder, regardless of
    BETTER_MEMORY_EMBEDDINGS_BACKEND — FTS5 triggers index observations
    entirely in-database.
    """
    home = tmp_memory_db.parent
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("OLLAMA_HOST", "http://does-not-exist.invalid:1")

    from better_memory.mcp.server import _dispatch_for_tests, create_server

    server, cleanup, _ = create_server()
    try:
        assert server is not None
    finally:
        asyncio.run(cleanup())

    # End-to-end: observe a memory then drill it back out with a query.
    # Both calls run a fresh create_server() under the hood (via
    # _dispatch_for_tests), so this also catches any state that leaks
    # across server instances in sqlite mode. ``memory.retrieve_observations``
    # exercises the FTS5 search path when ``query`` is set.
    async def _roundtrip() -> list[dict]:
        observe_result = await _dispatch_for_tests(
            "memory.observe",
            {
                "content": "sqlite wiring smoke probe alpha bravo charlie",
                "outcome": "success",
                "component": "mcp",
            },
        )
        assert observe_result and observe_result[0].text
        retrieve_result = await _dispatch_for_tests(
            "memory.retrieve_observations",
            {"query": "sqlite wiring smoke probe", "limit": 5},
        )
        assert retrieve_result and retrieve_result[0].text
        return json.loads(retrieve_result[0].text)

    rows = asyncio.run(_roundtrip())
    # ``rows`` is a list of observation dicts ordered by query relevance.
    assert any(
        "sqlite wiring smoke probe" in (row.get("content") or "")
        for row in rows
    ), f"observed memory not returned by retrieve_observations; rows={rows!r}"
