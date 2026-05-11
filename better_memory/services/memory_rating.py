"""Memory rating service: closed-loop self-rating of reflections and
semantic memories.

Two public methods:
- credit_one: single-row per-tool-use credit, called via memory.credit MCP tool.
- apply_session_ratings: atomic batch update at session end (see Task 3).

Connection ownership: this service writes within its own SAVEPOINT + commit
envelope. Callers must not share a connection that already has an open outer
transaction with another service.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal


def _default_clock() -> datetime:
    return datetime.now(UTC)


Kind = Literal["reflection", "semantic"]
Classification = Literal["cited", "shaped", "ignored", "misled"]
CreditClassification = Literal["cited", "shaped", "misled"]
SkipReason = Literal[
    "not_exposed", "already_rated", "memory_missing", "memory_retired"
]


_VALID_KINDS: set[str] = {"reflection", "semantic"}
_VALID_CLASSES: set[str] = {"cited", "shaped", "ignored", "misled"}
_CREDIT_CLASSES: set[str] = {"cited", "shaped", "misled"}


class MemoryRatingService:
    """Writes useful_count / times_misled on reflections + semantic memories,
    and stamps rated_at / classification on session_memory_exposure rows.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    # --------------------------------------------------------------- credit_one
    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> dict[str, object]:
        """Apply one rating for (session_id, kind, id).

        Validation (ValueError before any DB write):
        - kind must be 'reflection' or 'semantic'.
        - classification must be 'cited', 'shaped', or 'misled' (NOT 'ignored').

        Skip outcomes (no exception, no write, returned via dict):
        - 'not_exposed' — no matching exposure row for this session.
        - 'already_rated' — exposure row has rated_at IS NOT NULL.
        - 'memory_missing' — the memory id no longer exists.
        - 'memory_retired' — reflection has status retired/superseded
          (semantic memories have no status — skip rule doesn't apply).

        Apply outcomes:
        - 'cited' / 'shaped' → useful_count++, last_useful_at = now.
        - 'misled'           → times_misled++, last_misled_at = now.
        And in all apply outcomes, the exposure row is stamped:
        rated_at = now, classification = <input>.

        Returns:
            {"applied": <class>, "skipped": None}  on apply
            {"applied": None,    "skipped": <reason>}  on skip
        """
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"Invalid kind: {kind!r}. Expected one of {_VALID_KINDS}"
            )
        if classification == "ignored":
            raise ValueError(
                "credit_one does not accept classification='ignored'; "
                "'ignored' is the session-end sweep default."
            )
        if classification not in _CREDIT_CLASSES:
            raise ValueError(
                f"Invalid classification: {classification!r}. "
                f"Expected one of {_CREDIT_CLASSES}"
            )

        now = self._clock().isoformat()
        self._conn.execute("SAVEPOINT memory_credit")
        try:
            outcome = self._apply_one(
                session_id=session_id, kind=kind, memory_id=id,
                classification=classification, now=now,
            )
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT memory_credit")
            self._conn.execute("RELEASE SAVEPOINT memory_credit")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT memory_credit")
        self._conn.commit()
        return outcome

    # ------------------------------------------------------------- _apply_one
    def _apply_one(
        self,
        *,
        session_id: str,
        kind: str,
        memory_id: str,
        classification: str,
        now: str,
    ) -> dict[str, object]:
        """Inside-savepoint per-row apply. Returns the same dict shape as
        credit_one. Shared by credit_one and apply_session_ratings (Task 3).
        """
        # 1. Find the unrated exposure row.
        row = self._conn.execute(
            "SELECT rated_at FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?",
            (session_id, kind, memory_id),
        ).fetchone()
        if row is None:
            return {"applied": None, "skipped": "not_exposed"}
        if row["rated_at"] is not None:
            return {"applied": None, "skipped": "already_rated"}

        # 2. Check the memory still exists.
        if kind == "reflection":
            mem = self._conn.execute(
                "SELECT status FROM reflections WHERE id = ?", (memory_id,),
            ).fetchone()
            if mem is None:
                return {"applied": None, "skipped": "memory_missing"}
            if mem["status"] in ("retired", "superseded"):
                return {"applied": None, "skipped": "memory_retired"}
            table = "reflections"
        else:  # semantic
            mem = self._conn.execute(
                "SELECT id FROM semantic_memories WHERE id = ?", (memory_id,),
            ).fetchone()
            if mem is None:
                return {"applied": None, "skipped": "memory_missing"}
            table = "semantic_memories"

        # 3. Bump the appropriate counter.
        if classification in ("cited", "shaped"):
            self._conn.execute(
                f"UPDATE {table} "
                f"SET useful_count = useful_count + 1, last_useful_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        elif classification == "misled":
            self._conn.execute(
                f"UPDATE {table} "
                f"SET times_misled = times_misled + 1, last_misled_at = ? "
                f"WHERE id = ?",
                (now, memory_id),
            )
        # 'ignored' is a no-op on the memory row; reached only via
        # apply_session_ratings, not credit_one.

        # 4. Stamp the exposure row.
        self._conn.execute(
            "UPDATE session_memory_exposure "
            "SET rated_at = ?, classification = ? "
            "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?"
            "  AND rated_at IS NULL",
            (now, classification, session_id, kind, memory_id),
        )

        return {"applied": classification, "skipped": None}
