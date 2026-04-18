"""Observation write-path service.

Handles creation of episodic ``observations`` rows plus the supporting
``observation_embeddings`` vector row and ``audit_log`` entry, and records
re-use / outcome signals that move ``reinforcement_score`` up (success) or
down (failure).

Retrieval lives in later phases.

Transactional behaviour
-----------------------
The SQLite connection uses Python's default deferred-transaction mode. A call
to :meth:`ObservationService.create`:

    1. Calls the embedder *before* any DB write (fail-fast).
    2. Opens a SAVEPOINT, inserts the observation (AI trigger populates the
       FTS content-linked virtual table), inserts the embedding into the
       ``vec0`` table, and writes an audit row. All four statements succeed
       together or the SAVEPOINT rolls them all back.
    3. Commits the transaction.

If the SAVEPOINT is rolled back on error, the FTS trigger's side-effects are
undone along with the base-table row because SQLite FTS5 triggers participate
in the enclosing transaction.

Notes on the design spec
------------------------
The task brief originally said ``create`` should insert into
``observation_fts``. The shipped schema in ``0001_init.sql`` uses a
``content='observations'`` FTS5 external-content table with AFTER INSERT /
UPDATE / DELETE triggers that mirror writes automatically. Therefore
*no* direct insert into ``observation_fts`` is required — the trigger
handles it. The ``vec0`` ``observation_embeddings`` table, on the other
hand, is NOT trigger-populated and must be written manually.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import sqlite_vec

Outcome = Literal["success", "failure", "neutral"]
UseOutcome = Literal["success", "failure"]


def _default_clock() -> datetime:
    """UTC-aware ``now``. Kept as a module-level function for clarity."""
    return datetime.now(UTC)


class ObservationService:
    """Service for creating observations and recording their reinforcement.

    Connection ownership
    --------------------
    ``ObservationService`` assumes it owns the provided :class:`sqlite3.Connection`
    and is free to call :meth:`~sqlite3.Connection.commit` and
    :meth:`~sqlite3.Connection.rollback` on it. Callers must not share a
    connection that already has an open transaction with other services: the
    ``commit()`` in :meth:`create` and the ``rollback()`` in
    :meth:`record_use` (unknown id path) would otherwise steal commit/rollback
    authority from the caller and either commit uncommitted work or discard it.
    This contract may be revisited when higher-level orchestration lands in a
    later phase.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedder: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        project_resolver: Callable[[], str] | None = None,
        scope_resolver: Callable[[], str | None] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._clock: Callable[[], datetime] = clock or _default_clock
        self._project_resolver: Callable[[], str] = (
            project_resolver if project_resolver is not None else (lambda: Path.cwd().name)
        )
        self._scope_resolver: Callable[[], str | None] = (
            scope_resolver if scope_resolver is not None else (lambda: None)
        )
        self._session_id = session_id if session_id is not None else uuid4().hex

    # ------------------------------------------------------------------ public
    async def create(
        self,
        content: str,
        *,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
    ) -> str:
        """Insert a new observation, embedding and audit row; return its id."""
        obs_id = uuid4().hex

        resolved_project = project if project is not None else self._project_resolver()
        resolved_scope = scope_path if scope_path is not None else self._scope_resolver()

        # Fail fast: compute the embedding BEFORE opening a write transaction
        # so a slow / broken Ollama server does not leave a pending SAVEPOINT.
        vector = await self._embedder.embed(content)
        vec_blob = sqlite_vec.serialize_float32(vector)

        now = self._clock().isoformat()

        conn = self._conn
        conn.execute("SAVEPOINT observation_create")
        try:
            conn.execute(
                """
                INSERT INTO observations (
                    id, content, project, component, theme, session_id,
                    trigger_type, outcome, reinforcement_score, scope_path,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)
                """,
                (
                    obs_id,
                    content,
                    resolved_project,
                    component,
                    theme,
                    self._session_id,
                    trigger_type,
                    outcome,
                    resolved_scope,
                    now,
                ),
            )

            conn.execute(
                "INSERT INTO observation_embeddings (observation_id, embedding) "
                "VALUES (?, ?)",
                (obs_id, vec_blob),
            )

            self._write_audit(
                entity_id=obs_id,
                action="created",
                detail={
                    "outcome": outcome,
                    "scope_path": resolved_scope,
                    "component": component,
                },
                now=now,
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT observation_create")
            conn.execute("RELEASE SAVEPOINT observation_create")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT observation_create")

        # Service owns the connection — see class docstring for the contract.
        conn.commit()
        return obs_id

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        """Bump ``used_count`` (and validation counters on outcome)."""
        now = self._clock().isoformat()
        conn = self._conn

        if outcome == "success":
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?,
                       validated_true = validated_true + 1,
                       reinforcement_score = reinforcement_score + 1.0,
                       last_validated = ?
                 WHERE id = ?
                """,
                (now, now, observation_id),
            )
        elif outcome == "failure":
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?,
                       validated_false = validated_false + 1,
                       reinforcement_score = reinforcement_score - 1.0,
                       last_validated = ?
                 WHERE id = ?
                """,
                (now, now, observation_id),
            )
        elif outcome is None:
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?
                 WHERE id = ?
                """,
                (now, observation_id),
            )
        else:  # defensive — typed as Literal so normally unreachable
            raise ValueError(f"Invalid outcome: {outcome!r}")

        if cursor.rowcount == 0:
            # No row updated — reject to give callers a clear error.
            # Service owns the connection — see class docstring for the contract.
            conn.rollback()
            raise ValueError(f"Observation not found: {observation_id}")

        self._write_audit(
            entity_id=observation_id,
            action="used",
            detail={"outcome": outcome},
            now=now,
        )
        conn.commit()

    # ----------------------------------------------------------------- helpers
    def _write_audit(
        self,
        *,
        entity_id: str,
        action: str,
        detail: dict[str, Any],
        now: str,
    ) -> None:
        """Insert a row into ``audit_log``. Caller owns the transaction."""
        self._conn.execute(
            """
            INSERT INTO audit_log (
                id, entity_type, entity_id, action, actor, detail,
                session_id, created_at
            ) VALUES (?, 'observation', ?, ?, 'ai', ?, ?, ?)
            """,
            (
                uuid4().hex,
                entity_id,
                action,
                json.dumps(detail),
                self._session_id,
                now,
            ),
        )
