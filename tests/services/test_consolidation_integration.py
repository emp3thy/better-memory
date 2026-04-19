"""Integration tests — real Ollama, off by default.

Run with: ``uv run pytest -m integration tests/services/test_consolidation_integration.py``

Requires a running Ollama instance reachable at ``$OLLAMA_HOST`` with
the model specified in ``$CONSOLIDATE_MODEL`` (default ``llama3``)
pulled locally.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.llm.ollama import OllamaChat
from better_memory.services.consolidation import ConsolidationService


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.mark.integration
async def test_real_ollama_drafts_insight(conn: sqlite3.Connection) -> None:
    for i in range(3):
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, status, "
            "validated_true, outcome) "
            "VALUES (?, ?, 'p', 'api', 'retry', 'active', 1, 'success')",
            (f"o{i}", f"Observation {i}: retry 5xx with backoff."),
        )
    conn.commit()

    chat = OllamaChat()  # real client, reads config
    try:
        svc = ConsolidationService(conn=conn, chat=chat)
        candidates = await svc.branch_dry_run(project="p")
    finally:
        await chat.aclose()

    assert len(candidates) == 1
    c = candidates[0]
    # Non-empty, non-whitespace drafted content.
    assert c.content.strip()
    assert c.polarity == "do"
    assert c.observation_ids == ["o0", "o1", "o2"]
