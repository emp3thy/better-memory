"""Tests for :mod:`better_memory.db.schema`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def _virtual_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()
    return {r["name"] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_migrations_creates_core_tables(tmp_memory_db: Path) -> None:
    """All baseline (non-virtual) tables are present after migration."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        tables = _table_names(conn)
        expected = {
            "observations",
            "audit_log",
            "hook_events",
            "schema_migrations",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"
    finally:
        conn.close()


def test_apply_migrations_creates_virtual_tables(tmp_memory_db: Path) -> None:
    """FTS5 and sqlite-vec virtual tables exist after migration."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        virtual = _virtual_table_names(conn)
        expected = {
            "observation_fts",
            "observation_embeddings",
        }
        assert expected.issubset(virtual), f"Missing virtual tables: {expected - virtual}"
    finally:
        conn.close()


def test_observations_has_episodic_columns(tmp_memory_db: Path) -> None:
    """The ``observations`` table includes the episodic extension columns."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "observations")
        for col in ("outcome", "reinforcement_score", "scope_path"):
            assert col in cols, f"missing observations column: {col}"
    finally:
        conn.close()


def test_observations_outcome_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting an observation with a bogus outcome raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome) "
                "VALUES (?, ?, ?, ?)",
                ("obs-bad", "bogus outcome test", "proj-a", "bogus"),
            )
    finally:
        conn.close()


def test_observations_outcome_accepts_valid_values(tmp_memory_db: Path) -> None:
    """``success``, ``failure``, ``neutral`` are accepted for ``outcome``."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        for i, outcome in enumerate(("success", "failure", "neutral")):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome) "
                "VALUES (?, ?, ?, ?)",
                (f"obs-{i}", f"content {i}", "proj-a", outcome),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
        assert n == 3
    finally:
        conn.close()


def test_fts_triggers_index_observations(tmp_memory_db: Path) -> None:
    """Inserting into observations populates observation_fts via triggers."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO observations (id, content, project, component, theme) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "obs-1",
                "flamingo migration failed under cold weather",
                "proj-a",
                "migrations",
                "zoology",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("flamingo",),
        ).fetchone()
        assert row is not None, "FTS did not index inserted observation"
    finally:
        conn.close()


def test_fts_update_trigger_on_observations(tmp_memory_db: Path) -> None:
    """Updating observations.content re-indexes the FTS row."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO observations (id, content, project) VALUES (?, ?, ?)",
            ("obs-u", "flamingo marker", "proj-a"),
        )
        conn.commit()
        conn.execute(
            "UPDATE observations SET content = ? WHERE id = ?",
            ("pelican marker", "obs-u"),
        )
        conn.commit()
        flamingo = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("flamingo",),
        ).fetchall()
        pelican = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("pelican",),
        ).fetchall()
        assert len(flamingo) == 0, "UPDATE trigger left stale FTS row"
        assert len(pelican) == 1, "UPDATE trigger did not index new content"
    finally:
        conn.close()


def test_fts_delete_trigger_on_observations(tmp_memory_db: Path) -> None:
    """Deleting an observation removes the FTS row."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO observations (id, content, project) VALUES (?, ?, ?)",
            ("obs-d", "heron marker", "proj-a"),
        )
        conn.commit()
        conn.execute("DELETE FROM observations WHERE id = ?", ("obs-d",))
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM observation_fts WHERE observation_fts MATCH ?",
            ("heron",),
        ).fetchall()
        assert len(rows) == 0, "DELETE trigger left FTS row behind"
    finally:
        conn.close()


def test_apply_migrations_is_idempotent(tmp_memory_db: Path) -> None:
    """Running :func:`apply_migrations` twice applies each file exactly once."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [r["version"] for r in rows]
        assert versions == ["0001", "0002"]
    finally:
        conn.close()


def test_episodic_indexes_exist(tmp_memory_db: Path) -> None:
    """The two episodic indexes are created."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_observations_project_component_outcome" in names
        assert "idx_observations_scope_outcome" in names
    finally:
        conn.close()


def test_insight_tables_dropped(tmp_memory_db: Path) -> None:
    """All insight-related tables, virtual tables, and triggers are gone."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE name IN (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "insights",
                "insight_sources",
                "insight_relations",
                "insight_fts",
                "insight_embeddings",
                "insights_ai",
                "insights_ad",
                "insights_au",
            ),
        ).fetchall()
        assert rows == [], f"Leftover insight objects: {[r['name'] for r in rows]}"
    finally:
        conn.close()


def test_episodes_table_exists_with_columns(tmp_memory_db: Path) -> None:
    """episodes has all expected columns."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "episodes")
        expected = {
            "id", "project", "tech", "goal",
            "started_at", "hardened_at", "ended_at",
            "close_reason", "outcome", "summary",
        }
        assert expected.issubset(cols), f"Missing: {expected - cols}"
    finally:
        conn.close()


def test_episodes_close_reason_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting episode with bogus close_reason raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, close_reason) "
                "VALUES (?, ?, ?, ?)",
                ("ep-bad", "proj-a", "2026-04-20T10:00:00Z", "bogus_reason"),
            )
    finally:
        conn.close()


def test_episodes_outcome_check_constraint(tmp_memory_db: Path) -> None:
    """Inserting episode with bogus outcome raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (id, project, started_at, outcome) "
                "VALUES (?, ?, ?, ?)",
                ("ep-bad2", "proj-a", "2026-04-20T10:00:00Z", "bogus_outcome"),
            )
    finally:
        conn.close()


def test_episodes_valid_insert(tmp_memory_db: Path) -> None:
    """A minimal valid episode row inserts cleanly."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-1", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.commit()
        row = conn.execute("SELECT id, project FROM episodes").fetchone()
        assert row["id"] == "ep-1"
        assert row["project"] == "proj-a"
    finally:
        conn.close()
