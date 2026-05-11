"""Tests for exposure tracking on bootstrap + mid-session retrieve paths."""
from __future__ import annotations

from datetime import UTC, datetime
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


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_reflection(conn, rid, project="p"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, status, created_at, updated_at)
           VALUES (?, 't', ?, 'general', 'do', 'uc', '[]', 0.5, 'confirmed',
                   '2026-01-01', '2026-01-01')""",
        (rid, project),
    )
    conn.commit()


def _seed_semantic(conn, sid, project="p"):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', ?, 'project', '2026-01-01', '2026-01-01')""",
        (sid, project),
    )
    conn.commit()


class TestBootstrapExposureWrite:
    def test_bootstrap_writes_exposure_rows_for_injected_memories(
        self, conn, fixed_clock,
    ):
        """When bootstrap injects reflections + semantic memories, an
        exposure row is recorded for each."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_semantic(conn, "s1")

        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="S1")

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
            "WHERE session_id = ? ORDER BY memory_kind, memory_id",
            ("S1",),
        ).fetchall()
        kinds_ids = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert ("reflection", "r1") in kinds_ids
        assert ("reflection", "r2") in kinds_ids
        assert ("semantic",   "s1") in kinds_ids
        assert all(r["source"] == "bootstrap" for r in rows)

    def test_bootstrap_skips_exposure_when_no_session_id(
        self, conn, fixed_clock,
    ):
        """If session_id is missing, no exposure rows are written
        (no synthetic ids)."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="")  # empty

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []

    def test_bootstrap_exposure_uses_now_as_exposed_at(
        self, conn, fixed_clock,
    ):
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="S1")

        row = conn.execute(
            "SELECT exposed_at FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["exposed_at"] == "2026-05-11T12:00:00+00:00"

    def test_bootstrap_writes_no_rows_when_no_memories_to_inject(
        self, conn, fixed_clock,
    ):
        """When a project has no reflections and no semantic memories,
        bootstrap must NOT write any exposure rows (the early-return
        `if not rows` guard in _record_exposure)."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        # Seed nothing.
        svc = SessionBootstrapService(conn, clock=fixed_clock)
        svc.bootstrap(project="p", session_id="S1")

        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM session_memory_exposure"
        ).fetchone()
        assert rows["n"] == 0


class TestReflectionRetrieveExposureWrite:
    def test_retrieve_writes_exposure_rows(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source "
            "FROM session_memory_exposure WHERE session_id='S1'"
        ).fetchall()
        ids = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert ("reflection", "r1") in ids
        assert ("reflection", "r2") in ids
        assert all(r["source"] == "retrieve" for r in rows)

    def test_retrieve_skips_exposure_when_no_env(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []

    def test_retrieve_skips_exposure_for_empty_result(
        self, conn, fixed_clock, monkeypatch,
    ):
        from better_memory.services.reflection import ReflectionSynthesisService

        # No reflections seeded.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        svc = ReflectionSynthesisService(conn, clock=fixed_clock)
        result = svc.retrieve_reflections(project="p")

        rows = conn.execute(
            "SELECT * FROM session_memory_exposure"
        ).fetchall()
        assert rows == []
