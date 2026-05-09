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

Connection ownership: caller owns the sqlite3 connection. The service does
not commit on its own — episode opens commit through ``EpisodeService``'s
existing SAVEPOINT envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from better_memory.config import project_name
from better_memory.services.episode import EpisodeService

_VALID_SOURCES: frozenset[str] = frozenset({"startup", "resume", "clear", "compact"})


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
    def __init__(self, conn) -> None:
        self._conn = conn
        self._episodes = EpisodeService(conn)

    def bootstrap(
        self,
        *,
        source: str | None,
        session_id: str,
        cwd: Path,
    ) -> BootstrapResult:
        coerced_source = source if source in _VALID_SOURCES else "startup"
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

        return BootstrapResult(
            additional_context="",  # filled in Task 5
            project=project,
            source=coerced_source,
            episode_id=episode_id,
            episode_action=action,
        )
