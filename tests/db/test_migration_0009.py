"""Migration 0009 — memory_rating schema."""
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


class TestExposureTable:
    def test_table_created_with_expected_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall()
        }
        assert cols == {
            "session_id", "memory_kind", "memory_id",
            "exposed_at", "source", "rated_at", "classification",
        }

    def test_primary_key_includes_exposed_at(self, conn):
        pk_cols = [
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall() if r["pk"] > 0
        ]
        assert pk_cols == ["session_id", "memory_kind", "memory_id", "exposed_at"]

    def test_unrated_index_exists(self, conn):
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_sme_session_unrated'"
        ).fetchone()
        assert idx is not None


class TestReflectionsNewColumns:
    def test_useful_count_column_exists(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(reflections)"
            ).fetchall()
        }
        assert {"useful_count", "last_useful_at",
                "times_misled", "last_misled_at"} <= cols

    def test_useful_count_defaults_to_zero(self, conn):
        # insert a reflection and verify defaults
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        row = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0
        assert row["times_misled"] == 0


class TestSemanticMemoriesNewColumns:
    def test_useful_count_column_exists(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(semantic_memories)"
            ).fetchall()
        }
        assert {"useful_count", "last_useful_at",
                "times_misled", "last_misled_at"} <= cols


class TestRatingDiagnosticsTable:
    def test_table_created(self, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='rating_diagnostics'"
        ).fetchone()
        assert row is not None

    def test_session_id_missing_counter_initialised_to_zero(self, conn):
        row = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()
        assert row["value"] == 0
