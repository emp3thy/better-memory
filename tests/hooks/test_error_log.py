"""Unit tests for hooks._error_log.record_hook_error."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Point BETTER_MEMORY_HOME at tmp_path and migrate the DB."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    db = tmp_path / "memory.db"
    c = connect(db)
    try:
        apply_migrations(c)
    finally:
        c.close()
    return db


def test_record_hook_error_inserts_row(db_path: Path) -> None:
    """The helper writes one row per call with all fields populated."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(
        hook_name="observer", exc=RuntimeError("simulated failure")
    )

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT hook_name, exception_type, exception_message "
            "FROM hook_errors"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["hook_name"] == "observer"
    assert rows[0]["exception_type"] == "RuntimeError"
    assert rows[0]["exception_message"] == "simulated failure"


def test_record_hook_error_swallows_db_failure(monkeypatch) -> None:
    """If the DB write itself fails, the helper returns silently
    (hooks must never raise)."""
    from better_memory.hooks import _error_log
    from better_memory.hooks._error_log import record_hook_error

    def _raising_connect(*args, **kwargs):
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(_error_log, "connect", _raising_connect)

    # Must NOT raise.
    record_hook_error(
        hook_name="observer", exc=RuntimeError("original error")
    )


def test_record_hook_error_records_traceback(db_path: Path) -> None:
    """The traceback string from sys.exc_info is captured."""
    from better_memory.hooks._error_log import record_hook_error

    try:
        raise ValueError("inner exception with traceback")
    except ValueError as exc:
        record_hook_error(hook_name="observer", exc=exc)

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT traceback FROM hook_errors"
        ).fetchone()
    finally:
        conn.close()
    assert row["traceback"] is not None
    assert "ValueError" in row["traceback"]


def test_record_hook_error_records_cwd(db_path: Path) -> None:
    """os.getcwd() is captured for diagnosability."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(hook_name="observer", exc=RuntimeError("x"))

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT cwd FROM hook_errors").fetchone()
    finally:
        conn.close()
    assert row["cwd"] is not None
    assert len(row["cwd"]) > 0


def test_record_hook_error_uses_uuid_id(db_path: Path) -> None:
    """Each row gets a unique UUID id."""
    from better_memory.hooks._error_log import record_hook_error

    record_hook_error(hook_name="observer", exc=RuntimeError("a"))
    record_hook_error(hook_name="observer", exc=RuntimeError("b"))

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM hook_errors").fetchall()
    finally:
        conn.close()
    ids = {row["id"] for row in rows}
    assert len(ids) == 2  # both unique
    for id_ in ids:
        assert len(id_) == 32  # uuid4().hex is 32 chars


def test_record_hook_error_falls_back_to_empty_cwd_when_getcwd_raises(
    db_path: Path, monkeypatch
) -> None:
    """Regression for code-review M3 on Group C: if os.getcwd() raises
    (deleted cwd, sandboxed subprocess permission error), the helper
    must STILL write the row — with cwd='' as the fallback. Otherwise
    the diagnostic that's supposed to expose hook failures silently
    fails on exactly the kind of weird production state where we most
    want it to work."""
    from better_memory.hooks import _error_log
    from better_memory.hooks._error_log import record_hook_error

    def _failing_getcwd():
        raise OSError("simulated deleted cwd")

    monkeypatch.setattr(_error_log.os, "getcwd", _failing_getcwd)

    record_hook_error(hook_name="observer", exc=RuntimeError("x"))

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT hook_name, cwd FROM hook_errors"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "row should still be written even when os.getcwd() raises"
    )
    assert row["hook_name"] == "observer"
    assert row["cwd"] == ""
