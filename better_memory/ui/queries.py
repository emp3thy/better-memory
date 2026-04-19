"""Aggregate read-only queries for the Management UI.

These helpers own no transactions — they call SELECT only. Writes go
through the service layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from better_memory.services.insight import Insight, row_to_insight


@dataclass(frozen=True)
class KanbanCounts:
    observations: int
    candidates: int
    insights: int
    promoted: int


def kanban_counts(conn: sqlite3.Connection, *, project: str) -> KanbanCounts:
    """Return the four pipeline-stage counts for ``project``."""
    obs_row = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE status = 'active' AND project = ?",
        (project,),
    ).fetchone()
    cand_row = conn.execute(
        "SELECT COUNT(*) FROM insights "
        "WHERE status = 'pending_review' AND project = ?",
        (project,),
    ).fetchone()
    ins_row = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE status = 'confirmed' AND project = ?",
        (project,),
    ).fetchone()
    pro_row = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE status = 'promoted' AND project = ?",
        (project,),
    ).fetchone()
    return KanbanCounts(
        observations=obs_row[0],
        candidates=cand_row[0],
        insights=ins_row[0],
        promoted=pro_row[0],
    )


@dataclass(frozen=True)
class ObservationListRow:
    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    created_at: str


def list_observations(
    conn: sqlite3.Connection,
    *,
    project: str,
    limit: int = 50,
) -> list[ObservationListRow]:
    """Return active observations for ``project``, newest first."""
    rows = conn.execute(
        """
        SELECT id, content, component, theme, outcome, created_at
        FROM observations
        WHERE status = 'active' AND project = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (project, limit),
    ).fetchall()
    return [
        ObservationListRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _list_insights_by_status(
    conn: sqlite3.Connection, *, project: str, status: str, limit: int
) -> list[Insight]:
    rows = conn.execute(
        """
        SELECT * FROM insights
        WHERE project = ? AND status = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (project, status, limit),
    ).fetchall()
    return [row_to_insight(r) for r in rows]


def list_candidates(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="pending_review", limit=limit
    )


def list_insights(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="confirmed", limit=limit
    )


def list_promoted(
    conn: sqlite3.Connection, *, project: str, limit: int = 50
) -> list[Insight]:
    return _list_insights_by_status(
        conn, project=project, status="promoted", limit=limit
    )


def list_insight_sources(
    conn: sqlite3.Connection, *, insight_id: str
) -> list[ObservationListRow]:
    """Return the observations linked to ``insight_id`` via insight_sources."""
    rows = conn.execute(
        """
        SELECT o.id, o.content, o.component, o.theme, o.outcome, o.created_at
        FROM insight_sources s
        JOIN observations o ON o.id = s.observation_id
        WHERE s.insight_id = ?
        ORDER BY o.created_at DESC, o.id DESC
        """,
        (insight_id,),
    ).fetchall()
    return [
        ObservationListRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
