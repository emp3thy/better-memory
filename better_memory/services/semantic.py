"""User-stated facts/preferences. Free-form content; project + general scope.

Distinct from observations (episodic, recorded as work happens, fed to
synthesis) and reflections (LLM-distilled lessons). Semantic memories are
user assertions of current truth — surfaced at every session start.

See docs/superpowers/specs/2026-05-04-semantic-memories-design.md.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4



def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SemanticMemory:
    """Read model returned by retrieve."""

    id: str
    content: str
    project: str
    scope: str            # 'project' | 'general'
    created_at: str
    updated_at: str
    useful_count: int = 0
    last_useful_at: str | None = None
    times_misled: int = 0
    last_misled_at: str | None = None


_VALID_SCOPES = ("project", "general")


class SemanticMemoryService:
    """User-stated facts/preferences.

    Connection ownership: writes within own commit envelope. No SAVEPOINT
    needed — each method is a single-row mutation that's atomic on its own.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    def create(
        self, *, content: str, project: str, scope: str = "project"
    ) -> str:
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        if not content.strip():
            raise ValueError("content must not be empty")
        memory_id = uuid4().hex
        now = self._clock().isoformat()
        self._conn.execute(
            """
            INSERT INTO semantic_memories
                (id, content, project, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, project, scope, now, now),
        )
        self._conn.commit()
        return memory_id

    def update_text(self, *, id: str, content: str) -> None:
        if not content.strip():
            raise ValueError("content must not be empty")
        now = self._clock().isoformat()
        cur = self._conn.execute(
            "UPDATE semantic_memories SET content = ?, updated_at = ? "
            "WHERE id = ?",
            (content, now, id),
        )
        if cur.rowcount == 0:
            # No row updated — roll back the implicit BEGIN that sqlite3
            # opened before the UPDATE so we don't strand the WAL write
            # lock for callers sharing this connection. Mirrors
            # ObservationService.set_outcome (better_memory/services/observation.py:435).
            self._conn.rollback()
            raise ValueError(f"semantic memory not found: {id}")
        self._conn.commit()

    def set_scope(self, *, id: str, scope: str) -> None:
        """Toggle a semantic memory's scope between 'project' and 'general'.

        Bumps updated_at. No-op-style: setting scope to the same value
        still bumps updated_at (the update is real DB-side; we don't
        short-circuit). Raises ValueError on invalid scope or missing id.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        now = self._clock().isoformat()
        cur = self._conn.execute(
            "UPDATE semantic_memories SET scope = ?, updated_at = ? "
            "WHERE id = ?",
            (scope, now, id),
        )
        if cur.rowcount == 0:
            self._conn.rollback()
            raise ValueError(f"semantic memory not found: {id}")
        self._conn.commit()

    def create_from_observation(
        self, *, observation_id: str, scope: str = "project"
    ) -> str:
        """Promote an active observation into a new semantic memory.

        Atomically (within SAVEPOINT promote_observation):
        1. Read the observation; raise if missing or not status='active'.
        2. INSERT a new semantic_memories row with the observation's
           content + project, the requested scope, and current timestamp.
        3. UPDATE the observation status='consumed_without_reflection'
           and bump status_changed_at.

        Raises ValueError on invalid scope, missing observation, or
        already-consumed observation. Returns the new memory id.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        row = self._conn.execute(
            "SELECT content, project, status FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"observation not found: {observation_id}")
        if row["status"] != "active":
            raise ValueError(
                f"observation {observation_id} is not active "
                f"(status={row['status']!r}); cannot promote"
            )
        if not row["content"].strip():
            raise ValueError(
                "observation content is empty; cannot promote"
            )

        memory_id = uuid4().hex
        now = self._clock().isoformat()
        self._conn.execute("SAVEPOINT promote_observation")
        try:
            self._conn.execute(
                """
                INSERT INTO semantic_memories
                    (id, content, project, scope, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, row["content"], row["project"], scope, now, now),
            )
            self._conn.execute(
                "UPDATE observations "
                "SET status = 'consumed_without_reflection', status_changed_at = ? "
                "WHERE id = ?",
                (now, observation_id),
            )
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT promote_observation")
            self._conn.execute("RELEASE SAVEPOINT promote_observation")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT promote_observation")
        self._conn.commit()
        return memory_id

    def delete(self, *, id: str) -> None:
        """Idempotent — no error if id absent."""
        self._conn.execute(
            "DELETE FROM semantic_memories WHERE id = ?", (id,),
        )
        self._conn.commit()

    def list_for_project(
        self,
        *,
        project: str,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[SemanticMemory]:
        """Project rows + general-scope rows from any project, ordered by
        useful_count DESC then created_at DESC (newest first as tiebreaker).

        Args:
            project: project key for project-scope filtering.
            scope_filter: ``None`` (default) returns project-scope rows for
                ``project`` plus all general-scope rows. ``'project'``
                returns only project-scope rows for ``project``.
                ``'general'`` returns only general-scope rows (any project).
            search: optional case-insensitive substring match on
                ``content``. ``%`` and ``_`` in the input are escaped so
                they match literally rather than as LIKE wildcards.
            track_exposure: when ``True`` (default), writes a
                source='retrieve' row per returned memory into
                ``session_memory_exposure`` if ``CLAUDE_SESSION_ID`` is set.
                Set to ``False`` when calling from contexts that manage their
                own exposure tracking (e.g., SessionBootstrapService.bootstrap).
        """
        where_clauses: list[str] = []
        params: list[object] = []

        if scope_filter == "project":
            where_clauses.append("project = ? AND scope = 'project'")
            params.append(project)
        elif scope_filter == "general":
            where_clauses.append("scope = 'general'")
        else:
            where_clauses.append("(project = ? OR scope = 'general')")
            params.append(project)

        if search:
            escaped = (
                search.replace("\\", "\\\\")
                      .replace("%", "\\%")
                      .replace("_", "\\_")
            )
            where_clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        sql = (
            "SELECT id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at "
            "FROM semantic_memories "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY useful_count DESC, created_at DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        results = [
            SemanticMemory(
                id=r["id"], content=r["content"], project=r["project"],
                scope=r["scope"],
                created_at=r["created_at"], updated_at=r["updated_at"],
                useful_count=r["useful_count"] or 0,
                last_useful_at=r["last_useful_at"],
                times_misled=r["times_misled"] or 0,
                last_misled_at=r["last_misled_at"],
            )
            for r in rows
        ]
        # Best-effort exposure tracking — see spec §5.2.1.
        # track_exposure=False is used by SessionBootstrapService.bootstrap,
        # which manages its own exposure write via _record_exposure.
        sid = os.environ.get("CLAUDE_SESSION_ID")
        if track_exposure and sid and results:
            now = self._clock().isoformat()
            self._conn.executemany(
                "INSERT OR IGNORE INTO session_memory_exposure "
                "(session_id, memory_kind, memory_id, exposed_at, source) "
                "VALUES (?, 'semantic', ?, ?, 'retrieve')",
                [(sid, m.id, now) for m in results],
            )
            self._conn.commit()
        return results
