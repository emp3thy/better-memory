"""Tests for diagnostics Recent ratings panel + session_id_missing counter."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_rated_exposure(conn, sid, kind, mid, classification="cited"):
    """Insert an exposure that's already been rated."""
    conn.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source,
            rated_at, classification)
           VALUES (?, ?, ?, '2026-05-11T10:00:00+00:00', 'bootstrap',
                   '2026-05-11T11:00:00+00:00', ?)""",
        (sid, kind, mid, classification),
    )
    conn.commit()


def _seed_reflection(conn, rid, title="Some title"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, title),
    )
    conn.commit()


class TestSessionIdMissingCounter:
    def test_retrieve_reflections_bumps_counter_when_env_missing(
        self, conn, monkeypatch,
    ):
        """When CLAUDE_SESSION_ID is unset, retrieve_reflections increments
        rating_diagnostics.session_id_missing."""
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p")  # default track_exposure=True

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value >= 1

    def test_semantic_list_bumps_counter_when_env_missing(
        self, conn, monkeypatch,
    ):
        from better_memory.services.semantic import SemanticMemoryService

        conn.execute(
            """INSERT INTO semantic_memories
               (id, content, project, scope, created_at, updated_at)
               VALUES ('s1', 'fact', 'p', 'project',
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = SemanticMemoryService(conn)
        svc.list_for_project(project="p")  # default track_exposure=True

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value >= 1

    def test_counter_not_bumped_when_track_exposure_false(
        self, conn, monkeypatch,
    ):
        """Bootstrap-style callers passing track_exposure=False must NOT
        bump the counter (no env-missing inflation from legitimate
        non-Claude integration tests)."""
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", track_exposure=False)

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value == 0


class TestDiagnosticsPanel:
    """Verify the Flask route exposes Recent ratings + diagnostics counter."""

    def test_recent_ratings_panel_lists_rated_exposures(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        _seed_reflection(conn, "r1", title="My Reflection Title")
        _seed_rated_exposure(conn, "S1", "reflection", "r1", "cited")

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app()
        client = app.test_client()
        response = client.get("/diagnostics")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Recent ratings" in body
        assert "r1" in body
        assert "cited" in body
        assert "My Reflection Title" in body

    def test_session_id_missing_counter_displayed(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        conn.execute(
            "UPDATE rating_diagnostics SET value=3 WHERE metric='session_id_missing'"
        )
        conn.commit()

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app()
        client = app.test_client()
        response = client.get("/diagnostics")
        body = response.data.decode("utf-8")
        assert "session_id_missing" in body
        assert "3" in body
