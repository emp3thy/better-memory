"""A session's exposures are a set: re-serving a memory must not add rows.

The PK on session_memory_exposure includes exposed_at, so before the
write-time guard every re-retrieval added a row. The rating path already
collapses duplicates (one classification per (kind, id) per session), so the
extra rows existed only to inflate any statistic computed over the raw table
— measured 16.08% vs 9.25% "useful" on the same live data.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.semantic import SemanticMemoryService
from better_memory.services.session_bootstrap import SessionBootstrapService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _rows(conn, session_id="s1"):
    return conn.execute(
        "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
        "WHERE session_id = ? ORDER BY memory_kind, memory_id",
        (session_id,),
    ).fetchall()


def _seed_reflection(conn, rid):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, rid),
    )
    conn.commit()


class TestExposureDedup:
    def test_record_exposures_is_idempotent_per_session(self, conn):
        svc = SessionBootstrapService(conn)
        items = [("reflection", "r-a", None), ("semantic", "s-a", None)]
        svc.record_exposures(session_id="s1", items=items, source="bootstrap")
        svc.record_exposures(session_id="s1", items=items, source="bootstrap")
        assert len(_rows(conn)) == 2

    def test_reserve_keeps_first_source(self, conn):
        # bootstrap exposes first; a later retrieve of the same memory must
        # not relabel or duplicate it.
        svc = SessionBootstrapService(conn)
        svc.record_exposures(
            session_id="s1", items=[("reflection", "r-a", None)], source="bootstrap"
        )
        svc.record_exposures(
            session_id="s1", items=[("reflection", "r-a", None)], source="retrieve"
        )
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["source"] == "bootstrap"

    def test_other_sessions_unaffected(self, conn):
        svc = SessionBootstrapService(conn)
        svc.record_exposures(
            session_id="s1", items=[("reflection", "r-a", None)], source="bootstrap"
        )
        svc.record_exposures(
            session_id="s2", items=[("reflection", "r-a", None)], source="bootstrap"
        )
        assert len(_rows(conn, "s1")) == 1
        assert len(_rows(conn, "s2")) == 1

    def test_repeated_retrieve_reflections_writes_one_row(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed_reflection(conn, "r-a")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p")
        svc.retrieve_reflections(project="p")
        svc.retrieve_reflections(project="p")
        assert len(_rows(conn)) == 1

    def test_repeated_semantic_list_writes_one_row(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        sem = SemanticMemoryService(conn)
        sem.create(content="fact", project="p")
        sem.list_for_project(project="p")
        sem.list_for_project(project="p")
        assert len(_rows(conn)) == 1
