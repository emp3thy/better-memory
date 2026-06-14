"""Tests for retrieve_relevant over a real sqlite StorageBackend."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.relevant import RelevantMemory, retrieve_relevant
from better_memory.services.semantic import SemanticMemoryService
from better_memory.storage.sqlite import SqliteBackend


@pytest.fixture
def backend(tmp_memory_db: Path):
    conn = connect(tmp_memory_db)
    apply_migrations(conn)
    sem = SemanticMemoryService(conn)
    sem.create(content="Always write the implementation plan with confidence scores",
               project="proj", scope="project")
    sem.create(content="Never ask the user to babysit a PR", project="other", scope="general")
    sem.create(content="Prefer tea over coffee", project="proj", scope="project")
    try:
        yield SqliteBackend(memory_conn=conn, embedder=None, session_id=None, project="proj")
    finally:
        conn.close()


def test_filters_to_keyword_matches(backend):
    out = retrieve_relevant(backend, query="let us write the plan", project="proj", limit=5)
    texts = " ".join(m.summary.lower() for m in out)
    assert "plan" in texts
    assert "coffee" not in texts          # irrelevant memory excluded


def test_includes_general_scope(backend):
    out = retrieve_relevant(backend, query="babysit the PR", project="proj", limit=5)
    assert any("babysit" in m.summary.lower() for m in out)  # general-scope semantic matched


def test_empty_on_no_match(backend):
    assert retrieve_relevant(backend, query="xylophone zeppelin", project="proj", limit=5) == []


def test_empty_on_empty_query(backend):
    assert retrieve_relevant(backend, query="   ", project="proj", limit=5) == []


def test_respects_limit(backend):
    out = retrieve_relevant(
        backend, query="plan confidence babysit pr user", project="proj", limit=1
    )
    assert len(out) == 1


def test_returns_relevantmemory(backend):
    out = retrieve_relevant(backend, query="plan", project="proj", limit=5)
    assert out and all(isinstance(m, RelevantMemory) for m in out)
    assert all(m.kind in ("reflection", "semantic") for m in out)
