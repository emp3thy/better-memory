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
                -- DISTINCT: a reflection citing two obs in this episode counts once
                SELECT COUNT(DISTINCT rs.reflection_id)
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
    useful_count: int = 0
    times_misled: int = 0
    times_overlooked: int = 0


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
    useful_only: bool = False,
) -> list[ReflectionListRow]:
    """Return reflections matching the six filter fields from spec §8.

    Status semantics:
    - When ``status`` is None (default), returns rows with
      ``status IN ('pending_review', 'confirmed')`` — the active set,
      matching ``ReflectionSynthesisService.retrieve_reflections``.
    - When ``status`` is given, exact match on that single value
      (lets the user surface ``retired`` or ``superseded`` reflections
      explicitly).

    ``useful_only``: when True, restricts to rows where ``useful_count > 0``.

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
    if useful_only:
        clauses.append("useful_count > 0")

    where = " AND ".join(clauses)
    sql = (
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, evidence_count, updated_at, "
        "useful_count, times_misled, times_overlooked "
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
            useful_count=r["useful_count"],
            times_misled=r["times_misled"],
            times_overlooked=r["times_overlooked"],
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
    scope: str
    created_at: str
    updated_at: str
    useful_count: int = 0
    last_useful_at: str | None = None
    times_misled: int = 0
    last_misled_at: str | None = None
    times_overlooked: int = 0
    last_overlooked_at: str | None = None


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

    @property
    def useful_count(self) -> int:
        return self.reflection.useful_count

    @property
    def last_useful_at(self) -> str | None:
        return self.reflection.last_useful_at

    @property
    def times_misled(self) -> int:
        return self.reflection.times_misled

    @property
    def last_misled_at(self) -> str | None:
        return self.reflection.last_misled_at

    @property
    def times_overlooked(self) -> int:
        return self.reflection.times_overlooked

    @property
    def last_overlooked_at(self) -> str | None:
        return self.reflection.last_overlooked_at


def reflection_row(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionFull | None:
    """The single-row half of reflection_detail: the reflections row mapped
    to ReflectionFull, or None if absent. No provenance."""
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, scope, "
        "created_at, updated_at, "
        "useful_count, last_useful_at, times_misled, last_misled_at, "
        "times_overlooked, last_overlooked_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
    if r_row is None:
        return None
    return ReflectionFull(
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
        scope=r_row["scope"],
        created_at=r_row["created_at"],
        updated_at=r_row["updated_at"],
        useful_count=r_row["useful_count"] or 0,
        last_useful_at=r_row["last_useful_at"],
        times_misled=r_row["times_misled"] or 0,
        last_misled_at=r_row["last_misled_at"],
        times_overlooked=r_row["times_overlooked"] or 0,
        last_overlooked_at=r_row["last_overlooked_at"],
    )


def reflection_provenance(
    conn: sqlite3.Connection, *, reflection_id: str
) -> list[ReflectionSourceObservation]:
    """The source-observation half of reflection_detail. Empty list when the
    reflection has no sources (or does not exist).

    Sources are joined through ``reflection_sources`` to ``observations``
    and from there to ``episodes`` so the drawer can show the owning
    episode's goal + outcome + close_reason for each piece of evidence.

    Source ordering: ``observations.created_at DESC, observations.rowid DESC``.
    Same-status policy as Phase 8's episode_detail: ALL source
    observations are returned regardless of ``observations.status``.
    """
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
    return [
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


def reflection_detail(
    conn: sqlite3.Connection, *, reflection_id: str
) -> ReflectionDetail | None:
    """Return one reflection with its source observations, or None.

    Recomposed from reflection_row + reflection_provenance — output is
    byte-identical to the pre-split version (pinned by
    test_reflection_detail_composes_from_row_and_provenance).
    """
    reflection = reflection_row(conn, reflection_id=reflection_id)
    if reflection is None:
        return None
    return ReflectionDetail(
        reflection=reflection,
        sources=reflection_provenance(conn, reflection_id=reflection_id),
    )


@dataclass(frozen=True)
class ObservationRow:
    id: str
    content: str
    project: str
    component: str | None
    theme: str | None
    outcome: str
    status: str
    created_at: str
    episode_id: str | None
    reinforcement_score: float


def observation_list_for_ui(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    component: str | None = None,
    limit: int = 100,
) -> list[ObservationRow]:
    """Observation list with optional filters. Newest first.

    No filter defaults: omitting ``project``/``status``/``outcome``/
    ``component`` returns observations across all values for that
    column. The panel shows everything on first load (filters are
    user-driven).
    """
    clauses: list[str] = []
    params: list = []
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    if component is not None:
        clauses.append("component = ?")
        params.append(component)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, content, project, component, theme, outcome, status, "
        "       created_at, episode_id, reinforcement_score "
        "FROM observations "
        f"{where} "
        "ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?"
    )
    params.append(limit)
    return [
        ObservationRow(
            id=r["id"],
            content=r["content"],
            project=r["project"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            status=r["status"],
            created_at=r["created_at"],
            episode_id=r["episode_id"],
            reinforcement_score=r["reinforcement_score"],
        )
        for r in conn.execute(sql, params).fetchall()
    ]


def observation_distinct_projects(conn: sqlite3.Connection) -> list[str]:
    """Return all distinct project values in the observations table, sorted.

    Powers the Project dropdown on the observations page.
    """
    return [
        r["project"]
        for r in conn.execute(
            "SELECT DISTINCT project FROM observations "
            "WHERE project IS NOT NULL AND project != '' "
            "ORDER BY project COLLATE NOCASE"
        ).fetchall()
    ]


def reflection_distinct_projects(conn: sqlite3.Connection) -> list[str]:
    """Return all distinct project values in the reflections table, sorted.

    Powers the Project dropdown on the reflections page.
    """
    return [
        r["project"]
        for r in conn.execute(
            "SELECT DISTINCT project FROM reflections "
            "WHERE project IS NOT NULL AND project != '' "
            "ORDER BY project COLLATE NOCASE"
        ).fetchall()
    ]


@dataclass(frozen=True)
class ObservationFull:
    """All columns from observations that the drawer renders."""

    id: str
    content: str
    project: str
    component: str | None
    theme: str | None
    tech: str | None
    trigger_type: str | None
    outcome: str
    status: str
    reinforcement_score: float
    episode_id: str | None
    created_at: str


@dataclass(frozen=True)
class LinkedReflectionRow:
    """One reflection that cites the observation under inspection."""

    id: str
    title: str
    polarity: str
    confidence: float
    status: str


@dataclass(frozen=True)
class ObservationAuditEntry:
    created_at: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None


@dataclass(frozen=True)
class ObservationDetail:
    observation: ObservationFull
    audit: list[ObservationAuditEntry]
    reflections: list[LinkedReflectionRow]


def observation_detail(
    conn: sqlite3.Connection, *, observation_id: str
) -> ObservationDetail | None:
    """Return one observation with audit + linked reflections, or None."""
    obs_row = conn.execute(
        "SELECT id, content, project, component, theme, tech, "
        "       trigger_type, outcome, status, reinforcement_score, "
        "       episode_id, created_at "
        "FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    if obs_row is None:
        return None

    observation = ObservationFull(
        id=obs_row["id"],
        content=obs_row["content"],
        project=obs_row["project"],
        component=obs_row["component"],
        theme=obs_row["theme"],
        tech=obs_row["tech"],
        trigger_type=obs_row["trigger_type"],
        outcome=obs_row["outcome"],
        status=obs_row["status"],
        reinforcement_score=obs_row["reinforcement_score"],
        episode_id=obs_row["episode_id"],
        created_at=obs_row["created_at"],
    )

    audit_rows = conn.execute(
        "SELECT created_at, actor, action, from_status, to_status "
        "FROM audit_log "
        "WHERE entity_type = 'observation' AND entity_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (observation_id,),
    ).fetchall()
    audit = [
        ObservationAuditEntry(
            created_at=r["created_at"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
        )
        for r in audit_rows
    ]

    refl_rows = conn.execute(
        """
        SELECT r.id, r.title, r.polarity, r.confidence, r.status
        FROM reflections r
        JOIN reflection_sources rs ON rs.reflection_id = r.id
        WHERE rs.observation_id = ?
        ORDER BY r.confidence DESC, r.id ASC
        """,
        (observation_id,),
    ).fetchall()
    reflections = [
        LinkedReflectionRow(
            id=r["id"],
            title=r["title"],
            polarity=r["polarity"],
            confidence=r["confidence"],
            status=r["status"],
        )
        for r in refl_rows
    ]

    return ObservationDetail(
        observation=observation,
        audit=audit,
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


@dataclass(frozen=True)
class HookErrorRow:
    id: str
    created_at: str
    hook_name: str
    exception_type: str
    exception_message: str | None
    traceback: str | None
    cwd: str | None


def hook_errors_list_for_ui(
    conn: sqlite3.Connection, *, limit: int = 100,
) -> list[HookErrorRow]:
    """Return recent hook errors, newest first."""
    rows = conn.execute(
        "SELECT id, created_at, hook_name, exception_type, "
        "       exception_message, traceback, cwd "
        "FROM hook_errors "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        HookErrorRow(
            id=r["id"],
            created_at=r["created_at"],
            hook_name=r["hook_name"],
            exception_type=r["exception_type"],
            exception_message=r["exception_message"],
            traceback=r["traceback"],
            cwd=r["cwd"],
        )
        for r in rows
    ]


def hook_error_detail(
    conn: sqlite3.Connection, *, error_id: str,
) -> HookErrorRow | None:
    """Single row by id for the drawer view."""
    row = conn.execute(
        "SELECT id, created_at, hook_name, exception_type, "
        "       exception_message, traceback, cwd "
        "FROM hook_errors WHERE id = ?",
        (error_id,),
    ).fetchone()
    if row is None:
        return None
    return HookErrorRow(
        id=row["id"],
        created_at=row["created_at"],
        hook_name=row["hook_name"],
        exception_type=row["exception_type"],
        exception_message=row["exception_message"],
        traceback=row["traceback"],
        cwd=row["cwd"],
    )


@dataclass(frozen=True)
class RetentionRunRow:
    id: int
    run_at: str
    archived_via_retired_reflection: int
    archived_via_consumed_without_reflection: int
    archived_via_no_outcome_episode: int
    pruned: int
    triggered_by: str


@dataclass(frozen=True)
class RatingEvidenceRow:
    """One rated exposure that carries an evidence line.

    Sourced from ``session_memory_exposure`` (migration 0016's ``evidence``
    column). Distinct from ``ReflectionFull.evidence_count``/``evidence_count``
    on ``ReflectionListRow`` — those count synthesis SOURCE OBSERVATIONS and
    have nothing to do with rating evidence.
    """

    classification: str
    evidence: str
    rated_at: str


def fetch_rating_evidence(
    conn: sqlite3.Connection, kind: str, memory_id: str, limit: int = 10,
) -> list[RatingEvidenceRow]:
    """Return rated exposures with a non-null evidence line, newest first.

    ``kind`` is ``'reflection'`` or ``'semantic'`` (matches
    ``session_memory_exposure.memory_kind``). Only rows where
    ``evidence IS NOT NULL`` are returned — ``ignored`` classifications may
    or may not carry one (see MemoryRatingService), everything else is
    required to. ``limit`` is enforced in SQL via ``LIMIT``.
    """
    rows = conn.execute(
        "SELECT classification, evidence, rated_at "
        "FROM session_memory_exposure "
        "WHERE memory_kind = ? AND memory_id = ? AND evidence IS NOT NULL "
        "ORDER BY rated_at DESC "
        "LIMIT ?",
        (kind, memory_id, limit),
    ).fetchall()
    return [
        RatingEvidenceRow(
            classification=r["classification"],
            evidence=r["evidence"],
            rated_at=r["rated_at"],
        )
        for r in rows
    ]


def retention_runs_list_for_ui(
    conn: sqlite3.Connection, *, limit: int = 30,
) -> list[RetentionRunRow]:
    """Return recent retention runs, newest first."""
    rows = conn.execute(
        "SELECT id, run_at, archived_via_retired_reflection, "
        "       archived_via_consumed_without_reflection, "
        "       archived_via_no_outcome_episode, pruned, triggered_by "
        "FROM retention_runs "
        "ORDER BY run_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        RetentionRunRow(
            id=r["id"],
            run_at=r["run_at"],
            archived_via_retired_reflection=r["archived_via_retired_reflection"],
            archived_via_consumed_without_reflection=r[
                "archived_via_consumed_without_reflection"
            ],
            archived_via_no_outcome_episode=r["archived_via_no_outcome_episode"],
            pruned=r["pruned"],
            triggered_by=r["triggered_by"],
        )
        for r in rows
    ]
