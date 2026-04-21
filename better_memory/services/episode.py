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
        row = self._active_episode_row(session_id)
        return _row_to_episode(row) if row is not None else None

    def start_foreground(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        """Harden a background episode, or supersede prior foreground.

        Semantics (spec §3):
        - If an active background episode exists for this session
          (goal IS NULL), stamp goal/tech/hardened_at on it.
        - Else if an active foreground episode exists with a different
          goal, close it as ``close_reason='superseded'``,
          ``outcome='no_outcome'`` and open a new foreground.
        - Else open a new foreground from scratch (started_at = now).

        Returns the resulting episode id (the hardened/new one).
        """
        now = self._clock().isoformat()
        tech_normalised = tech.lower() if tech is not None else None

        conn = self._conn
        conn.execute("SAVEPOINT episode_start_foreground")
        try:
            active = self._active_episode_row(session_id)

            if active is not None and active["goal"] is None:
                # Harden the background episode.
                conn.execute(
                    "UPDATE episodes "
                    "SET goal = ?, tech = ?, hardened_at = ? "
                    "WHERE id = ?",
                    (goal, tech_normalised, now, active["id"]),
                )
                result_id = active["id"]
            else:
                # Supersede any prior active foreground (active with a goal),
                # then open a new foreground.
                if active is not None:
                    conn.execute(
                        "UPDATE episodes "
                        "SET ended_at = ?, close_reason = 'superseded', "
                        "    outcome = 'no_outcome' "
                        "WHERE id = ?",
                        (now, active["id"]),
                    )
                    conn.execute(
                        "UPDATE episode_sessions "
                        "SET left_at = ? "
                        "WHERE episode_id = ? AND session_id = ?",
                        (now, active["id"], session_id),
                    )

                result_id = uuid4().hex
                conn.execute(
                    "INSERT INTO episodes "
                    "(id, project, tech, goal, started_at, hardened_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (result_id, project, tech_normalised, goal, now, now),
                )
                conn.execute(
                    "INSERT INTO episode_sessions "
                    "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
                    (result_id, session_id, now),
                )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT episode_start_foreground")
            conn.execute("RELEASE SAVEPOINT episode_start_foreground")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT episode_start_foreground")
        conn.commit()
        return result_id

    def _active_episode_row(self, session_id: str) -> sqlite3.Row | None:
        """Internal helper: returns the raw active episode Row (not Episode)."""
        return self._conn.execute(
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
