"""Consolidation engine — cluster observations, draft candidate insights,
flag stale observations for sweep, and merge duplicate candidates.

Spec: §9 of the design spec.
Triggered by the UI; never runs automatically.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from better_memory.llm.ollama import ChatCompleter
from better_memory.services.insight import Insight, row_to_insight


@dataclass(frozen=True)
class ObservationCluster:
    """A group of observations sharing (project, component, theme).

    ``observation_ids`` is ordered by ``created_at ASC`` so draft prompts
    present the oldest context first.
    """

    project: str
    component: str | None
    theme: str | None
    observation_ids: list[str]
    total_validated_true: int


def find_clusters(
    conn: sqlite3.Connection,
    *,
    project: str,
    min_size: int = 3,
    min_validated: int = 2,
) -> list[ObservationCluster]:
    """Return clusters of active observations that meet the thresholds.

    Spec §9 branch step 1-2: group by ``(project, component, theme)`` and
    keep only clusters with ``>= min_size`` observations AND
    ``>= min_validated`` total ``validated_true`` across the cluster.
    Observations with ``status != 'active'`` are excluded.
    """
    rows = conn.execute(
        """
        SELECT id, component, theme, validated_true
        FROM observations
        WHERE project = ? AND status = 'active'
        ORDER BY component, theme, created_at ASC, rowid ASC
        """,
        (project,),
    ).fetchall()

    groups: dict[
        tuple[str | None, str | None], list[sqlite3.Row]
    ] = {}
    for r in rows:
        key = (r["component"], r["theme"])
        groups.setdefault(key, []).append(r)

    out: list[ObservationCluster] = []
    for (component, theme), members in groups.items():
        if len(members) < min_size:
            continue
        total_validated = sum(m["validated_true"] for m in members)
        if total_validated < min_validated:
            continue
        out.append(
            ObservationCluster(
                project=project,
                component=component,
                theme=theme,
                observation_ids=[m["id"] for m in members],
                total_validated_true=total_validated,
            )
        )
    return out


@dataclass(frozen=True)
class ObservationForPrompt:
    """Subset of observation fields the draft prompt shows to the LLM."""

    id: str
    created_at: str
    content: str
    outcome: str


def build_draft_prompt(observations: list[ObservationForPrompt]) -> str:
    """Build the insight-draft prompt from spec §9."""
    lines = [
        f"Here are {len(observations)} observations about the same pattern:",
        "",
    ]
    for obs in observations:
        lines.append(
            f"- [{obs.created_at}] ({obs.outcome}) {obs.id}: {obs.content}"
        )
    lines.extend(
        [
            "",
            "Write a single insight that:",
            "- Generalises the pattern in present tense",
            "- States the conditions under which it holds",
            "- Notes any exceptions observed",
            "- Is specific enough to be actionable",
            "- Is concise (2-4 sentences for the pattern, 1-2 for conditions/exceptions)",
            "",
            "Return the insight text only, no preamble or formatting.",
        ]
    )
    return "\n".join(lines)


Polarity = Literal["do", "dont", "neutral"]


def _default_clock() -> datetime:
    """UTC-aware ``now``. Module-level so tests can patch for determinism."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class BranchCandidate:
    """A drafted insight ready for human review.

    Phase 3 callers feed this into ``apply_branch`` after human approval.
    """

    project: str
    component: str | None
    theme: str | None
    title: str
    content: str
    polarity: Polarity
    observation_ids: list[str]
    confidence: str  # "low" | "medium" | "high"


def _infer_polarity(outcomes: list[str]) -> Polarity:
    """Majority-vote outcome → polarity mapping. Empty list → neutral."""
    if not outcomes:
        return "neutral"
    counts = Counter(outcomes)
    top, _ = counts.most_common(1)[0]
    if top == "success":
        return "do"
    if top == "failure":
        return "dont"
    return "neutral"


def _derive_title(content: str) -> str:
    """First sentence or first 80 chars of ``content`` as a title."""
    first = content.split(".", 1)[0].strip()
    if len(first) > 80:
        first = first[:77].rstrip() + "..."
    return first or "Untitled insight"


def existing_insight_for_cluster(
    conn: sqlite3.Connection, cluster: ObservationCluster
) -> Insight | None:
    """Return the first confirmed or promoted insight matching the cluster.

    Match criterion: same ``(project, component)`` AND
    ``status IN ('confirmed', 'promoted')``. Both statuses mean a human
    has accepted the insight, so both count as "already exists".
    """
    row = conn.execute(
        """
        SELECT * FROM insights
        WHERE project = ?
          AND (component IS ? OR component = ?)
          AND status IN ('confirmed', 'promoted')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (cluster.project, cluster.component, cluster.component),
    ).fetchone()
    if row is None:
        return None
    return row_to_insight(row)


class ConsolidationService:
    """Consolidation engine: branch pass, sweep pass, merge.

    The service owns the sqlite connection and expects writes to be
    sequenced by the caller (Phase 2's Flask factory runs with
    ``threaded=False``, so one request / one job at a time).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        chat: ChatCompleter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._chat = chat
        self._clock: Callable[[], datetime] = clock or _default_clock

    async def branch_dry_run(
        self, *, project: str
    ) -> list[BranchCandidate]:
        """Return draft candidates for clusters needing consolidation.

        Does NOT write to the database. Caller applies each accepted
        candidate via :meth:`apply_branch`.
        """
        clusters = find_clusters(self._conn, project=project)
        if not clusters:
            return []

        out: list[BranchCandidate] = []
        for cluster in clusters:
            if existing_insight_for_cluster(self._conn, cluster) is not None:
                continue

            rows = self._conn.execute(
                f"""
                SELECT id, content, created_at, outcome
                FROM observations
                WHERE id IN ({",".join("?" * len(cluster.observation_ids))})
                ORDER BY created_at ASC, rowid ASC
                """,
                cluster.observation_ids,
            ).fetchall()

            prompt_rows = [
                ObservationForPrompt(
                    id=r["id"],
                    created_at=r["created_at"],
                    content=r["content"],
                    outcome=r["outcome"],
                )
                for r in rows
            ]
            prompt = build_draft_prompt(prompt_rows)
            drafted = (await self._chat.complete(prompt)).strip()
            if not drafted:
                continue

            polarity = _infer_polarity([r["outcome"] for r in rows])
            confidence = "high" if len(rows) >= 5 else "medium"

            out.append(
                BranchCandidate(
                    project=cluster.project,
                    component=cluster.component,
                    theme=cluster.theme,
                    title=_derive_title(drafted),
                    content=drafted,
                    polarity=polarity,
                    observation_ids=list(cluster.observation_ids),
                    confidence=confidence,
                )
            )
        return out

    async def apply_branch(self, candidate: BranchCandidate) -> str:
        """Persist ``candidate`` — create the insight, link sources, mark
        observations consolidated. Atomic. Returns the new insight id."""
        from uuid import uuid4

        insight_id = uuid4().hex
        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT apply_branch")
        try:
            conn.execute(
                """
                INSERT INTO insights
                    (id, title, content, project, component, status,
                     confidence, polarity, evidence_count,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending_review',
                        ?, ?, ?,
                        ?, ?)
                """,
                (
                    insight_id,
                    candidate.title,
                    candidate.content,
                    candidate.project,
                    candidate.component,
                    candidate.confidence,
                    candidate.polarity,
                    len(candidate.observation_ids),
                    now,
                    now,
                ),
            )
            for obs_id in candidate.observation_ids:
                conn.execute(
                    "INSERT INTO insight_sources (insight_id, observation_id) "
                    "VALUES (?, ?)",
                    (insight_id, obs_id),
                )
            placeholders = ",".join("?" * len(candidate.observation_ids))
            conn.execute(
                f"UPDATE observations SET status = 'consolidated' "
                f"WHERE id IN ({placeholders})",
                candidate.observation_ids,
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT apply_branch")
            conn.execute("RELEASE SAVEPOINT apply_branch")
            raise
        conn.execute("RELEASE SAVEPOINT apply_branch")
        conn.commit()
        return insight_id
