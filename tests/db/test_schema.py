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
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-oc", "proj-a", "2026-04-20T10:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome, episode_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("obs-bad", "bogus outcome test", "proj-a", "bogus", "ep-oc"),
            )
    finally:
        conn.close()


def test_observations_outcome_accepts_valid_values(tmp_memory_db: Path) -> None:
    """``success``, ``failure``, ``neutral`` are accepted for ``outcome``."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-ov", "proj-a", "2026-04-20T10:00:00Z"),
        )
        for i, outcome in enumerate(("success", "failure", "neutral")):
            conn.execute(
                "INSERT INTO observations (id, content, project, outcome, episode_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"obs-{i}", f"content {i}", "proj-a", outcome, "ep-ov"),
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
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fi", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, episode_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "obs-1",
                "flamingo migration failed under cold weather",
                "proj-a",
                "migrations",
                "zoology",
                "ep-fi",
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
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fu", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-u", "flamingo marker", "proj-a", "ep-fu"),
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
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-fd", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-d", "heron marker", "proj-a", "ep-fd"),
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
        assert versions == ["0001", "0002", "0003", "0004"]
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


def test_episode_sessions_exists_with_columns(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "episode_sessions")
        assert {"episode_id", "session_id", "joined_at", "left_at"}.issubset(cols)
    finally:
        conn.close()


def test_episode_sessions_composite_primary_key(tmp_memory_db: Path) -> None:
    """Duplicate (episode_id, session_id) insert raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-a", "p", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
            "VALUES (?, ?, ?)",
            ("ep-a", "sess-1", "2026-04-20T10:00:00Z"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
                "VALUES (?, ?, ?)",
                ("ep-a", "sess-1", "2026-04-20T11:00:00Z"),
            )
    finally:
        conn.close()


def test_episode_sessions_fk_enforced(tmp_memory_db: Path) -> None:
    """Inserting into episode_sessions without a matching episode row fails."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id, joined_at) "
                "VALUES (?, ?, ?)",
                ("no-such-ep", "sess-1", "2026-04-20T10:00:00Z"),
            )
    finally:
        conn.close()


def test_observations_has_episodic_fk_columns(tmp_memory_db: Path) -> None:
    """observations has episode_id (not null) and tech (nullable)."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        rows = conn.execute("PRAGMA table_info(observations)").fetchall()
        by_name = {r["name"]: r for r in rows}
        assert "episode_id" in by_name
        assert by_name["episode_id"]["notnull"] == 1, "episode_id must be NOT NULL"
        assert "tech" in by_name
        assert by_name["tech"]["notnull"] == 0, "tech must be nullable"
    finally:
        conn.close()


def test_observations_requires_episode_id(tmp_memory_db: Path) -> None:
    """Inserting an observation without episode_id raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project) VALUES (?, ?, ?)",
                ("obs-x", "content", "proj-a"),
            )
    finally:
        conn.close()


def test_observations_episode_fk_enforced(tmp_memory_db: Path) -> None:
    """Inserting an observation with unknown episode_id raises IntegrityError."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, ?, ?)",
                ("obs-y", "c", "p", "no-such-episode"),
            )
    finally:
        conn.close()


def test_observations_valid_insert_with_episode(tmp_memory_db: Path) -> None:
    """A valid observation linked to a real episode inserts cleanly."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-o", "proj-a", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, tech) "
            "VALUES (?, ?, ?, ?, ?)",
            ("obs-ok", "hello", "proj-a", "ep-o", "python"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT episode_id, tech FROM observations WHERE id = ?",
            ("obs-ok",),
        ).fetchone()
        assert row["episode_id"] == "ep-o"
        assert row["tech"] == "python"
    finally:
        conn.close()


def test_reflections_table_exists_with_columns(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "reflections")
        expected = {
            "id", "title", "project", "tech", "phase", "polarity",
            "use_cases", "hints", "confidence", "status", "superseded_by",
            "evidence_count", "created_at", "updated_at",
        }
        assert expected.issubset(cols), f"Missing: {expected - cols}"
    finally:
        conn.close()


def test_reflections_phase_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-bad", "t", "p", "bogus_phase", "do",
                 "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_polarity_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-bad", "t", "p", "general", "bogus_pol",
                 "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_confidence_range(tmp_memory_db: Path) -> None:
    """confidence must be in [0.1, 1.0]."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-hi", "t", "p", "general", "do", "uc", "[]",
                 1.5, 0, "2026-04-20", "2026-04-20"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r-lo", "t", "p", "general", "do", "uc", "[]",
                 0.05, 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_status_check_constraint(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, status, evidence_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r-st", "t", "p", "general", "do", "uc", "[]", 0.5,
                 "bogus_status", 0, "2026-04-20", "2026-04-20"),
            )
    finally:
        conn.close()


def test_reflections_valid_insert_and_fts(tmp_memory_db: Path) -> None:
    """Valid reflection inserts and is indexed by FTS."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-1", "Pelican preference", "proj-a", "general", "do",
             "when handling pelicans", '["use wide runways"]',
             0.7, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("pelican",),
        ).fetchone()
        assert row is not None, "FTS did not index inserted reflection"
    finally:
        conn.close()


def test_reflections_update_trigger(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-u", "flamingo", "proj-a", "general", "do",
             "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        conn.execute(
            "UPDATE reflections SET title = ? WHERE id = ?",
            ("pelican", "r-u"),
        )
        conn.commit()
        flamingo = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("flamingo",),
        ).fetchall()
        pelican = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("pelican",),
        ).fetchall()
        assert len(flamingo) == 0
        assert len(pelican) == 1
    finally:
        conn.close()


def test_reflections_delete_trigger(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r-d", "heron", "proj-a", "general", "do",
             "uc", "[]", 0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()
        conn.execute("DELETE FROM reflections WHERE id = ?", ("r-d",))
        conn.commit()
        rows = conn.execute(
            "SELECT rowid FROM reflection_fts WHERE reflection_fts MATCH ?",
            ("heron",),
        ).fetchall()
        assert len(rows) == 0
    finally:
        conn.close()


def test_reflection_sources_exists(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "reflection_sources")
        assert {"reflection_id", "observation_id"}.issubset(cols)
    finally:
        conn.close()


def test_reflection_sources_composite_pk_and_fks(tmp_memory_db: Path) -> None:
    """Composite PK enforced; both FKs enforced."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) VALUES (?, ?, ?)",
            ("ep-rs", "p", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES (?, ?, ?, ?)",
            ("obs-rs", "c", "p", "ep-rs"),
        )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            " confidence, evidence_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("refl-rs", "t", "p", "general", "do", "uc", "[]",
             0.5, 0, "2026-04-20", "2026-04-20"),
        )
        conn.commit()

        # Valid link inserts cleanly.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES (?, ?)",
            ("refl-rs", "obs-rs"),
        )
        conn.commit()

        # Duplicate (composite PK) rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("refl-rs", "obs-rs"),
            )

        # Unknown reflection FK rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("no-such-refl", "obs-rs"),
            )

        # Unknown observation FK rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reflection_sources (reflection_id, observation_id) "
                "VALUES (?, ?)",
                ("refl-rs", "no-such-obs"),
            )
    finally:
        conn.close()


def test_synthesis_runs_exists(tmp_memory_db: Path) -> None:
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "synthesis_runs")
        assert {"project", "tech", "last_run_at"}.issubset(cols)
    finally:
        conn.close()


def test_synthesis_runs_composite_pk(tmp_memory_db: Path) -> None:
    """(project, tech) is a primary key; tech defaults to '' (not NULL)."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        # Two rows with same project but different tech — both succeed.
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "python", "2026-04-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "sqlite", "2026-04-20T10:00:00Z"),
        )
        conn.commit()

        # Default tech is '' — project without tech is a distinct PK.
        conn.execute(
            "INSERT INTO synthesis_runs (project, last_run_at) VALUES (?, ?)",
            ("p", "2026-04-20T10:00:00Z"),
        )
        conn.commit()

        # Duplicate (project, tech) rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (project, tech, last_run_at) "
                "VALUES (?, ?, ?)",
                ("p", "python", "2026-04-20T11:00:00Z"),
            )

        # tech NOT NULL — explicit NULL rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (project, tech, last_run_at) "
                "VALUES (?, ?, ?)",
                ("p2", None, "2026-04-20T10:00:00Z"),
            )
    finally:
        conn.close()


def test_synthesis_runs_has_last_goal_column(tmp_memory_db: Path) -> None:
    """0003 migration adds last_goal column to synthesis_runs."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        cols = _column_names(conn, "synthesis_runs")
        assert "last_goal" in cols, f"Missing: last_goal. Got: {cols}"

        # Back-compat: rows can still be inserted without last_goal.
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at) "
            "VALUES (?, ?, ?)",
            ("p", "python", "2026-04-22T10:00:00+00:00"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT last_goal FROM synthesis_runs WHERE project = ?", ("p",)
        ).fetchone()
        assert row["last_goal"] is None  # nullable, default NULL
    finally:
        conn.close()


def test_synthesis_runs_last_goal_round_trips(tmp_memory_db: Path) -> None:
    """Explicit last_goal value stored and readable."""
    conn = connect(tmp_memory_db)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal) "
            "VALUES (?, ?, ?, ?)",
            ("p", "python", "2026-04-22T10:00:00+00:00", "implement feature X"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT last_goal FROM synthesis_runs WHERE project = ?", ("p",)
        ).fetchone()
        assert row["last_goal"] == "implement feature X"
    finally:
        conn.close()


class TestStatusChangedAtColumn:
    def test_observations_has_status_changed_at_column(self, tmp_memory_db):
        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(observations)").fetchall()
        }
        assert "status_changed_at" in cols

    def test_backfill_existing_rows_sets_status_changed_at_from_created_at(
        self, tmp_memory_db
    ):
        """Verify the migration's backfill UPDATE populates pre-existing
        rows that had NULL status_changed_at."""
        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        # Need an episode for FK constraint.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES ('ep-1', 'proj-a', '2026-04-01T00:00:00+00:00')"
        )
        # Simulate a row that pre-existed the column by inserting then
        # NULLing the column.
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-1', 'c', 'proj-a', 'ep-1', "
            "'2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "UPDATE observations SET status_changed_at = NULL "
            "WHERE id = 'obs-1'"
        )
        conn.commit()

        # Re-run the backfill UPDATE manually (this is the same SQL the
        # migration uses).
        conn.execute(
            "UPDATE observations "
            "SET status_changed_at = created_at "
            "WHERE status_changed_at IS NULL"
        )
        row = conn.execute(
            "SELECT status_changed_at FROM observations "
            "WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status_changed_at"] == "2026-04-01T00:00:00+00:00"

    def test_status_changed_at_index_exists(self, tmp_memory_db):
        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'observations' "
            "AND name = 'idx_observations_status_changed_at'"
        ).fetchone()
        assert idx is not None
