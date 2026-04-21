"""Episode lifecycle service.

Owns all writes to the ``episodes`` and ``episode_sessions`` tables.
Observations resolve their ``episode_id`` through this service at write
time; MCP tools ``memory.start_episode`` / ``memory.close_episode`` /
``memory.reconcile_episodes`` / ``memory.list_episodes`` wrap the same
API.

Reflection synthesis is NOT triggered here — that lives in Phase 5's
reflection service and is invoked from the MCP tool wrapper, not this
class. Phase 2 keeps the service pure-state.

Spec: §3 (lifecycle) + §4 (schema) of the episodic-memory design doc.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Episode:
    """Read model for an ``episodes`` row."""

    id: str
    project: str
    tech: str | None
    goal: str | None
    started_at: str
    hardened_at: str | None
    ended_at: str | None
    close_reason: str | None
    outcome: str | None
    summary: str | None


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        project=row["project"],
        tech=row["tech"],
        goal=row["goal"],
        started_at=row["started_at"],
        hardened_at=row["hardened_at"],
        ended_at=row["ended_at"],
        close_reason=row["close_reason"],
        outcome=row["outcome"],
        summary=row["summary"],
    )


class EpisodeService:
    """Manages episode open/harden/close transitions.

    Connection ownership: this service writes within its own transaction
    (SAVEPOINT + commit). Callers must not share a connection that has an
    open outer transaction with other services.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    def open_background(self, *, session_id: str, project: str) -> str:
        """Create a background episode (goal=NULL) for ``session_id``.

        Returns the new episode id. Also inserts the matching
        ``episode_sessions`` row with ``left_at = NULL``.
        """
        episode_id = uuid4().hex
        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT episode_open_background")
        try:
            conn.execute(
                "INSERT INTO episodes (id, project, started_at) "
                "VALUES (?, ?, ?)",
                (episode_id, project, now),
            )
            conn.execute(
                "INSERT INTO episode_sessions "
                "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
                (episode_id, session_id, now),
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_open_background")
            conn.execute("RELEASE SAVEPOINT episode_open_background")
            raise
        conn.execute("RELEASE SAVEPOINT episode_open_background")
        conn.commit()
        return episode_id

    def active_episode(self, session_id: str) -> Episode | None:
        """Return the open episode bound to ``session_id``, or None.

        "Open" means ``episodes.ended_at IS NULL`` AND there is a matching
        ``episode_sessions`` row with ``left_at IS NULL``. One-active-per-
        session is an invariant the lifecycle methods maintain.
        """
        row = self._conn.execute(
            """
            SELECT e.*
            FROM episodes e
            JOIN episode_sessions s ON s.episode_id = e.id
            WHERE s.session_id = ?
              AND s.left_at IS NULL
              AND e.ended_at IS NULL
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return _row_to_episode(row) if row is not None else None
