"""Aggregate read-only queries for the Management UI.

These helpers own no transactions — they call SELECT only. Writes go
through the service layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from better_memory.services.episode import Episode, row_to_episode


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
    consumed_without_reflection, etc.), so a closed episode's full
    provenance trail is visible. Likewise, reflections are returned
    regardless of their ``status`` (pending_review / confirmed /
    retired / superseded).

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
        episode=row_to_episode(ep_row),
        observations=observations,
        reflections=reflections,
    )


@dataclass(frozen=True)
class ReflectionListRow:
    """Read model for one row in the Reflections list."""

    id: str
    title: str
    project: str
    tech: str | None
    phase: str
    polarity: str
    confidence: float
    status: str
    use_cases: str
    evidence_count: int
    updated_at: str


# Default status filter — matches retrieve_reflections (active set only).
_DEFAULT_REFLECTION_STATUSES = ("pending_review", "confirmed")


def reflection_list_for_ui(
    conn: sqlite3.Connection,
    *,
    project: str,
    tech: str | None = None,
    phase: str | None = None,
    polarity: str | None = None,
    status: str | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[ReflectionListRow]:
    """Return reflections matching the six filter fields from spec §8.

    Status semantics:
    - When ``status`` is None (default), returns rows with
      ``status IN ('pending_review', 'confirmed')`` — the active set,
      matching ``ReflectionSynthesisService.retrieve_reflections``.
    - When ``status`` is given, exact match on that single value
      (lets the user surface ``retired`` or ``superseded`` reflections
      explicitly).

    Order: ``confidence DESC, updated_at DESC, rowid DESC``.
    Cap: ``limit`` rows (default 100). ``min_confidence`` is a
    floor — rows with confidence strictly less than this are dropped.
    """
    clauses: list[str] = ["project = ?"]
    params: list[object] = [project]

    if status is None:
        clauses.append(
            "status IN ("
            + ", ".join("?" * len(_DEFAULT_REFLECTION_STATUSES))
            + ")"
        )
        params.extend(_DEFAULT_REFLECTION_STATUSES)
    else:
        clauses.append("status = ?")
        params.append(status)

    if tech is not None:
        clauses.append("tech = ?")
        params.append(tech)
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    if polarity is not None:
        clauses.append("polarity = ?")
        params.append(polarity)
    if min_confidence > 0.0:
        clauses.append("confidence >= ?")
        params.append(min_confidence)

    where = " AND ".join(clauses)
    sql = (
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, evidence_count, updated_at "
        f"FROM reflections WHERE {where} "
        "ORDER BY confidence DESC, updated_at DESC, rowid DESC "
        "LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        ReflectionListRow(
            id=r["id"],
            title=r["title"],
            project=r["project"],
            tech=r["tech"],
            phase=r["phase"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
            use_cases=r["use_cases"],
            evidence_count=r["evidence_count"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class ReflectionFull:
    """Full reflection row for the drawer."""

    id: str
    title: str
    project: str
    tech: str | None
    phase: str
    polarity: str
    confidence: float
    status: str
    use_cases: str
    hints: str
    evidence_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReflectionSourceObservation:
    """One source observation with its owning episode's outcome.

    Joined: reflection_sources → observations → episodes.
    """

    observation_id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str  # observation outcome (success/failure/neutral)
    created_at: str
    episode_id: str
    episode_goal: str | None
    episode_outcome: str | None  # episode outcome — None on still-open ones
    episode_close_reason: str | None


@dataclass(frozen=True)
class ReflectionDetail:
    reflection: ReflectionFull
    sources: list[ReflectionSourceObservation]


def reflection_detail(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionDetail | None:
    """Return one reflection with its source observations.

    Sources are joined through ``reflection_sources`` to ``observations``
    and from there to ``episodes`` so the drawer can show the owning
    episode's goal + outcome + close_reason for each piece of evidence.

    Returns ``None`` if no reflection with this id exists.

    Source ordering: ``observations.created_at DESC, observations.rowid DESC``.
    Same-status policy as Phase 8's episode_detail: ALL source
    observations are returned regardless of ``observations.status``.
    """
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, "
        "created_at, updated_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
    if r_row is None:
        return None

    src_rows = conn.execute(
        """
        SELECT
            o.id              AS observation_id,
            o.content         AS content,
            o.component       AS component,
            o.theme           AS theme,
            o.outcome         AS obs_outcome,
            o.created_at      AS obs_created_at,
            e.id              AS episode_id,
            e.goal            AS episode_goal,
            e.outcome         AS episode_outcome,
            e.close_reason    AS episode_close_reason
        FROM reflection_sources rs
        JOIN observations o ON o.id = rs.observation_id
        JOIN episodes     e ON e.id = o.episode_id
        WHERE rs.reflection_id = ?
        ORDER BY o.created_at DESC, o.rowid DESC
        """,
        (reflection_id,),
    ).fetchall()
    sources = [
        ReflectionSourceObservation(
            observation_id=r["observation_id"],
            content=r["content"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["obs_outcome"],
            created_at=r["obs_created_at"],
            episode_id=r["episode_id"],
            episode_goal=r["episode_goal"],
            episode_outcome=r["episode_outcome"],
            episode_close_reason=r["episode_close_reason"],
        )
        for r in src_rows
    ]
    return ReflectionDetail(
        reflection=ReflectionFull(
            id=r_row["id"],
            title=r_row["title"],
            project=r_row["project"],
            tech=r_row["tech"],
            phase=r_row["phase"],
            polarity=r_row["polarity"],
            confidence=r_row["confidence"],
            status=r_row["status"],
            use_cases=r_row["use_cases"],
            hints=r_row["hints"],
            evidence_count=r_row["evidence_count"],
            created_at=r_row["created_at"],
            updated_at=r_row["updated_at"],
        ),
        sources=sources,
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
