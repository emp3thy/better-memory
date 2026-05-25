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
commit through ``EpisodeService``'s existing SAVEPOINT envelope. ``_record_exposure``
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
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.semantic import SemanticMemoryService

_VALID_SOURCES: frozenset[str] = frozenset({"startup", "resume", "clear", "compact"})

_HINT_MAX_CHARS = 600

_FOOTER = (
    "Use mcp__better-memory__memory_record_use(id, success|failure) when a "
    "memory materially helps or misleads. Use mcp__better-memory__memory_observe "
    "to write new ones."
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


def _render_semantic(items) -> tuple[str, list[str]]:
    if not items:
        return "", []
    lines = [f"### Semantic memories ({len(items)} entries)"]
    ids: list[str] = []
    for m in items:
        lines.append(f"- [{m.id[:8]}] {_truncate(m.content)}")
        ids.append(m.id)
    return "\n".join(lines), ids


def _render_reflection_bucket(name: str, items) -> tuple[str, list[str]]:
    if not items:
        return "", []
    blocks: list[str] = []
    ids: list[str] = []
    for item in items:
        lines = [
            f"**{item['title']}**",
            f"_{item['use_cases']}_",
        ]
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate(hint)}")
        lines.append(f"_id: {item['id']}_")
        blocks.append("\n".join(lines))
        ids.append(item["id"])
    return f"### Reflections — {name}\n" + "\n\n".join(blocks), ids


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
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._episodes = EpisodeService(conn)

    def _record_exposure(
        self,
        *,
        session_id: str,
        reflection_ids: list[str],
        semantic_ids: list[str],
    ) -> None:
        """Write one row per injected memory into session_memory_exposure.

        Called only after rendering succeeded, so we don't credit memories
        the LLM never actually saw. Best-effort: skips entirely if
        session_id is empty (e.g., manual invocation without env).
        """
        if not session_id:
            return
        now = self._clock().isoformat()
        rows = (
            [(session_id, "reflection", rid, now, "bootstrap")
             for rid in reflection_ids] +
            [(session_id, "semantic", sid, now, "bootstrap")
             for sid in semantic_ids]
        )
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def bootstrap(
        self,
        *,
        source: str | None = None,
        session_id: str,
        cwd: Path | None = None,
        project: str | None = None,
    ) -> BootstrapResult:
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

        sections: list[str] = [
            _render_header(
                project=project,
                source=coerced_source,
                action=action,
                episode_id=episode_id,
            ),
        ]
        sem_section, semantic_ids = _render_semantic(semantic)
        if sem_section:
            sections.append(sem_section)
        do_section, do_ids = _render_reflection_bucket("do (prior wins)", buckets["do"])
        if do_section:
            sections.append(do_section)
        dont_section, dont_ids = _render_reflection_bucket("dont (approaches to avoid)", buckets["dont"])
        if dont_section:
            sections.append(dont_section)
        neutral_section, neutral_ids = _render_reflection_bucket("neutral (context)", buckets["neutral"])
        if neutral_section:
            sections.append(neutral_section)
        sections.append("---")
        sections.append(_FOOTER)

        rendered = "\n\n".join(sections)

        self._record_exposure(
            session_id=session_id,
            reflection_ids=do_ids + dont_ids + neutral_ids,
            semantic_ids=semantic_ids,
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
        rows = self._conn.execute(
            """
            SELECT e.memory_kind, e.memory_id,
                   MIN(e.exposed_at) AS exposed_at,
                   MIN(e.source) AS source,
                   COALESCE(r.title, s.content) AS display
              FROM session_memory_exposure e
              LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                            AND e.memory_id = r.id
              LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                            AND e.memory_id = s.id
             WHERE e.session_id = ? AND e.rated_at IS NULL
             GROUP BY e.memory_kind, e.memory_id
             ORDER BY exposed_at ASC
            """,
            (session_id,),
        ).fetchall()
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
