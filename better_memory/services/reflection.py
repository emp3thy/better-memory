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

import json
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


class SynthesisResponseError(ValueError):
    """Raised when the LLM response is malformed, wrong-shape, or invalid."""


_VALID_PHASES = {"planning", "implementation", "general"}
_VALID_POLARITIES = {"do", "dont", "neutral"}


@dataclass(frozen=True)
class NewAction:
    title: str
    phase: str
    polarity: str
    use_cases: str
    hints: list[str]
    tech: str | None
    confidence: float
    source_observation_ids: list[str]


@dataclass(frozen=True)
class AugmentAction:
    reflection_id: str
    add_hints: list[str]
    rewrite_use_cases: str | None
    confidence_delta: float
    add_source_observation_ids: list[str]


@dataclass(frozen=True)
class MergeAction:
    source_id: str
    target_id: str
    justification: str


@dataclass(frozen=True)
class SynthesisResponse:
    new: list[NewAction]
    augment: list[AugmentAction]
    merge: list[MergeAction]
    ignore: list[str]


def _require(d: dict, key: str, kind: type, what: str) -> object:
    """Fetch ``d[key]`` and validate its type. Raise otherwise."""
    if key not in d:
        raise SynthesisResponseError(f"{what}: missing required field '{key}'")
    value = d[key]
    if not isinstance(value, kind):
        raise SynthesisResponseError(
            f"{what}.{key}: expected {kind.__name__}, got {type(value).__name__}"
        )
    return value


def _require_str(d: dict, key: str, what: str) -> str:
    v = _require(d, key, str, what)
    assert isinstance(v, str)
    return v


def _require_list_of_str(d: dict, key: str, what: str) -> list[str]:
    raw = _require(d, key, list, what)
    assert isinstance(raw, list)
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise SynthesisResponseError(
                f"{what}.{key}[{i}]: expected str, got {type(item).__name__}"
            )
        out.append(item)
    return out


def _require_number(d: dict, key: str, what: str) -> float:
    if key not in d:
        raise SynthesisResponseError(f"{what}: missing required field '{key}'")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SynthesisResponseError(
            f"{what}.{key}: expected number, got {type(v).__name__}"
        )
    return float(v)


def _parse_new(item: object) -> NewAction:
    if not isinstance(item, dict):
        raise SynthesisResponseError(
            f"new entry must be object, got {type(item).__name__}"
        )
    what = "new entry"
    phase = _require_str(item, "phase", what)
    if phase not in _VALID_PHASES:
        raise SynthesisResponseError(
            f"{what}.phase: expected one of {sorted(_VALID_PHASES)}, got {phase!r}"
        )
    polarity = _require_str(item, "polarity", what)
    if polarity not in _VALID_POLARITIES:
        raise SynthesisResponseError(
            f"{what}.polarity: expected one of {sorted(_VALID_POLARITIES)}, "
            f"got {polarity!r}"
        )
    tech_raw = item.get("tech")
    if tech_raw is not None and not isinstance(tech_raw, str):
        raise SynthesisResponseError(
            f"{what}.tech: expected str or null, got {type(tech_raw).__name__}"
        )
    return NewAction(
        title=_require_str(item, "title", what),
        phase=phase,
        polarity=polarity,
        use_cases=_require_str(item, "use_cases", what),
        hints=_require_list_of_str(item, "hints", what),
        tech=tech_raw,
        confidence=_require_number(item, "confidence", what),
        source_observation_ids=_require_list_of_str(
            item, "source_observation_ids", what
        ),
    )


def _parse_augment(item: object) -> AugmentAction:
    if not isinstance(item, dict):
        raise SynthesisResponseError(
            f"augment entry must be object, got {type(item).__name__}"
        )
    what = "augment entry"
    rewrite_raw = item.get("rewrite_use_cases")
    if rewrite_raw is not None and not isinstance(rewrite_raw, str):
        raise SynthesisResponseError(
            f"{what}.rewrite_use_cases: expected str or null, "
            f"got {type(rewrite_raw).__name__}"
        )
    return AugmentAction(
        reflection_id=_require_str(item, "reflection_id", what),
        add_hints=_require_list_of_str(item, "add_hints", what),
        rewrite_use_cases=rewrite_raw,
        confidence_delta=_require_number(item, "confidence_delta", what),
        add_source_observation_ids=_require_list_of_str(
            item, "add_source_observation_ids", what
        ),
    )


def _parse_merge(item: object) -> MergeAction:
    if not isinstance(item, dict):
        raise SynthesisResponseError(
            f"merge entry must be object, got {type(item).__name__}"
        )
    what = "merge entry"
    return MergeAction(
        source_id=_require_str(item, "source_id", what),
        target_id=_require_str(item, "target_id", what),
        justification=_require_str(item, "justification", what),
    )


class ReflectionSynthesisService:
    """Orchestrates pre-start synthesis: load, prompt, parse, apply, return.

    Connection ownership: the service writes within its own SAVEPOINT +
    commit envelope for apply methods. ``load_context`` is read-only.
    Callers must not share a connection that already has an open
    outer transaction with other services.
    """

    _SHORT_CIRCUIT_WINDOW_MINUTES = 10

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

    # ----------------------------------------------------------- parse_response
    def parse_response(self, raw: str) -> SynthesisResponse:
        """Parse and validate the LLM response JSON.

        Shape check:
        - Top level must be an object with keys ``new``, ``augment``,
          ``merge``, ``ignore`` (all arrays). Missing keys → error.
        - Each array entry must match its dataclass shape. Missing
          required fields or invalid enum values → error.
        - Extra fields at any level are silently dropped (LLMs may
          emit narrative commentary).

        Idempotency (dropping unknown observation/reflection ids)
        happens in the apply methods, not here, because it needs
        DB access.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SynthesisResponseError(f"invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise SynthesisResponseError(
                "top-level response must be a JSON object"
            )

        for key in ("new", "augment", "merge", "ignore"):
            if key not in data:
                raise SynthesisResponseError(
                    f"missing required top-level key: {key}"
                )
            if not isinstance(data[key], list):
                raise SynthesisResponseError(
                    f"top-level key {key} must be a list"
                )

        new = [_parse_new(item) for item in data["new"]]
        augment = [_parse_augment(item) for item in data["augment"]]
        merge = [_parse_merge(item) for item in data["merge"]]
        ignore: list[str] = []
        for item in data["ignore"]:
            if not isinstance(item, str):
                raise SynthesisResponseError(
                    f"ignore entry must be a string, got {type(item).__name__}"
                )
            ignore.append(item)

        return SynthesisResponse(
            new=new, augment=augment, merge=merge, ignore=ignore
        )

    # ---------------------------------------------------------------- _apply_new
    def _apply_new(
        self, actions: list[NewAction], *, project: str
    ) -> None:
        """Insert new reflections + their source links + consume observations.

        Idempotency: observation ids in ``source_observation_ids`` that
        don't exist in the DB are dropped. Entries whose entire source
        list turns out to be invalid are skipped silently.
        """
        from uuid import uuid4

        now = self._clock().isoformat()
        for action in actions:
            valid_sources = self._filter_existing_observations(
                action.source_observation_ids
            )
            if not valid_sources:
                continue

            confidence = max(0.1, min(1.0, action.confidence))
            reflection_id = uuid4().hex

            self._conn.execute(
                """
                INSERT INTO reflections (
                    id, title, project, tech, phase, polarity, use_cases,
                    hints, confidence, status, evidence_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?)
                """,
                (
                    reflection_id, action.title, project, action.tech,
                    action.phase, action.polarity, action.use_cases,
                    json.dumps(action.hints), confidence,
                    len(valid_sources), now, now,
                ),
            )
            for obs_id in valid_sources:
                self._conn.execute(
                    "INSERT INTO reflection_sources "
                    "(reflection_id, observation_id) VALUES (?, ?)",
                    (reflection_id, obs_id),
                )
            placeholders = ",".join("?" * len(valid_sources))
            self._conn.execute(
                f"UPDATE observations SET status = 'consumed_into_reflection' "
                f"WHERE id IN ({placeholders})",
                valid_sources,
            )

    def _filter_existing_observations(
        self, ids: list[str]
    ) -> list[str]:
        """Return the subset of ``ids`` that exist in ``observations``."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id FROM observations WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        existing = {r["id"] for r in rows}
        # Preserve original order for determinism.
        return [i for i in ids if i in existing]

    # ----------------------------------------------------------- _apply_augment
    def _apply_augment(self, actions: list[AugmentAction]) -> None:
        """Apply augment actions: append hints, rewrite use_cases, bump
        confidence, link new sources, recompute evidence count.

        Idempotency:
        - Unknown ``reflection_id`` → entry skipped.
        - Reflection with status in ``{retired, superseded}`` →
          entry skipped (cannot modify a retired lesson).
        - ``add_source_observation_ids`` filtered to existing obs;
          ``INSERT OR IGNORE`` dedupes against existing source rows.
        """
        now = self._clock().isoformat()
        for action in actions:
            row = self._conn.execute(
                "SELECT hints, confidence, status FROM reflections "
                "WHERE id = ?",
                (action.reflection_id,),
            ).fetchone()
            if row is None:
                continue
            if row["status"] in ("retired", "superseded"):
                continue

            # Append + dedup hints, preserving order.
            existing_hints = json.loads(row["hints"])
            merged_hints: list[str] = list(existing_hints)
            for h in action.add_hints:
                if h not in merged_hints:
                    merged_hints.append(h)

            # Clamp new confidence.
            new_confidence = max(
                0.1, min(1.0, row["confidence"] + action.confidence_delta)
            )

            # Add source links, filtering to existing observations.
            valid_sources = self._filter_existing_observations(
                action.add_source_observation_ids
            )
            for obs_id in valid_sources:
                self._conn.execute(
                    "INSERT OR IGNORE INTO reflection_sources "
                    "(reflection_id, observation_id) VALUES (?, ?)",
                    (action.reflection_id, obs_id),
                )

            # Mark added observations consumed.
            if valid_sources:
                placeholders = ",".join("?" * len(valid_sources))
                self._conn.execute(
                    f"UPDATE observations "
                    f"SET status = 'consumed_into_reflection' "
                    f"WHERE id IN ({placeholders})",
                    valid_sources,
                )

            # Recompute evidence_count from actual source count.
            new_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM reflection_sources "
                "WHERE reflection_id = ?",
                (action.reflection_id,),
            ).fetchone()["c"]

            # Update the reflection. Branch on rewrite_use_cases — two
            # explicit UPDATE statements are clearer and less error-prone
            # than a dynamically-assembled SET clause.
            if action.rewrite_use_cases is not None:
                self._conn.execute(
                    """
                    UPDATE reflections
                       SET use_cases = ?, hints = ?, confidence = ?,
                           evidence_count = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        action.rewrite_use_cases,
                        json.dumps(merged_hints),
                        new_confidence,
                        new_count,
                        now,
                        action.reflection_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE reflections
                       SET hints = ?, confidence = ?,
                           evidence_count = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        json.dumps(merged_hints),
                        new_confidence,
                        new_count,
                        now,
                        action.reflection_id,
                    ),
                )

    # ------------------------------------------------------------- _apply_merge
    def _apply_merge(self, actions: list[MergeAction]) -> None:
        """Merge source reflection into target, dropping unknown ids.

        Semantics per spec §5:
        - Move source's ``reflection_sources`` rows into target
          (INSERT OR IGNORE dedups against existing target sources).
        - DELETE source's ``reflection_sources`` rows.
        - Recompute target.evidence_count from actual COUNT(*).
        - Set source.status='superseded', superseded_by=target.
        - Bump both updated_at.

        Idempotency:
        - Unknown source_id or target_id → entry skipped.
        - Source with status in ``{retired, superseded}`` → skipped.
        - source_id == target_id → skipped (would DELETE the target's
          sources and supersede the reflection in place; double damage).
        """
        now = self._clock().isoformat()
        for action in actions:
            # Reject self-merge: same id on both sides would (a) DELETE the
            # reflection's own source rows because the INSERT OR IGNORE from
            # self is a no-op, and (b) mark the reflection superseded against
            # itself. Neither is a valid user intent.
            if action.source_id == action.target_id:
                continue

            src = self._conn.execute(
                "SELECT status FROM reflections WHERE id = ?",
                (action.source_id,),
            ).fetchone()
            if src is None:
                continue
            if src["status"] in ("retired", "superseded"):
                continue

            tgt = self._conn.execute(
                "SELECT id FROM reflections WHERE id = ?",
                (action.target_id,),
            ).fetchone()
            if tgt is None:
                continue

            # Move source's sources into target.
            self._conn.execute(
                "INSERT OR IGNORE INTO reflection_sources "
                "(reflection_id, observation_id) "
                "SELECT ?, observation_id FROM reflection_sources "
                "WHERE reflection_id = ?",
                (action.target_id, action.source_id),
            )
            # Delete source's source rows.
            self._conn.execute(
                "DELETE FROM reflection_sources WHERE reflection_id = ?",
                (action.source_id,),
            )
            # Recompute target evidence count.
            new_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM reflection_sources "
                "WHERE reflection_id = ?",
                (action.target_id,),
            ).fetchone()["c"]

            # Update source + target.
            self._conn.execute(
                "UPDATE reflections "
                "SET status = 'superseded', superseded_by = ?, updated_at = ? "
                "WHERE id = ?",
                (action.target_id, now, action.source_id),
            )
            self._conn.execute(
                "UPDATE reflections "
                "SET evidence_count = ?, updated_at = ? WHERE id = ?",
                (new_count, now, action.target_id),
            )

    # ------------------------------------------------------------ _apply_ignore
    def _apply_ignore(self, observation_ids: list[str]) -> None:
        """Mark observations as consumed_without_reflection.

        Idempotency: ids that don't exist are silently dropped by the
        IN filter.
        """
        valid = self._filter_existing_observations(observation_ids)
        if not valid:
            return
        placeholders = ",".join("?" * len(valid))
        self._conn.execute(
            f"UPDATE observations SET status = 'consumed_without_reflection' "
            f"WHERE id IN ({placeholders})",
            valid,
        )

    # --------------------------------------------------------- _upsert_watermark
    def _upsert_watermark(
        self, *, project: str, tech: str | None, goal: str
    ) -> None:
        """Record that synthesis just ran for (project, tech) with the given goal."""
        tech_key = tech if tech is not None else ""
        now = self._clock().isoformat()
        self._conn.execute(
            """
            INSERT INTO synthesis_runs (project, tech, last_run_at, last_goal)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project, tech) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                last_goal   = excluded.last_goal
            """,
            (project, tech_key, now, goal),
        )

    # ----------------------------------------------------- _should_short_circuit
    def _should_short_circuit(
        self, *, project: str, tech: str | None, goal: str
    ) -> bool:
        """Return True if this call exactly matches a recent synthesis.

        Conditions (all must hold):
        1. A synthesis_runs row exists for (project, tech).
        2. last_goal == goal.
        3. now - last_run_at < SHORT_CIRCUIT_WINDOW_MINUTES.
        4. No observations have been written after last_run_at for (project, tech).
        """
        from datetime import timedelta
        tech_key = tech if tech is not None else ""
        row = self._conn.execute(
            "SELECT last_run_at, last_goal FROM synthesis_runs "
            "WHERE project = ? AND tech = ?",
            (project, tech_key),
        ).fetchone()
        if row is None:
            return False
        if row["last_goal"] != goal:
            return False

        # Within the short-circuit window?
        try:
            last_run_dt = datetime.fromisoformat(row["last_run_at"])
        except (TypeError, ValueError):
            return False
        if last_run_dt.tzinfo is None:
            last_run_dt = last_run_dt.replace(tzinfo=UTC)
        elapsed = self._clock() - last_run_dt
        if elapsed >= timedelta(minutes=self._SHORT_CIRCUIT_WINDOW_MINUTES):
            return False

        # Any new observations since last_run_at?
        # Use the same outcome filter as load_context to stay consistent.
        new_count = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM observations o
            JOIN episodes e ON e.id = o.episode_id
            WHERE o.project = ?
              AND e.outcome IN ('success', 'partial', 'abandoned')
              AND o.created_at > ?
            """,
            (project, row["last_run_at"]),
        ).fetchone()["c"]
        return new_count == 0

    # ----------------------------------------------------------------- synthesize
    async def synthesize(
        self, *, goal: str, tech: str | None, project: str
    ) -> dict[str, list[dict]]:
        """End-to-end synthesis. Short-circuits on same-goal resume.

        Short-circuit conditions (spec §5):
        - Prior synthesis run exists for (project, tech) with last_goal == goal.
        - Now - last_run_at < 10 minutes.
        - No new observations since last_run_at.

        When short-circuiting, returns the current reflection buckets
        without calling the LLM.
        """
        if self._should_short_circuit(project=project, tech=tech, goal=goal):
            return self._bucketed_reflections(project=project, tech=tech)

        context = self.load_context(project=project, tech=tech)
        prompt = self.build_prompt(goal=goal, tech=tech, context=context)
        raw = await self._chat.complete(prompt)
        response = self.parse_response(raw)

        conn = self._conn
        conn.execute("SAVEPOINT reflection_synthesize")
        try:
            self._apply_new(response.new, project=project)
            self._apply_augment(response.augment)
            self._apply_merge(response.merge)
            self._apply_ignore(response.ignore)
            self._upsert_watermark(project=project, tech=tech, goal=goal)
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT reflection_synthesize")
            conn.execute("RELEASE SAVEPOINT reflection_synthesize")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT reflection_synthesize")
        conn.commit()

        return self._bucketed_reflections(project=project, tech=tech)

    def _bucketed_reflections(
        self, *, project: str, tech: str | None
    ) -> dict[str, list[dict]]:
        """Return current reflections for (project, tech?) bucketed by polarity.

        Ordered by confidence DESC, updated_at DESC per spec §5.5.
        Includes pending_review + confirmed; retired/superseded excluded.
        """
        if tech is None:
            rows = self._conn.execute(
                """
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count
                FROM reflections
                WHERE project = ?
                  AND status IN ('pending_review', 'confirmed')
                ORDER BY confidence DESC, updated_at DESC
                """,
                (project,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count
                FROM reflections
                WHERE project = ?
                  AND status IN ('pending_review', 'confirmed')
                  AND (tech = ? OR tech IS NULL)
                ORDER BY confidence DESC, updated_at DESC
                """,
                (project, tech),
            ).fetchall()

        buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
        for r in rows:
            entry = {
                "id": r["id"],
                "title": r["title"],
                "phase": r["phase"],
                "use_cases": r["use_cases"],
                "hints": json.loads(r["hints"]),
                "confidence": r["confidence"],
                "tech": r["tech"],
                "evidence_count": r["evidence_count"],
            }
            buckets[r["polarity"]].append(entry)
        return buckets

    # --------------------------------------------------------- retrieve_reflections
    def retrieve_reflections(
        self,
        *,
        project: str,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        limit_per_bucket: int = 20,
    ) -> dict[str, list[dict]]:
        """Return reflections bucketed by polarity, ordered by confidence DESC.

        Filters:
        - ``project``: required.
        - ``tech``: matches same-tech rows OR cross-tech (tech IS NULL) rows.
        - ``phase``: optional exact match.
        - ``polarity``: optional exact match; non-matching buckets remain empty.
        - ``limit_per_bucket``: cap each polarity bucket. Default 20 per spec §7.

        Excludes retired and superseded reflections. Includes pending_review
        + confirmed.
        """
        clauses = [
            "project = ?",
            "status IN ('pending_review', 'confirmed')",
        ]
        params: list[object] = [project]
        if tech is not None:
            clauses.append("(tech = ? OR tech IS NULL)")
            params.append(tech)
        if phase is not None:
            clauses.append("phase = ?")
            params.append(phase)
        if polarity is not None:
            clauses.append("polarity = ?")
            params.append(polarity)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"""
            SELECT id, title, phase, polarity, use_cases, hints,
                   confidence, tech, evidence_count
            FROM reflections
            WHERE {where}
            ORDER BY confidence DESC, updated_at DESC
            """,
            params,
        ).fetchall()

        buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
        for r in rows:
            bucket = buckets[r["polarity"]]
            if len(bucket) >= limit_per_bucket:
                continue
            bucket.append({
                "id": r["id"],
                "title": r["title"],
                "phase": r["phase"],
                "use_cases": r["use_cases"],
                "hints": json.loads(r["hints"]),
                "confidence": r["confidence"],
                "tech": r["tech"],
                "evidence_count": r["evidence_count"],
            })
        return buckets
