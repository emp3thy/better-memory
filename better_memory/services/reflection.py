"""Reflection synthesis service.

Orchestrates per-episode synthesis: for each closed-but-unsynthesized
episode, loads its observations and tech-filtered reflections, calls the
LLM once, and applies new/augment/merge/ignore actions atomically inside
a per-episode SAVEPOINT.

This module provides:
- Typed read models for LLM consumption (:class:`ReflectionForPrompt`,
  :class:`ObservationForPrompt`).
- Per-episode types: :class:`EpisodeForPrompt`, :class:`EpisodeContext`,
  :class:`EpisodeQueueCounts`, :class:`SynthesisStep`.
- :class:`ReflectionSynthesisService` with ``synthesize_next`` as the
  primary entry point.

Design notes:
- The service owns writes within its own transaction envelope
  (SAVEPOINT + commit), matching the convention used by
  ObservationService and EpisodeService.
- The LLM client is injected via a ``ChatCompleter`` Protocol so
  tests can swap :class:`better_memory.llm.fake.FakeChat` in
  without touching Ollama.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from better_memory import _diag
from better_memory._common import default_clock, env_session_id
from better_memory.search.query import sanitize_fts5_query
from better_memory.services.scoring import wilson_lower_bound

# Dropped from relevance queries. These survive `sanitize_fts5_query` and the
# >2-char filter, but appear in so many reflections that OR-matching on them
# promotes generic rows over the ones that actually describe the task.
_QUERY_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "what", "how", "why", "does", "did", "are", "was", "were", "have", "has",
    "had", "not", "any", "all", "can", "should", "would", "could", "will",
    "about", "before", "after", "then", "than", "there", "their", "them",
    "you", "your", "its", "our", "out", "use", "using", "used", "get",
    "make", "made", "want", "need", "like", "just", "some", "which", "who",
    "where", "here", "over", "under", "each", "other", "same", "such",
})


def _later_ts(a: str | None, b: str | None) -> str | None:
    """Return the later of two ISO-8601 timestamps, treating NULL as -inf.

    ISO-8601 strings with consistent format sort correctly as strings,
    so a plain max() suffices once NULL is handled.
    """
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


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
    """Read model for an observation, joined with episode context."""

    id: str
    content: str
    outcome: str
    component: str | None
    theme: str | None
    tech: str | None
    created_at: str
    episode_goal: str | None
    episode_outcome: str | None
    status: str = "active"


@dataclass(frozen=True)
class EpisodeForPrompt:
    """Read model for one episode, the unit of per-episode synthesis."""

    id: str
    project: str  # populated from the row by _pick_oldest_pending; lets _load_episode_context query reflections without a subquery
    goal: str | None  # episodes.goal is nullable (e.g. background episodes from session_start markers)
    tech: str | None
    outcome: str


@dataclass(frozen=True)
class EpisodeContext:
    """Inputs to a per-episode synthesis prompt."""

    episode: EpisodeForPrompt
    observations: list[ObservationForPrompt]   # ALL observations, regardless of status
    reflections: list[ReflectionForPrompt]     # tech-filtered: tech == episode.tech OR tech IS NULL


@dataclass(frozen=True)
class EpisodeQueueCounts:
    """Single-source-of-truth queue counts, computed from one connection."""

    done: int
    pending: int
    in_cooldown: int

    @property
    def total(self) -> int:
        return self.done + self.pending + self.in_cooldown


@dataclass(frozen=True)
class SynthesisStep:
    """Result of one synthesize_next call."""

    processed: bool                       # False ⇔ no pending episode (queue empty or all in cooldown)
    episode_id: str | None                # which episode was processed (None if processed=False)
    counts: dict[str, int]                # this-step counters: created/augmented/merged/ignored/auto_ignored
    queue: EpisodeQueueCounts             # post-step queue snapshot, single connection, single moment
    failure: str | None                   # set if LLM-class error was caught


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


#: A memory with fewer than this many rated exposures is "untested": its
#: Wilson score is statistically meaningless, so it competes for the
#: reserved exploration slot instead of the proven slots.
EXPLORATION_RATED_FLOOR = 3


def _wilson_rated(row) -> int:
    """Total rated exposures backing this memory's score."""
    return (row["useful_count"] + row["times_overlooked"]
            + row["times_ignored"])


def _wilson_score(row) -> float:
    positive = row["useful_count"] + row["times_overlooked"]
    return wilson_lower_bound(positive, _wilson_rated(row))


class ReflectionSynthesisService:
    """Orchestrates pre-start synthesis: load, prompt, parse, apply, return.

    Connection ownership: the service writes within its own SAVEPOINT +
    commit envelope for apply methods.
    Callers must not share a connection that already has an open
    outer transaction with other services.
    """

    _ZERO_COUNTS: dict[str, int] = {
        "created": 0, "augmented": 0, "merged": 0,
        "ignored": 0, "auto_ignored": 0,
    }

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or default_clock
    @staticmethod
    def _normalize_tech(tech: str | None) -> str | None:
        # Mirror EpisodeService.start_foreground / ObservationService.create
        # so case-mismatched tech doesn't silently miss on retrieval.
        return tech.lower() if tech else None

    # ------------------------------------------------------- _pick_oldest_pending
    def _pick_oldest_pending(
        self, project: str
    ) -> EpisodeForPrompt | None:
        """Return the oldest closed-but-unsynthesized episode for project.

        Excludes:
        - episodes still open (outcome IS NULL)
        - episodes already synthesized (synthesized_at IS NOT NULL)
        - episodes in the 300s failure cooldown window

        Returns None when nothing is eligible (queue is empty or all candidates
        are in cooldown).
        """
        cooldown_cutoff = (self._clock() - timedelta(seconds=300)).isoformat()
        row = self._conn.execute(
            """
            SELECT id, project, goal, tech, outcome
              FROM episodes
             WHERE project = ?
               AND outcome IS NOT NULL
               AND synthesized_at IS NULL
               AND (synth_failed_at IS NULL
                    OR synth_failed_at < ?)
             ORDER BY ended_at ASC
             LIMIT 1
            """,
            (project, cooldown_cutoff),
        ).fetchone()
        if row is None:
            return None
        return EpisodeForPrompt(
            id=row["id"],
            project=row["project"],
            goal=row["goal"],
            tech=row["tech"],
            outcome=row["outcome"],
        )

    # --------------------------------------------------- _load_episode_context
    def _load_episode_context(
        self, episode: EpisodeForPrompt
    ) -> EpisodeContext:
        """Load all observations for the episode + tech-filtered reflections."""
        obs_rows = self._conn.execute(
            """
            SELECT id, content, outcome, component, theme, tech,
                   created_at, status
              FROM observations
             WHERE episode_id = ?
             ORDER BY created_at ASC, rowid ASC
            """,
            (episode.id,),
        ).fetchall()
        observations = [
            ObservationForPrompt(
                id=r["id"], content=r["content"], outcome=r["outcome"],
                component=r["component"], theme=r["theme"], tech=r["tech"],
                created_at=r["created_at"],
                episode_goal=episode.goal,
                episode_outcome=episode.outcome,
                status=r["status"],
            )
            for r in obs_rows
        ]

        tech = self._normalize_tech(episode.tech)
        if tech is None:
            refl_rows = self._conn.execute(
                """
                SELECT id, title, tech, phase, polarity, use_cases, hints,
                       confidence, status
                  FROM reflections
                 WHERE (project = ? OR scope = 'general')
                   AND status IN ('pending_review', 'confirmed')
                 ORDER BY useful_count DESC, confidence DESC, updated_at DESC
                """,
                (episode.project,),
            ).fetchall()
        else:
            refl_rows = self._conn.execute(
                """
                SELECT id, title, tech, phase, polarity, use_cases, hints,
                       confidence, status
                  FROM reflections
                 WHERE (project = ? OR scope = 'general')
                   AND status IN ('pending_review', 'confirmed')
                   AND (tech = ? OR tech IS NULL)
                 ORDER BY useful_count DESC, confidence DESC, updated_at DESC
                """,
                (episode.project, tech),
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
        return EpisodeContext(
            episode=episode,
            observations=observations,
            reflections=reflections,
        )

    # ------------------------------------------------- _build_episode_prompt
    def _build_episode_prompt(self, ctx: EpisodeContext) -> str:
        """Render the per-episode synthesis prompt.

        Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md
        "Per-episode prompt template".

        Deterministic in its inputs.
        """
        lines: list[str] = []
        lines.append(
            "You are evaluating one coding episode for memory consolidation."
        )
        lines.append("")
        lines.append("EPISODE")
        lines.append(
            f"  goal:    {ctx.episode.goal if ctx.episode.goal else '(unspecified)'}"
        )
        lines.append(
            f"  tech:    {ctx.episode.tech if ctx.episode.tech else '(unspecified)'}"
        )
        lines.append(f"  outcome: {ctx.episode.outcome}")
        lines.append("")

        lines.append(
            "OBSERVATIONS from this episode (all of them, regardless of prior status):"
        )
        if not ctx.observations:
            lines.append("  (none)")
        else:
            for o in ctx.observations:
                tech_str = o.tech if o.tech else "any-tech"
                lines.append(
                    f"- id={o.id} (outcome={o.outcome}, "
                    f"component={o.component or '-'}, "
                    f"theme={o.theme or '-'}, tech={tech_str})"
                )
                lines.append(f"  status: {o.status}")
                lines.append(f"  content: {o.content}")
        lines.append("")

        lines.append(
            "EXISTING REFLECTIONS for this tech "
            "(you may augment, merge, leave alone):"
        )
        if not ctx.reflections:
            lines.append("  (none)")
        else:
            for r in ctx.reflections:
                tech_str = r.tech if r.tech else "any-tech"
                lines.append(
                    f"- id={r.id} [{r.polarity}/{r.phase}/{tech_str}] "
                    f"(confidence {r.confidence}, status {r.status})"
                )
                lines.append(f"  title: {r.title}")
                lines.append(f"  use_cases: {r.use_cases}")
                lines.append(f"  hints: {r.hints}")
        lines.append("")

        lines.append(
            "Decide what to do with this episode's observations. "
            "Respond ONLY with one JSON object — no prose, no commentary."
        )
        lines.append("")
        lines.append("RULES:")
        lines.append(
            '- The four top-level keys "new", "augment", "merge", "ignore" '
            "are ALL REQUIRED. Use [] for any category that has no real "
            "entries. DO NOT invent or fabricate entries to mimic the "
            "example below — the example is illustrative only."
        )
        lines.append(
            '- "new" entries: ONLY add a reflection if the observation(s) '
            "express a generalizable lesson worth surfacing in future "
            "sessions. If nothing in this episode rises to that bar, use [] "
            'and put the observation ids in "ignore" instead.'
        )
        lines.append(
            '- "augment" entries: ONLY use when an EXISTING reflection '
            "(from the EXISTING REFLECTIONS section above) gains new "
            "evidence from this episode's observations. If no existing "
            "reflection applies, use []."
        )
        lines.append(
            '- "merge" entries: combine TWO existing reflection ids '
            "(both must appear in the EXISTING REFLECTIONS section "
            "above) that express SUBSTANTIALLY the same lesson — "
            "paraphrased, differently scoped, or differently worded "
            "variants of the same underlying insight. PREFER to merge "
            "when you can do so with high confidence: merging combines "
            "evidence and rating signals (useful_count, "
            "times_overlooked, times_misled, and last_*_at timestamps) "
            "from the source onto the target, strengthening the "
            "surviving reflection and reducing duplicate noise at "
            "retrieval. If no two reflections meet that bar, use []. "
            "NEVER emit a merge entry with null, empty, or invented ids."
        )
        lines.append(
            '- For each entry in "new", ALL FIELDS ARE REQUIRED: '
            "title, phase, polarity, use_cases, hints, tech, "
            "confidence, source_observation_ids. Set tech to null if "
            "not language-specific. Do not omit any field."
        )
        lines.append(
            '- For each entry in "augment", ALL FIELDS ARE REQUIRED: '
            "reflection_id, add_hints, rewrite_use_cases, "
            "confidence_delta, add_source_observation_ids. Set "
            "rewrite_use_cases to null to leave the existing text "
            "unchanged. Do not omit any field."
        )
        lines.append(
            '- For each entry in "merge", ALL FIELDS ARE REQUIRED: '
            "source_id, target_id, justification. All three must be "
            "non-null strings."
        )
        lines.append(
            "- source_observation_ids and add_source_observation_ids "
            "must contain the actual observation ids shown above (the "
            "values after `id=`), NOT placeholders."
        )
        lines.append(
            '- phase must be one of: "planning", "implementation", "general".'
        )
        lines.append(
            '- polarity must be one of: "do", "dont", "neutral".'
        )
        lines.append("- confidence is a number between 0.1 and 1.0.")
        lines.append("")
        lines.append(
            "EXAMPLE response (illustrative — use the actual ids "
            "from THIS episode, not these placeholders):"
        )
        lines.append("{")
        lines.append('  "new": [')
        lines.append("    {")
        lines.append('      "title": "Prefer pathlib over os.path",')
        lines.append('      "phase": "implementation",')
        lines.append('      "polarity": "do",')
        lines.append(
            '      "use_cases": "When manipulating filesystem paths in Python",'
        )
        lines.append(
            '      "hints": ["pathlib.Path is cross-platform", '
            '"supports / for joining"],'
        )
        lines.append('      "tech": "python",')
        lines.append('      "confidence": 0.7,')
        lines.append('      "source_observation_ids": ["o-abc123"]')
        lines.append("    }")
        lines.append("  ],")
        lines.append('  "augment": [')
        lines.append("    {")
        lines.append('      "reflection_id": "r-existing",')
        lines.append(
            '      "add_hints": ["new evidence: also helps with relative paths"],'
        )
        lines.append('      "rewrite_use_cases": null,')
        lines.append('      "confidence_delta": 0.1,')
        lines.append('      "add_source_observation_ids": ["o-def456"]')
        lines.append("    }")
        lines.append("  ],")
        lines.append('  "merge": [],')
        lines.append('  "ignore": ["o-noise789"]')
        lines.append("}")
        lines.append("")
        lines.append(
            '(Note: in this example "merge" is [] because no two '
            "existing reflections need combining. That is the common "
            'case. If you also have nothing to add or augment, all '
            'four arrays may be []. That is a valid response.)'
        )
        lines.append("")
        lines.append("Now produce the JSON for THIS episode:")

        return "\n".join(lines)

    # --------------------------------------------------------- _mark_synthesized
    def _mark_synthesized(self, episode_id: str) -> None:
        """Set synthesized_at = clock() for the given episode."""
        self._conn.execute(
            "UPDATE episodes SET synthesized_at = ? WHERE id = ?",
            (self._clock().isoformat(), episode_id),
        )

    # ------------------------------------------------------- _read_queue_counts
    def _read_queue_counts(self, project: str) -> EpisodeQueueCounts:
        """Snapshot the per-project queue state in one statement.

        Single-source-of-truth for the route's banner — eliminates the race
        that would exist if `done` and `pending` were read from different
        connections at different times.
        """
        # Compute the cooldown cutoff via the injected clock so tests with
        # fixed_clock get deterministic semantics matching _pick_oldest_pending.
        cooldown_cutoff = (self._clock() - timedelta(seconds=300)).isoformat()
        row = self._conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE synthesized_at IS NOT NULL)
                AS done,
              COUNT(*) FILTER (WHERE synthesized_at IS NULL
                                 AND (synth_failed_at IS NULL
                                      OR synth_failed_at < ?))
                AS pending,
              COUNT(*) FILTER (WHERE synthesized_at IS NULL
                                 AND synth_failed_at >= ?)
                AS in_cooldown
            FROM episodes
            WHERE project = ? AND outcome IS NOT NULL
            """,
            (cooldown_cutoff, cooldown_cutoff, project),
        ).fetchone()
        return EpisodeQueueCounts(
            done=row["done"], pending=row["pending"], in_cooldown=row["in_cooldown"],
        )

    # ----------------------------------------------------------- parse_response
    def parse_response(self, raw: str) -> SynthesisResponse:
        """Parse and validate the LLM response JSON string.

        Thin wrapper around :meth:`parse_response_dict` that handles the
        ``json.loads`` step. Use :meth:`parse_response_dict` directly when
        the caller already has a parsed dict (e.g. an MCP handler whose
        framework decoded the JSON before dispatch — avoids a redundant
        encode/decode round-trip).
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SynthesisResponseError(f"invalid JSON: {exc}") from exc
        return self.parse_response_dict(data)

    # ------------------------------------------------------ parse_response_dict
    def parse_response_dict(self, data: object) -> SynthesisResponse:
        """Validate an already-parsed decision dict and return a SynthesisResponse.

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

        for action in actions:
            # Stamp once per iteration so each new reflection's
            # created_at / updated_at and the consume UPDATE's
            # status_changed_at all reflect this iteration — not a
            # leftover from a prior iteration.
            now = self._clock().isoformat()
            valid_sources = self._filter_existing_observations(
                action.source_observation_ids
            )
            if not valid_sources:
                continue

            confidence = max(0.1, min(1.0, action.confidence))
            reflection_id = uuid4().hex
            scope = self._derive_new_reflection_scope(valid_sources)

            self._conn.execute(
                """
                INSERT INTO reflections (
                    id, title, project, tech, phase, polarity, use_cases,
                    hints, confidence, status, evidence_count,
                    created_at, updated_at, scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)
                """,
                (
                    reflection_id, action.title, project,
                    self._normalize_tech(action.tech),
                    action.phase, action.polarity, action.use_cases,
                    json.dumps(action.hints), confidence,
                    len(valid_sources), now, now, scope,
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
                f"UPDATE observations "
                f"SET status = 'consumed_into_reflection', status_changed_at = ? "
                f"WHERE id IN ({placeholders})",
                [now, *valid_sources],
            )

    def _derive_new_reflection_scope(
        self, source_obs_ids: list[str]
    ) -> str:
        """Return 'general' iff every source observation has scope='general'.

        Empty source list defaults to 'project' (defensive — _apply_new
        already filters out new actions with no valid sources).
        """
        if not source_obs_ids:
            return "project"
        placeholders = ",".join("?" * len(source_obs_ids))
        rows = self._conn.execute(
            f"SELECT scope FROM observations WHERE id IN ({placeholders})",
            source_obs_ids,
        ).fetchall()
        if not rows:
            return "project"
        return "general" if all(r["scope"] == "general" for r in rows) else "project"

    def _filter_existing_observations(
        self, ids: list[str]
    ) -> list[str]:
        """Return the subset of ``ids`` that exist AND have status='active'.

        The status guard prevents apply methods from de-archiving an
        observation if the LLM hallucinates an archived id (or if a
        pre-existing archived row would otherwise be flipped back to
        a consumed_* status by the UPDATE).
        """
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id FROM observations "
            f"WHERE id IN ({placeholders}) AND status = 'active'",
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
        for action in actions:
            # Stamp once per iteration so each reflection's updated_at
            # reflects the time its UPDATE actually executed — not a
            # leftover from a prior iteration.
            now = self._clock().isoformat()
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
                    f"SET status = 'consumed_into_reflection', status_changed_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now, *valid_sources],
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
        - Accumulate source's rating counters onto target:
          useful_count, times_misled, times_overlooked are summed;
          last_useful_at, last_misled_at, last_overlooked_at take the
          later of the two timestamps. Source counters are left in
          place but become inert because source.status='superseded'
          excludes the row from retrieval.
        - Set source.status='superseded', superseded_by=target.
        - Bump both updated_at.

        Idempotency:
        - Unknown source_id or target_id → entry skipped.
        - Source with status in ``{retired, superseded}`` → skipped.
        - source_id == target_id → skipped (would DELETE the target's
          sources and supersede the reflection in place; double damage).
        """
        for action in actions:
            # Stamp once per iteration so each merge's UPDATE timestamp
            # reflects the time it actually executed — not the pre-loop
            # value carried over after a skipped iteration.
            now = self._clock().isoformat()
            # Reject self-merge: same id on both sides would (a) DELETE the
            # reflection's own source rows because the INSERT OR IGNORE from
            # self is a no-op, and (b) mark the reflection superseded against
            # itself. Neither is a valid user intent.
            if action.source_id == action.target_id:
                continue

            src = self._conn.execute(
                "SELECT status, useful_count, last_useful_at, "
                "       times_misled, last_misled_at, "
                "       times_overlooked, last_overlooked_at "
                "FROM reflections WHERE id = ?",
                (action.source_id,),
            ).fetchone()
            if src is None:
                continue
            if src["status"] in ("retired", "superseded"):
                continue

            tgt = self._conn.execute(
                "SELECT useful_count, last_useful_at, "
                "       times_misled, last_misled_at, "
                "       times_overlooked, last_overlooked_at "
                "FROM reflections WHERE id = ?",
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

            # Accumulate rating counters: sum hit counts, take the later
            # of each last_*_at timestamp. _later_ts handles either side
            # being NULL.
            new_useful = (tgt["useful_count"] or 0) + (src["useful_count"] or 0)
            new_misled = (tgt["times_misled"] or 0) + (src["times_misled"] or 0)
            new_overlooked = (
                (tgt["times_overlooked"] or 0)
                + (src["times_overlooked"] or 0)
            )
            new_last_useful = _later_ts(
                tgt["last_useful_at"], src["last_useful_at"]
            )
            new_last_misled = _later_ts(
                tgt["last_misled_at"], src["last_misled_at"]
            )
            new_last_overlooked = _later_ts(
                tgt["last_overlooked_at"], src["last_overlooked_at"]
            )

            # Update source + target.
            self._conn.execute(
                "UPDATE reflections "
                "SET status = 'superseded', superseded_by = ?, updated_at = ? "
                "WHERE id = ?",
                (action.target_id, now, action.source_id),
            )
            self._conn.execute(
                "UPDATE reflections "
                "SET evidence_count = ?, "
                "    useful_count = ?, last_useful_at = ?, "
                "    times_misled = ?, last_misled_at = ?, "
                "    times_overlooked = ?, last_overlooked_at = ?, "
                "    updated_at = ? "
                "WHERE id = ?",
                (
                    new_count,
                    new_useful, new_last_useful,
                    new_misled, new_last_misled,
                    new_overlooked, new_last_overlooked,
                    now,
                    action.target_id,
                ),
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
        now = self._clock().isoformat()
        placeholders = ",".join("?" * len(valid))
        self._conn.execute(
            f"UPDATE observations "
            f"SET status = 'consumed_without_reflection', status_changed_at = ? "
            f"WHERE id IN ({placeholders})",
            [now, *valid],
        )

    # ------------------------------------------------------- _auto_ignore_unused
    def _auto_ignore_unused(self, observation_ids: list[str]) -> int:
        """Mark input batch observations still 'active' as consumed_without_reflection.

        The watermark advances past every fed-to-LLM observation regardless
        of whether the LLM acted on it. Without this sweep, observations the
        LLM ignored (and didn't bother to list in its `ignore` array) would
        stay status='active' yet never reappear in any future synthesis —
        stranded in the active pool but invisible to consolidation.

        Bounded to ``observation_ids`` (the batch we fed) so observations
        written concurrently during the synthesize() call are not affected.
        Returns the rowcount of newly-flipped rows.
        """
        if not observation_ids:
            return 0
        now = self._clock().isoformat()
        placeholders = ",".join("?" * len(observation_ids))
        cur = self._conn.execute(
            f"UPDATE observations "
            f"SET status = 'consumed_without_reflection', status_changed_at = ? "
            f"WHERE id IN ({placeholders}) AND status = 'active'",
            [now, *observation_ids],
        )
        return cur.rowcount or 0

    # ------------------------------------------------- get_next_pending_context
    def get_next_pending_context(
        self, *, project: str
    ) -> EpisodeContext | None:
        """Return the next pending episode's full context, or None when empty.

        The IDE-driving LLM consumes this, produces a decision JSON, and
        submits it back via :meth:`apply_decision`. The two-call shape
        replaces the old ``synthesize_next`` (which embedded an Ollama
        client call between load and apply).
        """
        episode = self._pick_oldest_pending(project)
        if episode is None:
            return None
        return self._load_episode_context(episode)

    # ----------------------------------------------------------- apply_decision
    def apply_decision(
        self,
        *,
        episode_id: str,
        response: SynthesisResponse,
        project: str,
    ) -> SynthesisStep:
        """Apply a parsed decision against ``episode_id`` atomically.

        Wraps all four ``_apply_*`` methods plus ``_auto_ignore_unused``
        and ``_mark_synthesized`` inside one SAVEPOINT. On structural /
        DB error: rollback and re-raise — the caller decides how to
        surface the failure (the schema invariant is broken; further
        applies on this episode would just produce more bad rows).

        Validates ownership BEFORE entering the SAVEPOINT:
        - episode must exist
        - episode.project must match the supplied ``project`` (otherwise
          a caller could have ``_apply_new`` create reflections under
          project B while ``_mark_synthesized`` stamps project A's row)
        - episode must NOT already be synthesized (without this, a
          retry duplicates reflections — the old ``synthesize_next``
          was protected by ``_pick_oldest_pending``, which the split
          design no longer routes through)

        Decision-JSON validation lives in :meth:`parse_response`; the
        caller is expected to call that first and surface any
        :class:`SynthesisResponseError` to the producing LLM directly
        rather than stamping the episode failed.
        """
        row = self._conn.execute(
            "SELECT project, synthesized_at FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Episode {episode_id!r} not found")
        if row["project"] != project:
            raise ValueError(
                f"Episode {episode_id!r} belongs to project "
                f"{row['project']!r}, not {project!r}"
            )
        if row["synthesized_at"] is not None:
            raise ValueError(
                f"Episode {episode_id!r} is already synthesized "
                f"(synthesized_at={row['synthesized_at']})"
            )

        self._conn.execute("SAVEPOINT episode_synthesize")
        try:
            active_rows = self._conn.execute(
                "SELECT id FROM observations "
                "WHERE episode_id = ? AND status = 'active'",
                (episode_id,),
            ).fetchall()
            active_ids = [r["id"] for r in active_rows]

            self._apply_new(response.new, project=project)
            self._apply_augment(response.augment)
            self._apply_merge(response.merge)
            self._apply_ignore(response.ignore)
            auto_ignored = self._auto_ignore_unused(active_ids)
            self._mark_synthesized(episode_id)
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
            self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        self._conn.commit()

        counts = {
            "created": len(response.new),
            "augmented": len(response.augment),
            "merged": len(response.merge),
            "ignored": len(response.ignore),
            "auto_ignored": auto_ignored,
        }
        return SynthesisStep(
            processed=True, episode_id=episode_id,
            counts=counts,
            queue=self._read_queue_counts(project),
            failure=None,
        )

    # --------------------------------------------------------- retrieve_reflections
    def _fuse_by_relevance(
        self, rows: list, *, query: str, rrf_k: int = 60,
    ) -> list:
        """Re-order ``rows`` by RRF fusion of popularity rank and BM25 rank.

        ``rows`` arrives in popularity order (``useful_count + 3*times_overlooked``,
        then confidence, then recency). We compute a second ranking over the same
        ids by BM25 relevance against ``reflection_fts`` (title / use_cases /
        hints) and fuse the two with reciprocal rank fusion:

            score(d) = 1/(k + pop_rank(d)) + 1/(k + rel_rank(d))

        matching :mod:`better_memory.search.hybrid`. Rows the query does not
        match keep only the popularity term, so relevance *promotes* without
        ever discarding — a query that matches nothing degrades exactly to the
        previous behaviour.

        Tokens are OR-ed rather than AND-ed: ``sanitize_fts5_query`` joins bare
        terms, which FTS5 reads as implicit AND, and a natural-language task
        description almost never has every token present in one reflection.
        """
        ids = [r["id"] for r in rows]
        if not ids:
            return rows

        sanitized = sanitize_fts5_query(query)
        tokens = [
            t for t in sanitized.split()
            if len(t) > 2 and t.lower() not in _QUERY_STOPWORDS
        ]
        if not tokens:
            return rows
        match_expr = " OR ".join(tokens)

        placeholders = ",".join("?" for _ in ids)
        try:
            rel_rows = self._conn.execute(
                f"""
                SELECT r.id AS id, bm25(reflection_fts) AS bm
                FROM reflection_fts
                JOIN reflections r ON r.rowid = reflection_fts.rowid
                WHERE reflection_fts MATCH ? AND r.id IN ({placeholders})
                ORDER BY bm ASC
                """,
                [match_expr, *ids],
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed MATCH expression despite sanitising, or the FTS table
            # is absent on an old DB. Relevance is an enhancement, never a
            # hard dependency — fall back to the popularity order.
            return rows

        if not rel_rows:
            return rows

        rel_rank = {r["id"]: i for i, r in enumerate(rel_rows)}
        scored = []
        for pop_rank, row in enumerate(rows):
            score = 1.0 / (rrf_k + pop_rank)
            rr = rel_rank.get(row["id"])
            if rr is not None:
                score += 1.0 / (rrf_k + rr)
            scored.append((score, pop_rank, row))
        # pop_rank breaks ties deterministically and keeps the fusion stable.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [row for _, _, row in scored]

    def _bucket_item(self, r) -> dict:
        """Convert one reflections row into the dict shape returned to callers."""
        return {
            "id": r["id"],
            "title": r["title"],
            "phase": r["phase"],
            "use_cases": r["use_cases"],
            "hints": json.loads(r["hints"]),
            "confidence": r["confidence"],
            "tech": r["tech"],
            "evidence_count": r["evidence_count"],
            "useful_count": r["useful_count"],
            "times_misled": r["times_misled"],
            "times_overlooked": r["times_overlooked"],
            "times_ignored": r["times_ignored"],
            "updated_at": r["updated_at"],
        }

    def retrieve_reflections(
        self,
        *,
        project: str,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        limit_per_bucket: int | None = 20,
        track_exposure: bool = True,
        query: str | None = None,
    ) -> dict[str, list[dict]]:
        """Return reflections bucketed by polarity, ordered by confidence DESC.

        Filters:
        - ``project``: required.
        - ``tech``: matches same-tech rows OR cross-tech (tech IS NULL) rows.
        - ``phase``: optional exact match.
        - ``polarity``: optional exact match; non-matching buckets remain empty.
        - ``query``: optional natural-language description of the task at hand.
          When given, the filtered set is re-ordered by RRF fusion of the
          popularity prior with a BM25 relevance ranking over
          ``title / use_cases / hints`` (see :meth:`_fuse_by_relevance`).
          Without it, ordering is the popularity prior alone — which returns
          the same rows to every caller regardless of what they are doing.
        - ``limit_per_bucket``: cap each polarity bucket. Default 20 per spec §7.
          Pass ``None`` to disable the cap (returns every matching row per
          bucket); used by SessionBootstrapService which injects all
          reflections at session start.
        - ``track_exposure``: when ``True`` (default), writes a
          source='retrieve' row per returned memory into
          ``session_memory_exposure`` if ``CLAUDE_SESSION_ID`` is set.
          Set to ``False`` when calling from contexts that manage their own
          exposure tracking (e.g., SessionBootstrapService.bootstrap).

        Excludes retired and superseded reflections. Includes pending_review
        + confirmed.
        """
        fn = "ReflectionSynthesisService.retrieve_reflections"
        with _diag.trace(
            fn, project=project, tech=tech, phase=phase, polarity=polarity,
            limit_per_bucket=limit_per_bucket,
        ):
            tech = self._normalize_tech(tech)
            clauses = [
                "(project = ? OR scope = 'general')",
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
            _diag.step(fn, "executing_select")
            rows = self._conn.execute(
                f"""
                SELECT id, title, phase, polarity, use_cases, hints,
                       confidence, tech, evidence_count, useful_count,
                       times_misled, times_overlooked, times_ignored,
                       updated_at
                FROM reflections
                WHERE {where}
                """,
                params,
            ).fetchall()
            _diag.step(fn, "select_done", n_rows=len(rows))

            # Rank in Python: Wilson prior, then confidence, then recency.
            # Chained stable sorts apply tiebreakers lowest-priority first.
            rows = list(rows)
            rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
            rows.sort(key=lambda r: r["confidence"], reverse=True)
            rows.sort(key=_wilson_score, reverse=True)

            if query:
                rows = self._fuse_by_relevance(rows, query=query)
                _diag.step(fn, "relevance_fused", n_rows=len(rows))

            # Convert None (unlimited) to sys.maxsize so the loop body has a definite int.
            # reserve: only worth reserving an exploration slot when there's room for
            # at least one proven row alongside it (cap < 2 means the untested row
            # would consume the entire bucket).
            cap = limit_per_bucket if limit_per_bucket is not None else sys.maxsize
            reserve = limit_per_bucket is not None and cap >= 2
            buckets: dict[str, list[dict]] = {"do": [], "dont": [], "neutral": []}
            by_polarity: dict[str, list] = {"do": [], "dont": [], "neutral": []}
            for r in rows:
                by_polarity[r["polarity"]].append(r)
            for polarity, group in by_polarity.items():
                if not reserve:
                    buckets[polarity] = [self._bucket_item(r) for r in group[:cap]]
                    continue
                tested_idx = [i for i, r in enumerate(group)
                              if _wilson_rated(r) >= EXPLORATION_RATED_FLOOR]
                untested_idx = [i for i, r in enumerate(group)
                                if _wilson_rated(r) < EXPLORATION_RATED_FLOOR]
                chosen = tested_idx[: cap - 1]
                if untested_idx:
                    chosen.append(untested_idx[0])
                if len(chosen) < cap:               # top up from the remainder
                    taken = set(chosen)
                    for i in range(len(group)):
                        if len(chosen) >= cap:
                            break
                        if i not in taken:
                            chosen.append(i)
                chosen.sort()                        # preserve ranked order
                buckets[polarity] = [self._bucket_item(group[i]) for i in chosen]
            _diag.step(
                fn, "bucketed",
                do=len(buckets["do"]), dont=len(buckets["dont"]),
                neutral=len(buckets["neutral"]),
            )

            # Best-effort exposure tracking. Skip silently when env is missing
            # (e.g., test or non-Claude context) — see spec §5.2.1.
            # track_exposure=False is used by SessionBootstrapService.bootstrap,
            # which manages its own exposure write via _record_exposure.
            sid = env_session_id()
            if track_exposure:
                _diag.step(fn, "exposure_track_begin", sid=bool(sid))
                if not sid:
                    # Best-effort: bump diagnostics counter. Swallow any error so
                    # the missing-env path stays silent.
                    try:
                        self._conn.execute(
                            "UPDATE rating_diagnostics "
                            "SET value = value + 1, updated_at = ? "
                            "WHERE metric = 'session_id_missing'",
                            (self._clock().isoformat(),),
                        )
                        self._conn.commit()
                    except BaseException:
                        pass
                else:
                    all_ids = [
                        r["id"] for bucket in buckets.values() for r in bucket
                    ]
                    if all_ids:
                        now = self._clock().isoformat()
                        _diag.step(
                            fn, "exposure_insert", n_ids=len(all_ids)
                        )
                        # One row per (session, memory) — see
                        # SessionBootstrapService.record_exposures for why
                        # re-serves must not add rows.
                        self._conn.executemany(
                            "INSERT INTO session_memory_exposure "
                            "(session_id, memory_kind, memory_id, exposed_at, source) "
                            "SELECT ?, 'reflection', ?, ?, 'retrieve' "
                            "WHERE NOT EXISTS ("
                            "  SELECT 1 FROM session_memory_exposure "
                            "  WHERE session_id = ? AND memory_kind = 'reflection' "
                            "    AND memory_id = ?)",
                            [(sid, rid, now, sid, rid) for rid in all_ids],
                        )
                        _diag.step(fn, "exposure_commit")
                        self._conn.commit()
                _diag.step(fn, "exposure_track_done")
            return buckets


class ReflectionService:
    """UI-facing writes for reflections.

    Sibling of ``ReflectionSynthesisService``: this class does NOT
    synthesise — it handles the four lifecycle actions the user
    drives from the Reflections tab drawer:

    - ``confirm``: pending_review → confirmed (idempotent on confirmed).
    - ``retire``: pending_review/confirmed → retired (idempotent on retired).
    - ``update_text``: edit use_cases / hints in place; blocked on
      retired and superseded so we don't surprise the synthesis
      pipeline by mutating retired text.
    - ``promote_to_general``: project → general scope; idempotent on
      already-general; blocked on retired and superseded so promoted-but-
      invisible state can't slip into the cross-project pile.

    All four bump ``updated_at`` only when the row actually changes
    (no-op cases leave the timestamp untouched so reinforcement /
    audit trails stay honest).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or default_clock

    def confirm(self, *, reflection_id: str) -> None:
        """pending_review → confirmed; no-op on confirmed; raise on retired/superseded."""
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status == "confirmed":
            return
        if status != "pending_review":
            raise ValueError(
                f"Cannot confirm reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET status = 'confirmed', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()

    def retire(self, *, reflection_id: str) -> None:
        """pending_review / confirmed → retired; no-op on retired; raise on superseded."""
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status == "retired":
            return
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot retire reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET status = 'retired', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()

    def update_text(
        self, *, reflection_id: str, use_cases: str, hints: str
    ) -> None:
        """Edit ``use_cases`` and ``hints`` in place.

        Hints are accepted as newline-separated text (the UI form
        contract: one hint per line). Internally stored as
        ``json.dumps(list[str])`` to match
        ``ReflectionSynthesisService``'s contract — synthesis
        round-trips ``hints`` through ``json.loads()`` at two call
        sites (``_apply_augment``, ``retrieve_reflections``), so plain-text
        storage would crash
        the LLM read path.

        Blocked on retired/superseded — once a reflection has left the
        active set, mutating its text would silently change the audit
        trail.
        """
        if not use_cases or not use_cases.strip():
            raise ValueError("use_cases must not be empty")
        # Parse hints from newline-separated UI input → list[str].
        hint_list = [
            line.strip()
            for line in (hints or "").splitlines()
            if line.strip()
        ]
        if not hint_list:
            raise ValueError("hints must not be empty")
        row = self._conn.execute(
            "SELECT status FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot edit reflection in status {status!r}"
            )
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET use_cases = ?, hints = ?, updated_at = ? "
            "WHERE id = ?",
            (use_cases, json.dumps(hint_list), now, reflection_id),
        )
        self._conn.commit()

    def promote_to_general(self, *, reflection_id: str) -> None:
        """project → general; idempotent on already-general; raise on retired/superseded.

        Mirrors the no-op-on-already-target semantics of ``confirm`` and
        ``retire``: when the reflection is already general we return
        without bumping ``updated_at`` so audit trails stay honest.

        Status guard matches the UI gate in the drawer template — the
        button is hidden on retired/superseded, but we enforce server
        side too in case of direct API calls.
        """
        row = self._conn.execute(
            "SELECT scope, status FROM reflections WHERE id = ?",
            (reflection_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot promote reflection in status {status!r}"
            )
        if row["scope"] == "general":
            return
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET scope = 'general', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()
