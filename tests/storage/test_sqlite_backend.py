"""Smoke tests for SqliteBackend.

These verify the wrapper delegates correctly to underlying services. We do
NOT re-test service business logic — that lives in tests/services/.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlite_vec

from better_memory.services.observation import BucketedResults
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
    assert isinstance(result, BucketedResults)
    assert isinstance(result.do, list)


@pytest.mark.asyncio
async def test_list_observations_returns_recorded_observation(backend) -> None:
    obs_id = await backend.observe(content="findable text", theme="searchable")
    result = await backend.list_observations(theme="searchable", limit=5)
    assert any(r["id"] == obs_id for r in result)


@pytest.mark.asyncio
async def test_record_use_credits_recorded_observation(backend, memory_conn) -> None:
    obs_id = await backend.observe(content="will be credited", theme="x")
    backend.record_use(obs_id, outcome="success")
    row = memory_conn.execute(
        "SELECT used_count, validated_true FROM observations WHERE id = ?",
        (obs_id,),
    ).fetchone()
    assert row["used_count"] == 1
    assert row["validated_true"] == 1


# ----- Task 4: semantic + episode + reflection lifecycle -----


def test_semantic_observe_and_list(backend) -> None:
    sm_id = backend.semantic_observe(content="prefer uv over pip")
    rows = backend.semantic_list()
    assert any(getattr(r, "id", None) == sm_id for r in rows)


def test_semantic_update_text(backend) -> None:
    sm_id = backend.semantic_observe(content="original")
    backend.semantic_update_text(id=sm_id, content="updated")
    rows = backend.semantic_list(search="updated")
    assert any(getattr(r, "id", None) == sm_id for r in rows)


def test_semantic_set_scope(backend) -> None:
    sm_id = backend.semantic_observe(content="to be promoted", scope="project")
    backend.semantic_set_scope(id=sm_id, scope="general")
    # Listing for the project no longer surfaces general-scope rows by default;
    # the call should succeed and not raise.


def test_semantic_delete(backend) -> None:
    sm_id = backend.semantic_observe(content="to be deleted")
    backend.semantic_delete(id=sm_id)
    rows = backend.semantic_list()
    assert not any(getattr(r, "id", None) == sm_id for r in rows)


def test_open_and_close_background_episode(backend) -> None:
    ep_id = backend.open_background_episode(
        session_id="test-session", project="testproj",
    )
    assert ep_id
    # close_reason is constrained by schema to a fixed enum; "goal_complete"
    # is the canonical "happy path" reason. Plan's "test" string is rejected.
    backend.close_episode_by_id(
        episode_id=ep_id, outcome="success", close_reason="goal_complete",
    )


def test_list_episodes_returns_list(backend) -> None:
    assert isinstance(backend.list_episodes(), list)


def test_promote_and_retire_reflection(backend, memory_conn) -> None:
    # Seed a confirmed reflection so promote/retire have a valid target.
    # Schema-aware seed: confirmed status, project-scope. The reflections table
    # also requires NOT-NULL phase / created_at / updated_at (no defaults), so
    # we supply sensible values for those even though the plan's INSERT skipped
    # them.
    memory_conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, scope, created_at, updated_at) VALUES "
        "('refl-test-1', 'T', 'testproj', 'general', 'do', 'U', 'H', "
        "0.9, 'confirmed', 'project', '2026-05-25T00:00:00Z', "
        "'2026-05-25T00:00:00Z')"
    )
    memory_conn.commit()

    backend.promote_reflection(reflection_id="refl-test-1")
    row = memory_conn.execute(
        "SELECT scope FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["scope"] == "general"

    backend.retire_reflection(reflection_id="refl-test-1")
    row = memory_conn.execute(
        "SELECT status FROM reflections WHERE id=?", ("refl-test-1",)
    ).fetchone()
    assert row["status"] == "retired"
