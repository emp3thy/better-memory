"""Aggregate read-only queries for the Management UI.

These helpers own no transactions — they call SELECT only. Writes go
through the service layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from better_memory.services.episode import Episode
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
        ORDER BY o.created_at DESC, o.rowid DESC
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


@dataclass(frozen=True)
class EpisodeRow:
    """Read model for one row in the Episodes timeline."""

    id: str
    project: str
    tech: str | None
    goal: str | None
    started_at: str
    hardened_at: str | None
    ended_at: str | None
    close_reason: str | None
    outcome: str | None
    observation_count: int
    reflection_count: int


def episode_list_for_ui(
    conn: sqlite3.Connection, *, project: str, limit: int = 100
) -> list[EpisodeRow]:
    """Return episodes for ``project`` newest-first with attached counts.

    ``observation_count`` is the number of observations directly bound to this
    episode (``observations.episode_id = e.id``).

    ``reflection_count`` is the number of *distinct* reflections seeded by any
    observation in this episode.  A reflection that cites two observations in
    the same episode is counted once, not twice.

    Counts are computed via correlated subqueries (one each for observations
    and reflection_sources joined back through observations) — the timeline is
    small so this is cheap and avoids fan-out duplication from a JOIN.

    ``limit`` caps the number of rows returned (default 100 — slightly higher
    than the 50 used for finer-grained objects like observations because
    episodes are coarser-grained and there are fewer of them per project).
    """
    sql = """
        SELECT
            e.id,
            e.project,
            e.tech,
            e.goal,
            e.started_at,
            e.hardened_at,
            e.ended_at,
            e.close_reason,
            e.outcome,
            (
                SELECT COUNT(*) FROM observations o
                WHERE o.episode_id = e.id
            ) AS observation_count,
            (
                SELECT COUNT(DISTINCT rs.reflection_id)  -- DISTINCT: a reflection citing two obs in this episode counts once
                FROM reflection_sources rs
                JOIN observations o ON o.id = rs.observation_id
                WHERE o.episode_id = e.id
            ) AS reflection_count
        FROM episodes e
        WHERE e.project = ?
        ORDER BY e.started_at DESC, e.rowid DESC
        LIMIT ?
    """
    return [
        EpisodeRow(
            id=r["id"],
            project=r["project"],
            tech=r["tech"],
            goal=r["goal"],
            started_at=r["started_at"],
            hardened_at=r["hardened_at"],
            ended_at=r["ended_at"],
            close_reason=r["close_reason"],
            outcome=r["outcome"],
            observation_count=r["observation_count"],
            reflection_count=r["reflection_count"],
        )
        for r in conn.execute(sql, (project, limit)).fetchall()
    ]


def _episode_from_row(row: sqlite3.Row) -> Episode:
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


@dataclass(frozen=True)
class EpisodeObservationRow:
    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    created_at: str


@dataclass(frozen=True)
class EpisodeReflectionRow:
    id: str
    title: str
    phase: str
    polarity: str
    confidence: float
    status: str


@dataclass(frozen=True)
class EpisodeDetail:
    episode: Episode
    observations: list[EpisodeObservationRow]
    reflections: list[EpisodeReflectionRow]


def episode_detail(
    conn: sqlite3.Connection, *, episode_id: str
) -> EpisodeDetail | None:
    """Return one episode with its observations and seeded reflections.

    Returns ``None`` if no episode with this id exists.

    The drawer is a historical record: ALL observations bound to the
    episode are returned regardless of ``status`` (active, archived,
    consumed_without_reflection, etc.). This deliberately differs from
    ``list_observations`` which filters to ``status='active'`` for the
    live pipeline view. Likewise, reflections are returned regardless
    of ``status`` (pending_review / confirmed / retired / superseded)
    so a closed episode's full provenance trail is visible.

    Reflections are deduped — an episode's two observations seeding the
    same reflection produces a single row in the result.
    """
    ep_row = conn.execute(
        "SELECT * FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep_row is None:
        return None

    obs_rows = conn.execute(
        "SELECT id, content, component, theme, outcome, created_at "
        "FROM observations WHERE episode_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (episode_id,),
    ).fetchall()
    observations = [
        EpisodeObservationRow(
            id=r["id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            created_at=r["created_at"],
        )
        for r in obs_rows
    ]

    refl_rows = conn.execute(
        """
        SELECT DISTINCT
            r.id, r.title, r.phase, r.polarity, r.confidence, r.status
        FROM reflections r
        JOIN reflection_sources rs ON rs.reflection_id = r.id
        JOIN observations o ON o.id = rs.observation_id
        WHERE o.episode_id = ?
        ORDER BY r.confidence DESC, r.id ASC
        """,
        (episode_id,),
    ).fetchall()
    reflections = [
        EpisodeReflectionRow(
            id=r["id"],
            title=r["title"],
            phase=r["phase"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
        )
        for r in refl_rows
    ]

    return EpisodeDetail(
        episode=_episode_from_row(ep_row),
        observations=observations,
        reflections=reflections,
    )


def unclosed_episode_count(
    conn: sqlite3.Connection, *, project: str
) -> int:
    """Return the number of unclosed episodes for ``project``.

    Used by the Episodes-tab banner: any value > 0 surfaces the banner.
    Filtering to a specific session is intentionally NOT done here — the
    UI does not bind to a session, and the banner is meant to flag
    "anything still open" so the user can act on it.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes "
        "WHERE project = ? AND ended_at IS NULL",
        (project,),
    ).fetchone()
    return int(row["n"])
