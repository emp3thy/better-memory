"""Migration runner for the better-memory SQLite schema.

Migration files live in :mod:`better_memory.db.migrations` and are named
``NNNN_<description>.sql``. They are applied in lexical order; each file's
version (the ``NNNN`` prefix) is recorded in the ``schema_migrations`` table so
re-running :func:`apply_migrations` is a no-op.

Concurrent starts (two MCP servers pointing at the same DB) are serialised
via a two-phase claim on the version row:

* ``applied_at IS NULL`` — a process has claimed the version and is running
  the DDL. Peers seeing this row poll until it either transitions or
  disappears.
* ``applied_at IS NOT NULL`` — the DDL completed. Peers skip.
* row absent — no one holds a claim. A peer may attempt to claim.

The claim ``INSERT OR IGNORE`` is atomic under SQLite's writer serialisation,
so exactly one process wins the race and runs the DDL. On DDL failure the
winner deletes its claim row so peers (or the next start) can retry rather
than skipping past a half-applied migration. Peers time out after a generous
per-version budget and raise, so a stuck peer never lets a loser return with
a broken schema (the pre-fix regression this design closes).

Each file is executed via :meth:`sqlite3.Connection.executescript`, which
issues an implicit ``COMMIT`` before running the script; migrations are
therefore **not** atomic within a single file. On failure the database may
be left in a partial state — for first-time installs the recovery is to
discard the DB file and re-run. Multi-file migrations that require
atomicity must use a different execution path.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# How long a peer will wait for the current claim-holder to finish DDL on
# one migration before giving up and raising. Sized generously against the
# slowest realistic migration; hitting the deadline means the claim-holder
# died without cleaning up, which we surface as an error rather than
# silently skipping.
_CLAIM_WAIT_SECONDS = 120.0
_CLAIM_POLL_INTERVAL = 0.1


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
    """Return versions that are recorded as *completed*.

    A row with ``applied_at IS NULL`` is an in-progress claim, not a
    completed migration; excluding it here means a peer that sees the
    row still enters the polling loop rather than mistaking a live claim
    for a finished apply.
    """
    rows = conn.execute(
        "SELECT version FROM schema_migrations WHERE applied_at IS NOT NULL"
    ).fetchall()
    return {row[0] for row in rows}


def _version_from_filename(path: Path) -> str:
    """Return the ``NNNN`` prefix from a ``NNNN_<description>.sql`` filename."""
    name = path.name
    version = name.split("_", 1)[0]
    return version


def _try_claim(conn: sqlite3.Connection, version: str) -> bool:
    """Attempt to atomically claim ``version`` as in-progress.

    Explicit ``applied_at = NULL`` overrides the column default so peers
    can tell a live claim from a completed apply.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
        "VALUES (?, NULL)",
        (version,),
    )
    conn.commit()
    return cur.rowcount == 1


def _mark_complete(conn: sqlite3.Connection, version: str) -> None:
    conn.execute(
        "UPDATE schema_migrations SET applied_at = CURRENT_TIMESTAMP "
        "WHERE version = ?",
        (version,),
    )
    conn.commit()


def _release_claim(conn: sqlite3.Connection, version: str) -> None:
    """Best-effort delete of the claim row on DDL failure."""
    try:
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?", (version,),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def _wait_for_peer(conn: sqlite3.Connection, version: str) -> str:
    """Poll a peer's claim on ``version``.

    Returns:
      * ``"completed"`` — peer set ``applied_at`` non-NULL; caller skips.
      * ``"released"`` — peer deleted the claim (DDL failed); caller
        should attempt to claim it themselves.

    Raises ``sqlite3.OperationalError`` on timeout. Timing out is
    intentional: it turns "peer died mid-migration" into a loud error
    at the losing process rather than a silent return with a partial
    schema (the regression BugBot flagged on the first cut of #106).
    """
    deadline = time.monotonic() + _CLAIM_WAIT_SECONDS
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return "released"
        if row[0] is not None:
            return "completed"
        time.sleep(_CLAIM_POLL_INTERVAL)
    raise sqlite3.OperationalError(
        f"Timed out waiting for peer to apply migration {version} "
        f"after {_CLAIM_WAIT_SECONDS:.0f}s; a previous starter likely "
        f"died mid-migration. Inspect schema_migrations and the DB "
        f"before retrying."
    )


def _apply_one(
    conn: sqlite3.Connection,
    version: str,
    sql_file: Path,
) -> bool:
    """Apply a single migration, coordinating with any concurrent peer.

    Returns True iff we actually ran the DDL (i.e. we won the claim);
    False if a peer had already completed it. Raises on DDL failure or
    peer-wait timeout.
    """
    while True:
        if _try_claim(conn, version):
            sql = sql_file.read_text(encoding="utf-8")
            try:
                # ``executescript`` issues its own COMMIT before running,
                # so we cannot wrap it in an outer BEGIN. On failure,
                # SQLite auto-rolls back the individual failing statement;
                # a partial init is equivalent to a corrupt fresh DB —
                # discard and retry.
                conn.executescript(sql)
            except Exception:
                _release_claim(conn, version)
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            _mark_complete(conn, version)
            return True

        # Lost the claim; wait for the peer to finish or release.
        outcome = _wait_for_peer(conn, version)
        if outcome == "completed":
            return False
        # Peer released (DDL failed). Loop to try claiming ourselves.


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
        if _apply_one(conn, version, sql_file):
            applied_now.append(version)

    return applied_now
