"""Reflection synthesis service.

Orchestrates the Phase 5 synthesis flow defined in spec §5: load
context (existing reflections + new observations joined with episode
outcome), prompt an LLM for a structured response, and apply the
response atomically (new reflections, augmentations, merges,
ignores), updating the ``synthesis_runs`` watermark.

This module provides:
- Typed read models for LLM consumption (:class:`ReflectionForPrompt`,
  :class:`ObservationForPrompt`).
- :class:`SynthesisContext` aggregating them plus the watermark.
- :class:`ReflectionSynthesisService` with a ``load_context`` method
  (Task 2) that Tasks 3-10 build on.

Design notes:
- The service owns writes within its own transaction envelope
  (SAVEPOINT + commit), matching the convention used by
  ObservationService and EpisodeService.
- Context loading is read-only and commits nothing.
- The LLM client is injected via a ``ChatCompleter`` Protocol so
  tests can swap :class:`better_memory.llm.fake.FakeChat` in
  without touching Ollama.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from better_memory.llm.ollama import ChatCompleter


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReflectionForPrompt:
    """Read model for an existing reflection, as seen by the synthesis prompt."""

    id: str
    title: str
    tech: str | None
    phase: str
    polarity: str
    use_cases: str
    hints: str  # JSON-encoded list
    confidence: float
    status: str


@dataclass(frozen=True)
class ObservationForPrompt:
    """Read model for a new observation, joined with its episode's goal and outcome."""

    id: str
    content: str
    outcome: str
    component: str | None
    theme: str | None
    tech: str | None
    created_at: str
    episode_goal: str | None
    episode_outcome: str | None


@dataclass(frozen=True)
class SynthesisContext:
    """Inputs to the synthesis prompt plus the last-run watermark."""

    reflections: list[ReflectionForPrompt]
    observations: list[ObservationForPrompt]
    last_run_at: str | None  # ISO-8601 timestamp of the last synthesis, or None


class ReflectionSynthesisService:
    """Orchestrates pre-start synthesis: load, prompt, parse, apply, return.

    Connection ownership: the service writes within its own SAVEPOINT +
    commit envelope for apply methods. ``load_context`` is read-only.
    Callers must not share a connection that already has an open
    outer transaction with other services.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        chat: ChatCompleter,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._chat = chat
        self._clock: Callable[[], datetime] = clock or _default_clock

    # -------------------------------------------------------------- load_context
    def load_context(
        self, *, project: str, tech: str | None
    ) -> SynthesisContext:
        """Fetch reflections + new observations for the synthesis prompt.

        Reflections: ``status IN ('pending_review', 'confirmed')`` for
        ``project``. When ``tech`` is given, rows match either the same
        ``tech`` exactly OR ``tech IS NULL`` (cross-tech reflections
        are surfaced regardless of the incoming tech tag).

        Observations: rows written since the synthesis watermark
        (``synthesis_runs.last_run_at`` for ``(project, tech_key)``
        where ``tech_key`` is ``tech`` or ``''`` when tech is None).
        Further filtered to episodes with ``outcome IN ('success',
        'partial', 'abandoned')`` per spec §5.1. Each observation is
        returned with its owning episode's ``goal`` and ``outcome``
        joined in.

        When no prior synthesis run exists, the watermark is NULL and
        all eligible observations are returned.
        """
        # --- Reflections --------------------------------------------------
        if tech is None:
            refl_rows = self._conn.execute(
                """
                SELECT id, title, tech, phase, polarity, use_cases, hints,
                       confidence, status
                FROM reflections
                WHERE project = ?
                  AND status IN ('pending_review', 'confirmed')
                ORDER BY confidence DESC, updated_at DESC
                """,
                (project,),
            ).fetchall()
        else:
            refl_rows = self._conn.execute(
                """
                SELECT id, title, tech, phase, polarity, use_cases, hints,
                       confidence, status
                FROM reflections
                WHERE project = ?
                  AND status IN ('pending_review', 'confirmed')
                  AND (tech = ? OR tech IS NULL)
                ORDER BY confidence DESC, updated_at DESC
                """,
                (project, tech),
            ).fetchall()

        reflections = [
            ReflectionForPrompt(
                id=r["id"], title=r["title"], tech=r["tech"],
                phase=r["phase"], polarity=r["polarity"],
                use_cases=r["use_cases"], hints=r["hints"],
                confidence=r["confidence"], status=r["status"],
            )
            for r in refl_rows
        ]

        # --- Watermark ----------------------------------------------------
        tech_key = tech if tech is not None else ""
        run_row = self._conn.execute(
            "SELECT last_run_at FROM synthesis_runs "
            "WHERE project = ? AND tech = ?",
            (project, tech_key),
        ).fetchone()
        last_run_at = run_row["last_run_at"] if run_row is not None else None

        # --- Observations -------------------------------------------------
        # Join observations → episodes so we can (a) filter by episode.outcome
        # and (b) surface episode.goal / episode.outcome to the LLM.
        params: list[object] = [project]
        obs_sql = """
            SELECT o.id, o.content, o.outcome, o.component, o.theme, o.tech,
                   o.created_at, e.goal AS episode_goal,
                   e.outcome AS episode_outcome
            FROM observations o
            JOIN episodes e ON e.id = o.episode_id
            WHERE o.project = ?
              AND e.outcome IN ('success', 'partial', 'abandoned')
        """
        if last_run_at is not None:
            obs_sql += " AND o.created_at > ?"
            params.append(last_run_at)
        obs_sql += " ORDER BY o.created_at ASC, o.rowid ASC"

        obs_rows = self._conn.execute(obs_sql, params).fetchall()

        observations = [
            ObservationForPrompt(
                id=r["id"], content=r["content"], outcome=r["outcome"],
                component=r["component"], theme=r["theme"], tech=r["tech"],
                created_at=r["created_at"],
                episode_goal=r["episode_goal"],
                episode_outcome=r["episode_outcome"],
            )
            for r in obs_rows
        ]

        return SynthesisContext(
            reflections=reflections,
            observations=observations,
            last_run_at=last_run_at,
        )

    # ------------------------------------------------------------- build_prompt
    def build_prompt(
        self,
        *,
        goal: str,
        tech: str | None,
        context: SynthesisContext,
    ) -> str:
        """Render the synthesis prompt per spec §5.2.

        Deterministic in its inputs — same goal/tech/context always
        produces the same prompt. Safe to cache.
        """
        lines: list[str] = []
        lines.append(
            "You are evaluating memory consolidation for a coding project."
        )
        lines.append("")
        lines.append(f"GOAL: {goal}")
        lines.append(f"TECH: {tech if tech else '(unspecified)'}")
        lines.append("")

        # Existing reflections section.
        lines.append(
            "EXISTING REFLECTIONS (you may augment or merge these):"
        )
        if not context.reflections:
            lines.append("  (none)")
        else:
            for r in context.reflections:
                tech_str = r.tech if r.tech else "any-tech"
                lines.append(
                    f"- id={r.id} [{r.polarity}/{r.phase}/{tech_str}] "
                    f"(confidence {r.confidence}, status {r.status})"
                )
                lines.append(f"  title: {r.title}")
                lines.append(f"  use_cases: {r.use_cases}")
                lines.append(f"  hints: {r.hints}")
        lines.append("")

        # New observations section.
        lines.append(
            "NEW OBSERVATIONS since last synthesis (summarise "
            "into new reflections, augment existing, merge duplicates, "
            "or ignore):"
        )
        if not context.observations:
            lines.append("  (none)")
        else:
            for o in context.observations:
                tech_str = o.tech if o.tech else "any-tech"
                lines.append(
                    f"- id={o.id} (outcome={o.outcome}, "
                    f"component={o.component or '-'}, "
                    f"theme={o.theme or '-'}, tech={tech_str})"
                )
                lines.append(
                    f'  episode goal="{o.episode_goal or ""}" '
                    f"episode outcome={o.episode_outcome or ''}"
                )
                lines.append(f"  content: {o.content}")
        lines.append("")

        # Response-shape instructions.
        lines.append(
            "Respond ONLY with a JSON object matching this exact shape:"
        )
        lines.append("{")
        lines.append('  "new": [')
        lines.append(
            "    {"
            '"title": "...", '
            '"phase": "planning"|"implementation"|"general", '
            '"polarity": "do"|"dont"|"neutral", '
            '"use_cases": "...", '
            '"hints": ["..."], '
            '"tech": "..." or null, '
            '"confidence": 0.1..1.0, '
            '"source_observation_ids": ["..."]'
            "}"
        )
        lines.append("  ],")
        lines.append('  "augment": [')
        lines.append(
            "    {"
            '"reflection_id": "...", '
            '"add_hints": ["..."], '
            '"rewrite_use_cases": "..." or null, '
            '"confidence_delta": 0.0, '
            '"add_source_observation_ids": ["..."]'
            "}"
        )
        lines.append("  ],")
        lines.append('  "merge": [')
        lines.append(
            "    {"
            '"source_id": "...", '
            '"target_id": "...", '
            '"justification": "..."'
            "}"
        )
        lines.append("  ],")
        lines.append('  "ignore": ["observation_id", ...]')
        lines.append("}")

        return "\n".join(lines)
