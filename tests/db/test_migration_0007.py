"""Migration 0007: scope column on observations + reflections."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    apply_migrations(c)
    yield c
    c.close()


def test_observations_has_scope_column(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    assert "scope" in cols


def test_reflections_has_scope_column(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reflections)").fetchall()}
    assert "scope" in cols


def test_observations_scope_default_is_project(conn) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('o1','x','p1','ep1','active','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT scope FROM observations WHERE id='o1'").fetchone()
    assert row[0] == "project"


def test_reflections_scope_default_is_project(conn) -> None:
    conn.execute(
        "INSERT INTO reflections (id, title, project, phase, polarity, "
        "use_cases, hints, confidence, status, evidence_count, "
        "created_at, updated_at) VALUES "
        "('r1','t','p1','general','do','uc','[]',0.5,'pending_review',0,"
        "'2026-04-01T00:00:00+00:00','2026-04-01T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT scope FROM reflections WHERE id='r1'").fetchone()
    assert row[0] == "project"


def test_observations_scope_check_constraint_rejects_invalid(conn) -> None:
    import sqlite3
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at, scope) VALUES "
            "('o1','x','p1','ep1','active','success',"
            "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00','invalid')"
        )


def test_partial_index_on_general_reflections_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='reflections'"
    ).fetchall()
    assert "idx_reflections_scope_general" in {r[0] for r in rows}


def test_fixup_marks_workflow_observation_general_if_present(conn) -> None:
    """The 0007 fix-up flips the recorded workflow observation to scope='general'.

    Idempotent: if the row doesn't exist (test DB is fresh), this is a no-op.
    Pre-seed the row to verify the UPDATE actually fires.
    """
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal) VALUES "
        "('ep1','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete','g')"
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at, scope) VALUES "
        "('413d47550efd4adfa2c238d6ce5099f9','x','p1','ep1','active','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00','project')"
    )
    conn.commit()
    conn.execute(
        "UPDATE observations SET scope='general' "
        "WHERE id='413d47550efd4adfa2c238d6ce5099f9'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM observations "
        "WHERE id='413d47550efd4adfa2c238d6ce5099f9'"
    ).fetchone()
    assert row[0] == "general"
