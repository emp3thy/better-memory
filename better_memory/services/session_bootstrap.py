"""Session bootstrap: source-aware episode lifecycle + memory injection.

Invoked on every Claude Code SessionStart event by the
``better_memory.hooks.session_bootstrap`` hook and via the
``memory.session_bootstrap`` MCP tool. Owns:

- Source coercion (startup / resume / clear / compact; unknowns -> startup).
- Project resolution (delegates to ``better_memory.config.project_name``).
- Idempotent episode lifecycle (open new only on startup with no active
  episode; reuse otherwise).
- Retrieval of project + general semantic memories and reflections.
- Markdown rendering for ``additionalContext`` injection.

Connection ownership: caller owns the sqlite3 connection. Episode opens
commit through ``EpisodeService``'s existing SAVEPOINT envelope. ``record_exposures``
issues its own commit for the exposure write — callers must not wrap ``bootstrap()``
in an outer transaction they intend to roll back.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from better_memory.config import project_name
from better_memory.services import exposure_log
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.semantic import SemanticMemoryService

_VALID_SOURCES: frozenset[str] = frozenset({"startup", "resume", "clear", "compact"})

_HINT_MAX_CHARS = 600

_FOOTER = (
    "When an injected memory materially helps or misleads, credit it: "
    "memory_credit(kind, id, class, evidence) - one-line evidence statement. "
    "Use mcp__better-memory__memory_observe to write new ones."
)


def _truncate(s: str) -> str:
    return s if len(s) <= _HINT_MAX_CHARS else s[: _HINT_MAX_CHARS - 1] + "…"


def _render_header(*, project: str, source: str, action: str, episode_id: str) -> str:
    short = episode_id[:8] if episode_id else ""
    return (
        f"## better-memory: session bootstrap\n"
        f"Project: {project}  •  Source: {source}  •  "
        f"Episode: {action} id={short}"
    )


def _age_suffix(iso_ts: str | None, now: datetime) -> str:
    if not iso_ts:
        return ""
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return f" ({max(0, (now - ts).days)}d old)"


def _render_semantic_full(items, now: datetime) -> tuple[str, list[str]]:
    if not items:
        return "", []
    lines = [f"### Semantic memories ({len(items)} shown in full)"]
    ids: list[str] = []
    for m in items:
        lines.append(f"- [{m.id}]{_age_suffix(m.updated_at, now)} {_truncate(m.content)}")
        ids.append(m.id)
    return "\n".join(lines), ids


def _render_reflections_full(pairs, now: datetime) -> tuple[str, list[str]]:
    """pairs: list of (polarity, reflection-dict) already capped to top-N."""
    if not pairs:
        return "", []
    blocks: list[str] = []
    ids: list[str] = []
    for polarity, item in pairs:
        label = {
            "do": "do",
            "dont": "dont (pitfall - do the corrective action)",
            "neutral": "neutral",
        }[polarity]
        lines = [
            f"**{item['title']}** [{label}]"
            f"{_age_suffix(item.get('updated_at'), now)}",
            f"_{item['use_cases']}_",
        ]
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate(hint)}")
        lines.append(f"_id: {item['id']}_")
        blocks.append("\n".join(lines))
        ids.append(item["id"])
    return "### Reflections (shown in full)\n" + "\n\n".join(blocks), ids


def _render_index(sem_index, refl_index) -> tuple[str, int]:
    n = len(sem_index) + len(refl_index)
    if n == 0:
        return "", 0
    lines = ["### Index (not expanded - retrieve on demand)"]
    for polarity, item in refl_index:
        conf = item.get("confidence")
        conf_s = f", conf {conf:.1f}" if isinstance(conf, (int, float)) else ""
        lines.append(f"- {item['title']} ({polarity}{conf_s})")
    for m in sem_index:
        first_line = (m.content or "").splitlines()[0][:100]
        lines.append(f"- {first_line} (semantic)")
    return "\n".join(lines), n


@dataclass(frozen=True)
class BootstrapResult:
    additional_context: str
    project: str
    source: str
    episode_id: str
    episode_action: Literal["opened", "reused"]
    semantic_count: int = 0
    reflections_counts: dict[str, int] = field(
        default_factory=lambda: {"do": 0, "dont": 0, "neutral": 0}
    )


class SessionBootstrapService:
    def __init__(
        self,
        conn,
        *,
        clock: Callable[[], datetime] | None = None,
        top_n: int | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._episodes = EpisodeService(conn)
        self._top_n = top_n

    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str, str | None]],
        source: str,
    ) -> None:
        """Write one session_memory_exposure row per (kind, id, display) triple.

        At most one row per (session, kind, id), regardless of how many times
        the memory is re-served within the session. The rating vocabulary
        treats a session's exposures as a SET — list_session_exposures groups
        by (kind, id) and _apply_one stamps every row with one classification
        — but the PK includes exposed_at, so before this guard each re-serve
        added a row and any statistic over the raw table double-counted.
        Measured on a live DB the inflation was 16.08% vs 9.25% "useful"
        for the identical underlying behaviour.

        Best-effort: skips entirely when session_id is empty. Own commit
        (see module docstring on connection ownership).
        """
        if not session_id or not items:
            return
        now = self._clock().isoformat()
        exposure_log.record(
            self._conn,
            session_id=session_id,
            items=items,
            source=source,
            now=now,
        )
        self._conn.commit()

    def _record_exposure(
        self,
        *,
        session_id: str,
        reflection_ids: list[str],
        semantic_ids: list[str],
        reflection_display: dict[str, str | None],
        semantic_display: dict[str, str | None],
    ) -> None:
        """Write one row per injected memory into session_memory_exposure.

        Called only after rendering succeeded, so we don't credit memories
        the LLM never actually saw. Best-effort: skips entirely if
        session_id is empty (e.g., manual invocation without env).
        """
        self.record_exposures(
            session_id=session_id,
            items=[("reflection", rid, reflection_display.get(rid))
                   for rid in reflection_ids]
            + [("semantic", sid, semantic_display.get(sid))
               for sid in semantic_ids],
            source="bootstrap",
        )

    def bootstrap(
        self,
        *,
        source: str | None = None,
        session_id: str,
        cwd: Path | None = None,
        project: str | None = None,
    ) -> BootstrapResult:
        from better_memory.config import get_config

        coerced_source = source if source in _VALID_SOURCES else "startup"
        if project is None:
            if cwd is None:
                raise ValueError("Either 'project' or 'cwd' must be provided.")
            project = project_name(cwd)

        existing = self._episodes.active_episode(session_id)
        if existing is None:
            episode_id = self._episodes.open_background(
                session_id=session_id, project=project,
            )
            action: Literal["opened", "reused"] = "opened"
        else:
            episode_id = existing.id
            action = "reused"

        semantic_svc = SemanticMemoryService(self._conn)
        # When project resolves to "general", scope_filter="general" excludes rows
        # tagged project='general' with scope='project'. The default union
        # (project = ? OR scope = 'general') would otherwise pull them.
        # See spec §6.
        # track_exposure=False: bootstrap manages its own source='bootstrap'
        # exposure write via _record_exposure; the inline retrieve path must
        # not write a duplicate source='retrieve' row for the same memory.
        if project == "general":
            semantic = semantic_svc.list_for_project(
                project=project, scope_filter="general", track_exposure=False,
            )
        else:
            semantic = semantic_svc.list_for_project(
                project=project, scope_filter=None, track_exposure=False,
            )

        reflection_svc = ReflectionSynthesisService(self._conn)
        # Cap each polarity bucket so the bootstrap injection stays
        # signal-dense. Ranking inside retrieve_reflections already
        # surfaces battle-tested + recently-updated rows first, so the
        # rows that get dropped are the never-used long tail.
        buckets = reflection_svc.retrieve_reflections(
            project=project, limit_per_bucket=20, track_exposure=False,
        )

        semantic_count = len(semantic)
        reflections_counts = {
            "do": len(buckets["do"]),
            "dont": len(buckets["dont"]),
            "neutral": len(buckets["neutral"]),
        }

        if get_config().inject_mode == "deferred":
            general_only = [m for m in semantic if m.scope == "general"]
            deferred_now = self._clock()
            n_refl = sum(reflections_counts.values())
            n_sem = semantic_count
            index_line = (
                f"better-memory knows {n_refl} reflections + {n_sem} semantic "
                "memories for this project; relevant ones will surface as you "
                "work - or ask via memory_retrieve with a task query."
            )
            deferred_sections: list[str] = [
                _render_header(
                    project=project,
                    source=coerced_source,
                    action=action,
                    episode_id=episode_id,
                ),
            ]
            deferred_sem_section, deferred_semantic_ids = _render_semantic_full(
                general_only, deferred_now,
            )
            if deferred_sem_section:
                deferred_sections.append(deferred_sem_section)
            deferred_sections.append(index_line)
            deferred_sections.append("---")
            deferred_sections.append(_FOOTER)

            self._record_exposure(
                session_id=session_id,
                reflection_ids=[],
                semantic_ids=deferred_semantic_ids,
                reflection_display={},
                semantic_display={m.id: m.content for m in general_only},
            )

            return BootstrapResult(
                additional_context="\n\n".join(deferred_sections),
                project=project,
                source=coerced_source,
                episode_id=episode_id,
                episode_action=action,
                semantic_count=semantic_count,
                reflections_counts=reflections_counts,
            )

        if self._top_n is not None:
            top_n = self._top_n
        else:
            top_n = get_config().bootstrap_top_n

        now = self._clock()

        if top_n == 0:
            sem_full, sem_index = list(semantic), []
        else:
            general = [m for m in semantic if m.scope == "general"]
            project_scoped = [m for m in semantic if m.scope != "general"]
            sem_full = general + project_scoped[:top_n]
            sem_index = project_scoped[top_n:]

        flat_reflections = (
            [("do", r) for r in buckets["do"]]
            + [("dont", r) for r in buckets["dont"]]
            + [("neutral", r) for r in buckets["neutral"]]
        )
        if top_n == 0:
            refl_full, refl_index = flat_reflections, []
        else:
            refl_full = flat_reflections[:top_n]
            refl_index = flat_reflections[top_n:]

        sections: list[str] = [
            _render_header(
                project=project,
                source=coerced_source,
                action=action,
                episode_id=episode_id,
            ),
        ]
        sem_section, semantic_ids = _render_semantic_full(sem_full, now)
        if sem_section:
            sections.append(sem_section)
        refl_section, reflection_ids = _render_reflections_full(refl_full, now)
        if refl_section:
            sections.append(refl_section)
        index_section, index_count = _render_index(sem_index, refl_index)
        if index_section:
            sections.append(index_section)
        sections.append("---")
        footer = _FOOTER
        if index_count:
            footer = (
                f"{index_count} more memories are indexed above - call "
                "mcp__better-memory__memory_retrieve or "
                "mcp__better-memory__memory_retrieve_observations when a task "
                "touches them.\n" + _FOOTER
            )
        sections.append(footer)

        rendered = "\n\n".join(sections)

        self._record_exposure(
            session_id=session_id,
            reflection_ids=reflection_ids,
            semantic_ids=semantic_ids,
            reflection_display={r["id"]: r.get("title") for _, r in flat_reflections},
            semantic_display={m.id: m.content for m in semantic},
        )

        return BootstrapResult(
            additional_context=rendered,
            project=project,
            source=coerced_source,
            episode_id=episode_id,
            episode_action=action,
            semantic_count=semantic_count,
            reflections_counts=reflections_counts,
        )

    def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
        """List unrated memory exposures for the given session.

        Extracted from the inline MCP ``memory.list_session_exposures`` handler
        so ``StorageBackend.list_session_exposures`` can delegate. The return
        shape is the MCP tool's wire payload — preserve verbatim.

        Empty ``session_id`` returns ``{"session_id": None, "exposures": []}``
        to preserve the inline handler's `if not sid` short-circuit (the MCP
        handler resolves session_id from env/marker and may yield an empty
        string when no session is active).
        """
        if not session_id:
            return {"session_id": None, "exposures": []}
        # Dedupe by (memory_kind, memory_id) — a memory can have two
        # exposure rows (bootstrap + retrieve) in one session. The
        # rating apply path stamps ALL unrated rows per (kind, id) in
        # one UPDATE, so the LLM must see one entry per unique memory;
        # otherwise apply_session_ratings rejects the batch for duplicate
        # (kind, id) pairs.
        rows = exposure_log.list_unrated(self._conn, session_id=session_id)
        return {
            "session_id": session_id,
            "exposures": [
                {
                    "kind": r["memory_kind"],
                    "id": r["memory_id"],
                    **({"title": r["display"]} if r["memory_kind"] == "reflection"
                       else {"content": r["display"]}),
                    "exposed_at": r["exposed_at"],
                    "source": r["source"],
                }
                for r in rows
            ],
        }
