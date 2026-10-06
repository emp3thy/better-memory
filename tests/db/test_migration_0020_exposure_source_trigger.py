"""Migration 0020: exposure source CHECK widened to include 'trigger',
table rebuilt with every later column and both indexes preserved."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

_MIGRATIONS = Path(__file__).parent.parent.parent / "better_memory" / "db" / "migrations"


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def test_trigger_source_accepted(conn):
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES ('s1', 'semantic', 'm1', '2026-10-05T00:00:00+00:00', 'trigger')"
    )
    row = conn.execute(
        "SELECT source FROM session_memory_exposure WHERE session_id='s1'"
    ).fetchone()
    assert row["source"] == "trigger"


def test_unknown_source_still_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'reflection', 'r1', '2026-10-05T00:00:00+00:00', 'bogus')"
        )


def test_columns_pk_and_indexes_preserved(conn):
    info = conn.execute("PRAGMA table_info(session_memory_exposure)").fetchall()
    assert {r["name"] for r in info} == {
        "session_id", "memory_kind", "memory_id", "exposed_at", "source", "rated_at",
        "classification", "via_exploration", "evidence", "display",
    }
    pk = [r["name"] for r in sorted((r for r in info if r["pk"] > 0), key=lambda r: r["pk"])]
    assert pk == ["session_id", "memory_kind", "memory_id", "exposed_at"]
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_memory_exposure'"
        ).fetchall()
    }
    assert {"idx_sme_session_unrated", "idx_sme_memory"} <= names


def test_existing_rows_survive_rebuild(tmp_memory_db: Path):
    """Apply up to 0019, insert a fully populated row, then apply 0020."""
    c = connect(tmp_memory_db)
    try:
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            partial = Path(td)
            for f in sorted(_MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                if f.name < "0020":
                    shutil.copy(f, partial / f.name)
            apply_migrations(c, migrations_dir=partial)
        c.execute(
            "INSERT INTO session_memory_exposure (session_id, memory_kind, memory_id, "
            "exposed_at, source, rated_at, classification, via_exploration, evidence, display) "
            "VALUES ('s', 'reflection', 'r', '2026-01-01', 'contextual', '2026-01-02', "
            "'shaped', 1, 'used it', 'title')"
        )
        c.commit()
        apply_migrations(c)
        row = c.execute("SELECT * FROM session_memory_exposure").fetchone()
        assert (row["source"], row["classification"], row["via_exploration"],
                row["evidence"], row["display"]) == ("contextual", "shaped", 1, "used it", "title")
    finally:
        c.close()
