"""Migration 0008: semantic_memories table."""

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


def test_semantic_memories_table_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='semantic_memories'"
    ).fetchall()
    assert len(rows) == 1


def test_semantic_memories_has_expected_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memories)").fetchall()}
    assert cols == {"id", "content", "project", "scope", "created_at", "updated_at",
                    "useful_count", "last_useful_at", "times_misled", "last_misled_at"}


def test_scope_default_is_project(conn) -> None:
    conn.execute(
        "INSERT INTO semantic_memories (id, content, project, created_at, updated_at) "
        "VALUES ('m1','rule','p1','2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM semantic_memories WHERE id='m1'"
    ).fetchone()
    assert row[0] == "project"


def test_scope_check_constraint_rejects_invalid(conn) -> None:
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO semantic_memories (id, content, project, scope, "
            "created_at, updated_at) VALUES "
            "('m1','rule','p1','invalid',"
            "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
        )


def test_scope_accepts_general(conn) -> None:
    conn.execute(
        "INSERT INTO semantic_memories (id, content, project, scope, "
        "created_at, updated_at) VALUES "
        "('m1','rule','p1','general',"
        "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT scope FROM semantic_memories WHERE id='m1'"
    ).fetchone()
    assert row[0] == "general"


def test_project_index_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='semantic_memories'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_semantic_memories_project" in names


def test_general_partial_index_exists(conn) -> None:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_semantic_memories_general'"
    ).fetchall()
    assert len(rows) == 1
    assert "scope = 'general'" in rows[0][1] or "scope='general'" in rows[0][1]
