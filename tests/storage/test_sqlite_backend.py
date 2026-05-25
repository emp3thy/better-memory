"""Smoke tests for SqliteBackend.

These verify the wrapper delegates correctly to underlying services. We do
NOT re-test service business logic — that lives in tests/services/.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlite_vec

from better_memory.storage import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    """An in-memory sqlite connection with sqlite-vec loaded and migrations applied."""
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Migrations create vec0 virtual tables, so the extension must be loaded first.
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    return conn


@pytest.fixture
def backend(memory_conn) -> SqliteBackend:
    """A SqliteBackend wired to an in-memory db with a no-op embedder.

    ObservationService awaits embedder.embed, so we use AsyncMock for that
    coroutine while keeping a sync MagicMock for any other attribute access.
    """
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.0] * 768)
    return SqliteBackend(
        memory_conn=memory_conn,
        embedder=embedder,
        session_id="test-session",
        project="testproj",
    )


# NOTE: Full Protocol satisfaction is asserted again — without xfail — at the
# end of Task 5b, once SqliteBackend implements every method on the Protocol.
# In Task 3 only the hot-path (observe / retrieve / list_observations /
# record_use) is wrapped, so this isinstance check is expected to fail until
# Tasks 4 and 5b land. The xfail is intentional; do not delete it.
@pytest.mark.xfail(
    reason="Protocol coverage completes in Task 5b; Task 3 only wraps the hot path.",
    strict=True,
)
def test_sqlite_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_sqlite_backend_implements_hot_path_methods(backend) -> None:
    """Task 3 surface check: every hot-path method is present and callable."""
    for name in ("observe", "retrieve", "list_observations", "record_use"):
        assert callable(getattr(backend, name)), f"Backend missing {name}"


def test_supports_synthesis_is_true(backend) -> None:
    assert backend.supports_synthesis is True


@pytest.mark.asyncio
async def test_observe_returns_string_id(backend) -> None:
    obs_id = await backend.observe(
        content="test observation",
        outcome="success",
        theme="test",
    )
    assert isinstance(obs_id, str) and obs_id


@pytest.mark.asyncio
async def test_retrieve_returns_bucketed_results(backend) -> None:
    result = await backend.retrieve(query="anything", do_limit=3, dont_limit=3, neutral_limit=3)
    # BucketedResults is the service-defined return; we just confirm it has the buckets.
    do_bucket = getattr(result, "do", None) if not isinstance(result, dict) else result.get("do")
    assert do_bucket is not None


@pytest.mark.asyncio
async def test_list_observations_returns_list(backend) -> None:
    await backend.observe(content="findable text", theme="searchable")
    result = await backend.list_observations(query="findable", limit=5)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_record_use_credits_recorded_observation(backend) -> None:
    obs_id = await backend.observe(content="will be credited", theme="x")
    backend.record_use(obs_id, outcome="success")  # No exception = pass.
