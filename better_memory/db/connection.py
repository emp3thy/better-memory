"""SQLite connection helpers for better-memory.

Opens connections with:
    * WAL journaling (``PRAGMA journal_mode=WAL``)
    * Foreign-key enforcement (``PRAGMA foreign_keys=ON``)
    * The sqlite-vec extension loaded
    * ``sqlite3.Row`` as the row factory for dict-like access
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec


def connect(db_path: Path) -> sqlite3.Connection:
    """Open and configure a SQLite connection at ``db_path``.

    The parent directory is created if missing. The caller is responsible for
    closing the returned connection.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # We keep Python's default isolation behaviour so service code can call
    # ``conn.commit()`` normally.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row

        # Load sqlite-vec before any user queries so ``vec_*`` functions and the
        # ``vec0`` virtual-table module are available.
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)

        # WAL must be set via ``PRAGMA``; it persists across connections.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.close()
        raise

    return conn


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-managed variant of :func:`connect`.

    Yields an open connection and closes it on exit, even if the caller raises.
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
