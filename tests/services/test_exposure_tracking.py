"""Tests for exposure tracking on bootstrap + mid-session retrieve paths."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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
            confidence, created_at, updated_at)
           VALUES (?, 't', ?, 'general', 'do', 'uc', '[]', 0.5,
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
        self, conn, fixed_clock, monkeypatch,
    ):
        """When bootstrap injects reflections + semantic memories, an
        exposure row is recorded for each."""
        from better_memory.services.session_bootstrap import SessionBootstrapService

        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_semantic(conn, "s1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

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
