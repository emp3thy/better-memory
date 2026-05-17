"""Migration 0010 — overlooked rating class."""
from __future__ import annotations

import sqlite3
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


class TestExposureClassificationCheck:
    def test_overlooked_classification_accepted(self, conn):
        conn.execute(
            """INSERT INTO session_memory_exposure
               (session_id, memory_kind, memory_id, exposed_at, source,
                rated_at, classification)
               VALUES ('S1', 'reflection', 'r1', '2026-05-17T10:00:00+00:00',
                       'bootstrap', '2026-05-17T11:00:00+00:00', 'overlooked')"""
        )
        row = conn.execute(
            "SELECT classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["classification"] == "overlooked"

    def test_unknown_classification_still_rejected(self, conn):
        # Guard: the CHECK is widened, not dropped.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO session_memory_exposure
                   (session_id, memory_kind, memory_id, exposed_at, source,
                    classification)
                   VALUES ('S1', 'reflection', 'r1',
                           '2026-05-17T10:00:00+00:00', 'bootstrap', 'bogus')"""
            )

    def test_exposure_table_columns_preserved(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall()
        }
        assert cols == {
            "session_id", "memory_kind", "memory_id",
            "exposed_at", "source", "rated_at", "classification",
        }

    def test_primary_key_preserved(self, conn):
        pk_cols = [
            r["name"] for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall() if r["pk"] > 0
        ]
        assert pk_cols == [
            "session_id", "memory_kind", "memory_id", "exposed_at",
        ]

    def test_indexes_recreated(self, conn):
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='session_memory_exposure'"
            ).fetchall()
        }
        assert "idx_sme_session_unrated" in names
        assert "idx_sme_memory" in names


class TestOverlookedCounterColumns:
    def test_reflections_have_overlooked_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(reflections)"
            ).fetchall()
        }
        assert {"times_overlooked", "last_overlooked_at"} <= cols

    def test_semantic_memories_have_overlooked_columns(self, conn):
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(semantic_memories)"
            ).fetchall()
        }
        assert {"times_overlooked", "last_overlooked_at"} <= cols

    def test_times_overlooked_defaults_to_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        row = conn.execute(
            "SELECT times_overlooked, last_overlooked_at "
            "FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["times_overlooked"] == 0
        assert row["last_overlooked_at"] is None
