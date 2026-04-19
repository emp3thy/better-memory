"""Consolidation engine — cluster observations, draft candidate insights,
flag stale observations for sweep, and merge duplicate candidates.

Spec: §9 of the design spec.
Triggered by the UI; never runs automatically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
