"""Tests for :class:`better_memory.services.observation.ObservationService`.

These tests use an in-memory (temp-file) migrated SQLite database and a mock
embedder — they do NOT contact Ollama. Async tests rely on
``asyncio_mode = "auto"`` from pyproject.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import EmbeddingError
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService

# Deterministic 768-length vector used by the mock embedder.
_VEC_768 = [0.01] * 768


class _StubEmbedder:
    """Minimal mock of :class:`OllamaEmbedder` for unit tests."""

    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        raise_on_embed: Exception | None = None,
    ) -> None:
        self._vector = vector if vector is not None else list(_VEC_768)
        self._raise = raise_on_embed
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self._raise is not None:
            raise self._raise
        return list(self._vector)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock() -> Any:
    """A deterministic clock returning a fixed UTC datetime."""
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def service(conn: sqlite3.Connection, fixed_clock: Any) -> ObservationService:
    embedder = _StubEmbedder()
    return ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: None,
        session_id="sess-abc",
        episodes=EpisodeService(conn),
    )


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


async def test_create_returns_non_empty_id(service: ObservationService) -> None:
    obs_id = await service.create("hello world", component="auth")
    assert isinstance(obs_id, str)
    assert obs_id  # non-empty


async def test_create_inserts_observation_with_defaults(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("hello world", component="auth")

    row = conn.execute(
        "SELECT id, content, project, component, outcome, reinforcement_score, "
        "scope_path, status, used_count, validated_true, validated_false "
        "FROM observations WHERE id = ?",
        (obs_id,),
    ).fetchone()

    assert row is not None
    assert row["id"] == obs_id
    assert row["content"] == "hello world"
    assert row["project"] == "test-project"
    assert row["component"] == "auth"
    assert row["outcome"] == "neutral"
    assert row["reinforcement_score"] == pytest.approx(0.0)
    assert row["scope_path"] is None
    assert row["status"] == "active"
    assert row["used_count"] == 0
    assert row["validated_true"] == 0
    assert row["validated_false"] == 0


async def test_create_stores_success_outcome(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("positive example", outcome="success")
    row = conn.execute(
        "SELECT outcome FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row["outcome"] == "success"


async def test_create_stores_scope_path_argument(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("scoped note", scope_path="foo/bar")
    row = conn.execute(
        "SELECT scope_path FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row["scope_path"] == "foo/bar"


async def test_create_uses_scope_resolver_when_arg_not_given(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    embedder = _StubEmbedder()
    svc = ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: "auto/scope",
        session_id="sess-abc",
        episodes=EpisodeService(conn),
    )
    obs_id = await svc.create("auto-scoped")
    row = conn.execute(
        "SELECT scope_path FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row["scope_path"] == "auto/scope"


async def test_create_project_argument_overrides_resolver(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("overridden project", project="other-proj")
    row = conn.execute(
        "SELECT project FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row["project"] == "other-proj"


async def test_create_defaults_project_to_cwd_name_when_no_resolver(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    embedder = _StubEmbedder()
    svc = ObservationService(conn, embedder, clock=fixed_clock, session_id="s", episodes=EpisodeService(conn))
    obs_id = await svc.create("no resolver")
    row = conn.execute(
        "SELECT project FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row["project"] == Path.cwd().name


async def test_create_populates_fts_via_trigger(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("hello world is great", component="auth")

    # The base-table rowid is the integer INTEGER rowid, not the text id; look
    # it up via the observations table to correlate.
    obs_rowid = conn.execute(
        "SELECT rowid FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()["rowid"]

    matches = conn.execute(
        "SELECT rowid FROM observation_fts WHERE observation_fts MATCH 'hello'"
    ).fetchall()
    assert any(r["rowid"] == obs_rowid for r in matches)


async def test_create_stores_embedding(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("embedded text")
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM observation_embeddings WHERE observation_id = ?",
        (obs_id,),
    ).fetchone()["c"]
    assert count == 1


async def test_create_writes_audit_row(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("audited", component="auth", outcome="success")

    rows = conn.execute(
        "SELECT entity_type, entity_id, action, actor, detail, session_id "
        "FROM audit_log WHERE entity_id = ?",
        (obs_id,),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_type"] == "observation"
    assert row["entity_id"] == obs_id
    assert row["action"] == "created"
    assert row["actor"] == "ai"
    assert row["session_id"] == "sess-abc"
    detail = json.loads(row["detail"])
    assert detail["outcome"] == "success"
    assert detail["component"] == "auth"
    assert detail["scope_path"] is None


async def test_create_rolls_back_on_embedder_failure(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    bad_embedder = _StubEmbedder(raise_on_embed=EmbeddingError("boom"))
    svc = ObservationService(
        conn,
        bad_embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: None,
        session_id="sess-abc",
        episodes=EpisodeService(conn),
    )

    with pytest.raises(EmbeddingError):
        await svc.create("doomed")

    # Nothing persisted.
    obs_count = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
    emb_count = conn.execute(
        "SELECT COUNT(*) AS c FROM observation_embeddings"
    ).fetchone()["c"]
    audit_count = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
    assert obs_count == 0
    assert emb_count == 0
    assert audit_count == 0

    # Fail-fast contract (Phase 2 caveat): episode lazy-open commits before
    # the embed call, so a background episode and its episode_sessions row
    # may be present after an embed-failure. Lock the shape so a future
    # refactor that tightens the contract (no DB artifacts at all) surfaces
    # this test failing loudly.
    episodes_rows = conn.execute("SELECT goal, ended_at FROM episodes").fetchall()
    # Exactly one background episode (goal NULL, ended_at NULL) may exist.
    assert len(episodes_rows) <= 1
    if episodes_rows:
        assert episodes_rows[0]["goal"] is None
        assert episodes_rows[0]["ended_at"] is None

    session_rows = conn.execute(
        "SELECT left_at FROM episode_sessions"
    ).fetchall()
    assert len(session_rows) <= 1
    if session_rows:
        assert session_rows[0]["left_at"] is None


# ---------------------------------------------------------------------------
# record_use()
# ---------------------------------------------------------------------------


async def test_record_use_bumps_used_count_only_when_no_outcome(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("plain")
    service.record_use(obs_id)

    row = conn.execute(
        "SELECT used_count, validated_true, validated_false, reinforcement_score, "
        "last_used, last_validated FROM observations WHERE id = ?",
        (obs_id,),
    ).fetchone()
    assert row["used_count"] == 1
    assert row["validated_true"] == 0
    assert row["validated_false"] == 0
    assert row["reinforcement_score"] == pytest.approx(0.0)
    assert row["last_used"] is not None
    assert row["last_validated"] is None


async def test_record_use_raises_for_unknown_id(service: ObservationService) -> None:
    with pytest.raises(ValueError):
        service.record_use("nonexistent-id")


async def test_record_use_writes_audit_row(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("to-be-used")
    service.record_use(obs_id, outcome="success")

    audit_rows = conn.execute(
        "SELECT action, actor, detail, session_id FROM audit_log "
        "WHERE entity_id = ? ORDER BY created_at",
        (obs_id,),
    ).fetchall()
    assert len(audit_rows) == 2  # created + used
    used = audit_rows[1]
    assert used["action"] == "used"
    assert used["actor"] == "ai"
    assert used["session_id"] == "sess-abc"
    detail = json.loads(used["detail"])
    assert detail["outcome"] == "success"


# ---------------------------------------------------------------------------
# Round-trip verification (the plan's explicit check)
# ---------------------------------------------------------------------------


async def test_roundtrip_success_and_failure_move_scores_opposite(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    a_id = await service.create("alpha")
    service.record_use(a_id, outcome="success")

    b_id = await service.create("beta")
    service.record_use(b_id, outcome="failure")

    a = conn.execute(
        "SELECT used_count, validated_true, validated_false, "
        "reinforcement_score, last_used, last_validated "
        "FROM observations WHERE id = ?",
        (a_id,),
    ).fetchone()
    b = conn.execute(
        "SELECT used_count, validated_true, validated_false, "
        "reinforcement_score, last_used, last_validated "
        "FROM observations WHERE id = ?",
        (b_id,),
    ).fetchone()

    assert a["used_count"] == 1
    assert a["validated_true"] == 1
    assert a["validated_false"] == 0
    assert a["reinforcement_score"] == pytest.approx(1.0)
    assert a["last_used"] is not None
    assert a["last_validated"] is not None

    assert b["used_count"] == 1
    assert b["validated_true"] == 0
    assert b["validated_false"] == 1
    assert b["reinforcement_score"] == pytest.approx(-1.0)
    assert b["last_used"] is not None
    assert b["last_validated"] is not None


async def test_multiple_successes_accumulate_score(
    conn: sqlite3.Connection, service: ObservationService
) -> None:
    obs_id = await service.create("repeat me")
    service.record_use(obs_id, outcome="success")
    service.record_use(obs_id, outcome="success")
    service.record_use(obs_id, outcome="failure")

    row = conn.execute(
        "SELECT used_count, validated_true, validated_false, reinforcement_score "
        "FROM observations WHERE id = ?",
        (obs_id,),
    ).fetchone()
    assert row["used_count"] == 3
    assert row["validated_true"] == 2
    assert row["validated_false"] == 1
    # 1.0 + 1.0 - 1.0 = 1.0
    assert row["reinforcement_score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# CLAUDE_SESSION_ID env-var resolution
# ---------------------------------------------------------------------------


def test_session_id_resolves_from_env_var(
    conn: sqlite3.Connection, fixed_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When CLAUDE_SESSION_ID is set and no session_id kwarg, use the env var."""
    from better_memory.services.episode import EpisodeService
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-sess-abc")
    embedder = _StubEmbedder()
    svc = ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        episodes=EpisodeService(conn),
    )
    assert svc._session_id == "claude-sess-abc"


def test_session_id_kwarg_overrides_env_var(
    conn: sqlite3.Connection, fixed_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit session_id kwarg beats the env var."""
    from better_memory.services.episode import EpisodeService
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-sess-abc")
    embedder = _StubEmbedder()
    svc = ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        session_id="explicit-sess",
        episodes=EpisodeService(conn),
    )
    assert svc._session_id == "explicit-sess"


def test_session_id_falls_back_to_uuid_when_no_env(
    conn: sqlite3.Connection, fixed_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without CLAUDE_SESSION_ID or explicit kwarg, generate a uuid4."""
    from better_memory.services.episode import EpisodeService
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    embedder = _StubEmbedder()
    svc = ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        episodes=EpisodeService(conn),
    )
    assert svc._session_id  # non-empty
    assert svc._session_id != "claude-sess-abc"  # random, unpredictable
    assert len(svc._session_id) == 32  # uuid4().hex length
