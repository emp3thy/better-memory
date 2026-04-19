"""Aggregate read-only queries for the Management UI.

These helpers own no transactions — they call SELECT only. Writes go
through the service layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


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
