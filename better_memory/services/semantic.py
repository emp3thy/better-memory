"""User-stated facts/preferences. Free-form content; project + general scope.

Distinct from observations (episodic, recorded as work happens, fed to
synthesis) and reflections (LLM-distilled lessons). Semantic memories are
user assertions of current truth — surfaced at every session start.

See docs/superpowers/specs/2026-05-04-semantic-memories-design.md.
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
class SemanticMemory:
    """Read model returned by retrieve."""

    id: str
    content: str
    project: str
    scope: str            # 'project' | 'general'
    created_at: str
    updated_at: str


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

    def delete(self, *, id: str) -> None:
        """Idempotent — no error if id absent."""
        self._conn.execute(
            "DELETE FROM semantic_memories WHERE id = ?", (id,),
        )
        self._conn.commit()

    def list_for_project(self, *, project: str) -> list[SemanticMemory]:
        """Project rows + general-scope rows from any project, newest first."""
        rows = self._conn.execute(
            """
            SELECT id, content, project, scope, created_at, updated_at
              FROM semantic_memories
             WHERE project = ? OR scope = 'general'
             ORDER BY created_at DESC
            """,
            (project,),
        ).fetchall()
        return [
            SemanticMemory(
                id=r["id"], content=r["content"], project=r["project"],
                scope=r["scope"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]
