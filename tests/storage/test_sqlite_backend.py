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


def test_sqlite_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_sqlite_backend_shares_caller_sync_embedder(memory_conn) -> None:
    """Regression for PR #83: SqliteBackend must NOT build its own
    SyncEmbedder — it must reuse the caller's instance so the circuit
    breaker is process-wide, not split in two. Neither _synthesis nor
    _semantic take a sync_embedder at all any more — Task 4
    (remove-ollama-embeddings) removed the parameter from
    SemanticMemoryService, and Task 5 removed it from
    ReflectionSynthesisService. SqliteBackend keeps its own
    ``_sync_embedder`` attribute solely to embed the query for
    ``relevance_ranks``' vector leg."""
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.0] * 768)
    sentinel_sync_embedder = MagicMock(name="sentinel-sync-embedder")

    backend = SqliteBackend(
        memory_conn=memory_conn,
        embedder=embedder,
        sync_embedder=sentinel_sync_embedder,
        session_id="test-session",
        project="testproj",
    )

    assert backend._sync_embedder is sentinel_sync_embedder


def test_sqlite_backend_sync_embedder_defaults_to_none(backend) -> None:
    """When the caller passes no sync_embedder (default), the backend must
    not silently construct its own."""
    assert backend._sync_embedder is None


def test_sqlite_backend_implements_hot_path_methods(backend) -> None:
    """Task 3 surface check: every hot-path method is present and callable."""
    for name in ("observe", "retrieve", "list_observations", "record_use"):
        assert callable(getattr(backend, name)), f"Backend missing {name}"


def test_supports_synthesis_is_true(backend) -> None:
    assert backend.supports_synthesis is True


def test_supports_episodes_is_true(backend) -> None:
    """SqliteBackend exposes episode lifecycle; UI shows the Episodes tab."""
    assert backend.supports_episodes is True


@pytest.mark.asyncio
async def test_observe_returns_string_id(backend) -> None:
    obs_id = await backend.observe(
        content="test observation",
        outcome="success",
        theme="test",
    )
    assert isinstance(obs_id, str) and obs_id


def test_retrieve_returns_polarity_bucketed_reflections(backend) -> None:
    """Plan 2 Task 0 amendment: retrieve now wraps ReflectionSynthesisService."""
    result = backend.retrieve(project="testproj", limit_per_bucket=3)
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"do", "dont", "neutral"}
    for bucket in ("do", "dont", "neutral"):
        assert isinstance(result[bucket], list)


def test_retrieve_passes_through_polarity_filter(backend, memory_conn) -> None:
    """polarity=do filters the result to only the do bucket (others empty)."""
    # Seed one reflection per polarity. Required NOT-NULL columns mirror the
    # promote/retire test's seed: phase + use_cases + hints + created_at + updated_at.
    memory_conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        " confidence, status, scope, created_at, updated_at) VALUES "
        "('r-do', 'do-refl', 'testproj', 'general', 'do', 'uc', '[]', "
        " 0.9, 'confirmed', 'project', "
        " '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z'),"
        "('r-dont', 'dont-refl', 'testproj', 'general', 'dont', 'uc', '[]', "
        " 0.9, 'confirmed', 'project', "
        " '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')"
    )
    memory_conn.commit()
    result = backend.retrieve(project="testproj", polarity="do", limit_per_bucket=10)
    do_ids = [r["id"] for r in result["do"]]
    assert "r-do" in do_ids
    assert "r-dont" not in do_ids


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
    rows = backend.semantic_list()
    matching = [r for r in rows if getattr(r, "id", None) == sm_id]
    assert len(matching) == 1
    assert getattr(matching[0], "content", None) == "updated"


def test_semantic_set_scope(backend, memory_conn) -> None:
    sm_id = backend.semantic_observe(content="to be promoted", scope="project")
    backend.semantic_set_scope(id=sm_id, scope="general")
    row = memory_conn.execute(
        "SELECT scope FROM semantic_memories WHERE id = ?", (sm_id,),
    ).fetchone()
    assert row["scope"] == "general"


def test_semantic_delete(backend) -> None:
    sm_id = backend.semantic_observe(content="to be deleted")
    backend.semantic_delete(id=sm_id)
    rows = backend.semantic_list()
    assert not any(getattr(r, "id", None) == sm_id for r in rows)


def test_open_and_close_background_episode(backend, memory_conn) -> None:
    ep_id = backend.open_background_episode(
        session_id="test-session", project="testproj",
    )
    assert ep_id
    # close_reason is constrained by schema to a fixed enum; "goal_complete"
    # is the canonical "happy path" reason. Plan's "test" string is rejected.
    backend.close_episode_by_id(
        episode_id=ep_id, outcome="success", close_reason="goal_complete",
    )
    row = memory_conn.execute(
        "SELECT ended_at, outcome, close_reason FROM episodes WHERE id = ?",
        (ep_id,),
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["outcome"] == "success"
    assert row["close_reason"] == "goal_complete"


def test_list_episodes_returns_list(backend) -> None:
    ep_id = backend.open_background_episode(
        session_id="list-test-session", project="testproj",
    )
    result = backend.list_episodes(project="testproj")
    assert isinstance(result, list)
    assert any(getattr(ep, "id", None) == ep_id for ep in result)


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


# ----- Task 5b: synthesis + session bootstrap + ratings -----


def test_session_bootstrap_returns_result(backend) -> None:
    result = backend.session_bootstrap(session_id="test-session", project="testproj")
    assert result.project == "testproj"
    assert result.episode_action in {"opened", "attached"}


def test_list_session_exposures_returns_envelope(backend) -> None:
    result = backend.list_session_exposures(session_id="test-session")
    assert isinstance(result, dict)
    assert result.get("session_id") == "test-session"
    assert "exposures" in result


def test_apply_session_ratings_empty_raises(backend) -> None:
    # Service raises ValueError on empty ratings — the wrapper must surface it.
    with pytest.raises(ValueError):
        backend.apply_session_ratings(session_id="test-session", ratings=[])


def test_credit_one_for_missing_memory_returns_skip(backend, memory_conn) -> None:
    # Seed an exposure for a memory that doesn't actually exist anywhere.
    memory_conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) VALUES "
        "('test-session', 'reflection', 'does-not-exist', '2026-05-25T00:00:00Z', 'bootstrap')"
    )
    memory_conn.commit()

    result = backend.credit_one(
        session_id="test-session",
        kind="reflection",
        id="does-not-exist",
        classification="cited",
        evidence="checking the missing-memory skip path",
    )
    assert result["applied"] is None
    assert result["skipped"] == "memory_missing"


def test_synthesize_next_get_context_returns_none_when_no_pending(backend) -> None:
    # Fresh in-memory db — no pending episodes for synthesis.
    assert backend.synthesize_next_get_context(project="testproj") is None


# ----- Task 6: relevance_ranks -----


def test_relevance_ranks_finds_seeded_fts_row(backend, memory_conn) -> None:
    """Protocol-completeness check: a reflection whose title matches the
    query via BM25 (reflection_fts, auto-populated by the schema's insert
    trigger) shows up in the returned (kind, id) -> rank map."""
    memory_conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        " confidence, status, scope, created_at, updated_at) VALUES "
        "('refl-fts-1', 'Retention archives by confidence', 'testproj', "
        " 'general', 'do', 'uc', '[]', 0.9, 'confirmed', 'project', "
        " '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')"
    )
    memory_conn.commit()

    result = backend.relevance_ranks(
        query="how does retention archive things", kinds=("reflection",),
    )
    assert result == {("reflection", "refl-fts-1"): 0}


def test_relevance_ranks_blank_query_returns_empty(backend) -> None:
    assert backend.relevance_ranks(query="   ") == {}


def test_relevance_ranks_no_match_returns_empty(backend) -> None:
    assert backend.relevance_ranks(query="nothing matches anything here") == {}


def test_relevance_ranks_kinds_filter(backend, memory_conn) -> None:
    """Requesting only kinds=("semantic",) must not surface reflection
    matches, even when a matching reflection exists."""
    memory_conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        " confidence, status, scope, created_at, updated_at) VALUES "
        "('refl-fts-2', 'Retention archives by confidence', 'testproj', "
        " 'general', 'do', 'uc', '[]', 0.9, 'confirmed', 'project', "
        " '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z')"
    )
    memory_conn.commit()

    result = backend.relevance_ranks(
        query="how does retention archive things", kinds=("semantic",),
    )
    assert result == {}


def test_new_capability_flags_all_true(backend) -> None:
    """The five UI content-capability flags are all True on sqlite --
    sqlite is the full-feature backend."""
    assert backend.supports_observations is True
    assert backend.supports_provenance is True
    assert backend.supports_retention_runs is True
    assert backend.supports_reflection_review is True
    assert backend.supports_reflection_text_edit is True


def test_supports_episodes_unchanged_regression(backend) -> None:
    """Regression pin: the pre-existing episodes flag is untouched by the
    flag-addition phase."""
    assert backend.supports_episodes is True


def test_reflection_get_returns_row_without_sources(backend, memory_conn):
    from dataclasses import asdict

    from better_memory.ui import queries

    memory_conn.execute(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "('r1','t','testproj','general','do','uc','[]',0.5,'confirmed',0,"
        "'project','2026-04-01T00:00:00+00:00','2026-04-01T00:00:00+00:00')"
    )
    memory_conn.commit()
    got = backend.reflection_get(reflection_id="r1")
    detail = queries.reflection_detail(memory_conn, reflection_id="r1")
    assert detail is not None
    assert got == asdict(detail.reflection)
    assert "sources" not in got


def test_reflection_get_missing_returns_none(backend):
    assert backend.reflection_get(reflection_id="nope") is None


def test_reflection_list_matches_queries_for_ui(backend, memory_conn):
    from dataclasses import asdict

    from better_memory.ui import queries

    memory_conn.executemany(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "(?, ?, 'testproj', 'general', 'do', 'uc', '[]', ?, ?, 0, 'project', "
        "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
        [("a", "A", 0.9, "confirmed"),
         ("b", "B", 0.5, "confirmed"),
         ("c", "C", 0.5, "retired")],
    )
    memory_conn.commit()
    got = backend.reflection_list(project="testproj")
    expect = [asdict(r) for r in queries.reflection_list_for_ui(
        memory_conn, project="testproj")]
    assert got == expect
    # default excludes retired; explicit status='retired' surfaces it
    assert [r["id"] for r in got] == ["a", "b"]
    retired = backend.reflection_list(project="testproj", status="retired")
    assert [r["id"] for r in retired] == ["c"]


def test_distinct_projects_matches_reflection_query(backend, memory_conn):
    """Identity pin: SqliteBackend.distinct_projects == reflection_distinct_projects."""
    from better_memory.ui import queries

    memory_conn.executemany(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, scope, "
        "created_at, updated_at) VALUES "
        "(?, ?, ?, 'general', 'do', 'uc', '[]', 0.9, 'confirmed', 0, "
        "'project', '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
        [("a", "A", "zeta-proj"), ("b", "B", "alpha-proj")],
    )
    memory_conn.commit()
    expected = queries.reflection_distinct_projects(memory_conn)
    assert backend.distinct_projects() == expected
    # Must be non-empty for the identity pin to be meaningful.
    assert expected


def test_semantic_get_returns_model(backend, memory_conn):
    from better_memory.services.semantic import SemanticMemory
    memory_conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) VALUES "
        "('s1','the rule','testproj','project',"
        "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:00+00:00')"
    )
    memory_conn.commit()
    got = backend.semantic_get(id="s1")
    assert isinstance(got, SemanticMemory)
    assert got.content == "the rule"
    assert backend.semantic_get(id="nope") is None


