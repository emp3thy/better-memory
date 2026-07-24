"""Migration 0010 — overlooked rating class."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "better_memory" / "db" / "migrations"


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
            "via_exploration", "evidence",
        }

    def test_primary_key_preserved(self, conn):
        pk_rows = [
            r for r in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            ).fetchall() if r["pk"] > 0
        ]
        pk_cols = [r["name"] for r in sorted(pk_rows, key=lambda r: r["pk"])]
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


class TestExposureRowSurvivesMigration0010:
    """Prove that rows present before 0010 survive the DROP+RENAME recreation."""

    def test_existing_rows_preserved_after_table_recreation(self, tmp_memory_db: Path):
        # Step 1: Apply only migrations 0001–0009 by pointing apply_migrations at a
        # temporary directory containing only those files.
        with tempfile.TemporaryDirectory() as td:
            pre10_dir = Path(td)
            for sql_file in sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                prefix = int(sql_file.name.split("_", 1)[0])
                if prefix <= 9:
                    shutil.copy(sql_file, pre10_dir / sql_file.name)

            c = connect(tmp_memory_db)
            try:
                apply_migrations(c, migrations_dir=pre10_dir)

                # Step 2: Insert a test row with classification='misled' (valid pre-0010).
                c.execute(
                    """INSERT INTO session_memory_exposure
                       (session_id, memory_kind, memory_id, exposed_at, source,
                        rated_at, classification)
                       VALUES ('SURV', 'reflection', 'r-surv',
                               '2026-05-17T09:00:00+00:00', 'bootstrap',
                               '2026-05-17T09:30:00+00:00', 'misled')"""
                )
                c.commit()

                # Step 3: Apply migration 0010 only, via a dir containing just that file.
                m0010 = _MIGRATIONS_DIR / "0010_overlooked_rating.sql"
                only10_dir = Path(td) / "only10"
                only10_dir.mkdir()
                shutil.copy(m0010, only10_dir / m0010.name)
                apply_migrations(c, migrations_dir=only10_dir)

                # Step 4: Assert the row is still there with all values intact.
                row = c.execute(
                    "SELECT session_id, memory_kind, memory_id, exposed_at, "
                    "source, rated_at, classification "
                    "FROM session_memory_exposure WHERE session_id='SURV'"
                ).fetchone()
                assert row is not None, "Row was lost during migration 0010 table recreation"
                assert row["session_id"] == "SURV"
                assert row["memory_kind"] == "reflection"
                assert row["memory_id"] == "r-surv"
                assert row["exposed_at"] == "2026-05-17T09:00:00+00:00"
                assert row["source"] == "bootstrap"
                assert row["rated_at"] == "2026-05-17T09:30:00+00:00"
                assert row["classification"] == "misled"
            finally:
                c.close()
