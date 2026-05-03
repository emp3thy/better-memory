"""Migration 0005: retention_runs + hook_errors tables exist with correct shape."""

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


def test_retention_runs_table_exists(conn) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(retention_runs)").fetchall()
    }
    assert cols == {
        "id",
        "run_at",
        "archived_via_retired_reflection",
        "archived_via_consumed_without_reflection",
        "archived_via_no_outcome_episode",
        "pruned",
        "triggered_by",
    }


def test_retention_runs_has_run_at_index(conn) -> None:
    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT * FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'retention_runs'"
        ).fetchall()
    }
    assert "idx_retention_runs_run_at" in indexes


def test_hook_errors_table_exists(conn) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(hook_errors)").fetchall()
    }
    assert cols == {
        "id",
        "created_at",
        "hook_name",
        "exception_type",
        "exception_message",
        "traceback",
        "cwd",
    }


def test_hook_errors_has_created_at_index(conn) -> None:
    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT * FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'hook_errors'"
        ).fetchall()
    }
    assert "idx_hook_errors_created_at" in indexes
