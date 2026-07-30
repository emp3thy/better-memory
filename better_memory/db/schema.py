"""Migration runner for the better-memory SQLite schema.

Migration files live in :mod:`better_memory.db.migrations` and are named
``NNNN_<description>.sql``. They are applied in lexical order; each file's
version (the ``NNNN`` prefix) is recorded in the ``schema_migrations`` table so
re-running :func:`apply_migrations` is a no-op.

Concurrent starts (two MCP servers pointing at the same DB) contend on the
version row via ``INSERT OR IGNORE`` before any DDL runs, so only one process
executes each migration. Losers of that race trust the winner and skip to the
next file.

Each file is executed via :meth:`sqlite3.Connection.executescript`, which
issues an implicit ``COMMIT`` before running the script; migrations are
therefore **not** atomic. On failure the database may be left in a partial
state — for first-time installs the recovery is to discard the DB file and
re-run. The claim row is deleted on DDL failure so the next start retries
cleanly rather than skipping a half-applied version. Multi-file migrations
that require atomicity must use a different execution path.
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

        # Atomic pre-claim: SQLite serialises writers on the same DB, so if a
        # second process is racing us on this version, one INSERT wins
        # (rowcount == 1) and the other is a no-op via OR IGNORE
        # (rowcount == 0). Only the winner runs the DDL; without this,
        # both would enter ``executescript`` and the loser would die on
        # e.g. ``CREATE VIRTUAL TABLE ... already exists`` at server start.
        claim = conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
            (version,),
        )
        conn.commit()
        if claim.rowcount == 0:
            # Another process already recorded this version. Trust it.
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
            # Release the claim so the next start retries this version rather
            # than skipping past a half-applied migration.
            try:
                conn.execute(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    (version,),
                )
                conn.commit()
            except sqlite3.Error:
                pass
            # Defensive: if anything is left pending, clean up.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

        applied_now.append(version)

    return applied_now
