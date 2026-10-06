"""Migration 0019: nullable ``triggers`` JSON column on both memory tables;
seed the trigger-channel diagnostics counters."""
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


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_triggers_column_on_both_tables(conn):
    assert "triggers" in _columns(conn, "reflections")
    assert "triggers" in _columns(conn, "semantic_memories")


def test_triggers_column_is_nullable_default_null(conn):
    conn.execute(
        "INSERT INTO semantic_memories (id, content, project, scope, created_at, updated_at) "
        "VALUES ('s1', 'c', 'p', 'project', '2026-01-01', '2026-01-01')"
    )
    row = conn.execute("SELECT triggers FROM semantic_memories WHERE id='s1'").fetchone()
    assert row[0] is None


def test_seeds_trigger_diagnostics(conn):
    rows = dict(conn.execute("SELECT metric, value FROM rating_diagnostics").fetchall())
    for metric in ("contextual_suppressed_nonhuman", "trigger_fired", "trigger_injected"):
        assert rows[metric] == 0


def test_idempotent(conn):
    apply_migrations(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM rating_diagnostics WHERE metric='trigger_fired'"
    ).fetchone()[0]
    assert n == 1
