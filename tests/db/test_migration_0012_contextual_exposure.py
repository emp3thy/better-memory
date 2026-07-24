"""Migration 0012: widen exposure source CHECK to include 'contextual';
seed contextual diagnostics counters.

NOTE: the design doc / plan drafted this as migration "0011", but
0011_trigram_fts.sql (PR #66) landed on main first and claimed that
version. This migration is 0012 instead; only the version number
changed, the SQL content matches the plan's 0011 spec verbatim.

Data-preservation round-trip per project reflection 96936ffc.
"""
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


class TestExposureSourceCheck:
    def test_contextual_source_accepted(self, conn):
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'reflection', 'r1', '2026-07-11T00:00:00+00:00', 'contextual')"
        )
        row = conn.execute(
            "SELECT source FROM session_memory_exposure WHERE session_id='s1'"
        ).fetchone()
        assert row["source"] == "contextual"

    def test_unknown_source_still_rejected(self, conn):
        # Guard: the CHECK is widened, not dropped.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_memory_exposure "
                "(session_id, memory_kind, memory_id, exposed_at, source) "
                "VALUES ('s1', 'reflection', 'r1', '2026-07-11T00:00:00+00:00', 'bogus')"
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
            "via_exploration",
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
        assert {"idx_sme_session_unrated", "idx_sme_memory"} <= names


class TestContextualDiagnostics:
    def test_seeds_contextual_diagnostics_counters(self, conn):
        metrics = {
            r["metric"]
            for r in conn.execute("SELECT metric FROM rating_diagnostics").fetchall()
        }
        assert {
            "contextual_fired_userprompt",
            "contextual_fired_pretool",
            "contextual_injected",
            "contextual_suppressed_floor",
            "contextual_suppressed_dedup",
        } <= metrics

    def test_contextual_counters_default_to_zero(self, conn):
        rows = {
            r["metric"]: r["value"]
            for r in conn.execute("SELECT metric, value FROM rating_diagnostics").fetchall()
        }
        for metric in (
            "contextual_fired_userprompt",
            "contextual_fired_pretool",
            "contextual_injected",
            "contextual_suppressed_floor",
            "contextual_suppressed_dedup",
        ):
            assert rows[metric] == 0


class TestExposureRowSurvivesMigration0012:
    """Prove that rows present before 0012 survive the DROP+RENAME recreation."""

    def test_existing_rows_preserved_after_table_recreation(self, tmp_memory_db: Path):
        with tempfile.TemporaryDirectory() as td:
            pre_dir = Path(td) / "pre"
            pre_dir.mkdir()
            for sql_file in sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                prefix = int(sql_file.name.split("_", 1)[0])
                if prefix <= 11:
                    shutil.copy(sql_file, pre_dir / sql_file.name)

            c = connect(tmp_memory_db)
            try:
                apply_migrations(c, migrations_dir=pre_dir)

                # Insert a row with all optional columns populated, valid pre-0012.
                c.execute(
                    "INSERT INTO session_memory_exposure "
                    "(session_id, memory_kind, memory_id, exposed_at, source, "
                    "rated_at, classification) "
                    "VALUES ('s0', 'semantic', 'm0', '2026-07-10T09:00:00+00:00', "
                    "'bootstrap', '2026-07-10T10:00:00+00:00', 'cited')"
                )
                c.commit()

                m0012 = _MIGRATIONS_DIR / "0012_contextual_exposure.sql"
                only_dir = Path(td) / "only12"
                only_dir.mkdir()
                shutil.copy(m0012, only_dir / m0012.name)
                apply_migrations(c, migrations_dir=only_dir)

                row = c.execute(
                    "SELECT session_id, memory_kind, memory_id, exposed_at, "
                    "source, rated_at, classification "
                    "FROM session_memory_exposure WHERE session_id='s0'"
                ).fetchone()
                assert row is not None, "Row was lost during migration 0012 table recreation"
                assert row["session_id"] == "s0"
                assert row["memory_kind"] == "semantic"
                assert row["memory_id"] == "m0"
                assert row["exposed_at"] == "2026-07-10T09:00:00+00:00"
                assert row["source"] == "bootstrap"
                assert row["rated_at"] == "2026-07-10T10:00:00+00:00"
                assert row["classification"] == "cited"
            finally:
                c.close()
