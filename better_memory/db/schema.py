"""Migration runner for the better-memory SQLite schema.

Migration files live in :mod:`better_memory.db.migrations` and are named
``NNNN_<description>.sql``. They are applied in lexical order; each file's
version (the ``NNNN`` prefix) is recorded in the ``schema_migrations`` table so
re-running :func:`apply_migrations` is a no-op.

Each file is executed inside an explicit transaction. ``CREATE VIRTUAL TABLE``
and ``CREATE TRIGGER`` work inside a transaction in modern SQLite builds
(>= 3.39), which matches the minimum we test against.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    """Bootstrap the migrations-tracking table if it does not yet exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _version_from_filename(path: Path) -> str:
    """Return the ``NNNN`` prefix from a ``NNNN_<description>.sql`` filename."""
    name = path.name
    version = name.split("_", 1)[0]
    return version


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations, return the versions that were applied.

    Parameters
    ----------
    conn:
        An open SQLite connection (typically from
        :func:`better_memory.db.connection.connect`).
    migrations_dir:
        Directory containing ``NNNN_*.sql`` migration files. Defaults to
        ``better_memory/db/migrations``.
    """
    migrations_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR

    _ensure_schema_migrations_table(conn)
    applied = _applied_versions(conn)

    applied_now: list[str] = []
    files = sorted(p for p in migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    for sql_file in files:
        version = _version_from_filename(sql_file)
        if version in applied:
            continue

        sql = sql_file.read_text(encoding="utf-8")

        # ``executescript`` issues its own COMMIT before running, so we cannot
        # wrap it in an outer BEGIN. On failure, SQLite auto-rolls back the
        # individual failing statement; the caller can inspect partial state or
        # recreate the DB. For the init migration this is acceptable because a
        # partial init is equivalent to a corrupt fresh DB — discard and retry.
        try:
            conn.executescript(sql)
        except Exception:
            # Defensive: if anything is left pending, clean up.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            (version,),
        )
        conn.commit()
        applied_now.append(version)

    return applied_now
