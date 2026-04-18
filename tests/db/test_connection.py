"""Tests for :mod:`better_memory.db.connection`."""

from __future__ import annotations

from pathlib import Path

from better_memory.db.connection import connect, connection


def test_connect_creates_parent_directories(tmp_path: Path) -> None:
    """``connect`` creates any missing parent directories for the DB file."""
    nested = tmp_path / "nested" / "deeper" / "memory.db"
    conn = connect(nested)
    try:
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        conn.close()


def test_connect_enables_wal_mode(tmp_memory_db: Path) -> None:
    """WAL mode is the active journal mode after :func:`connect`."""
    conn = connect(tmp_memory_db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_connect_enables_foreign_keys(tmp_memory_db: Path) -> None:
    """Foreign-key enforcement is enabled after :func:`connect`."""
    conn = connect(tmp_memory_db)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_connect_loads_sqlite_vec(tmp_memory_db: Path) -> None:
    """sqlite-vec extension is loaded (``vec_version()`` responds)."""
    conn = connect(tmp_memory_db)
    try:
        version = conn.execute("SELECT vec_version()").fetchone()[0]
        assert isinstance(version, str)
        assert version  # non-empty
    finally:
        conn.close()


def test_connect_uses_row_factory(tmp_memory_db: Path) -> None:
    """Rows come back as :class:`sqlite3.Row` objects (key access works)."""
    import sqlite3

    conn = connect(tmp_memory_db)
    try:
        row = conn.execute("SELECT 1 AS one").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["one"] == 1
    finally:
        conn.close()


def test_connection_context_manager_closes(tmp_memory_db: Path) -> None:
    """The :func:`connection` context manager opens and closes the DB."""
    import sqlite3

    with connection(tmp_memory_db) as conn:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.execute("SELECT vec_version()").fetchone()[0]

    # Using the connection after the context manager should raise.
    import pytest

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
