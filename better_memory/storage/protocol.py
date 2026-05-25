"""StorageBackend Protocol.

Fat protocol with high-level operations. Both SqliteBackend (Plan 1) and
AgentCoreBackend (Plan 2) implement it. Synthesis methods are sqlite-only —
the MCP server reads `supports_synthesis` to gate their tool registration.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


# Re-export Outcome so storage callers don't need to import from services.
Outcome = Literal["success", "failure", "neutral"]


@runtime_checkable
class StorageBackend(Protocol):
    """High-level storage operations consumed by the MCP layer.

    Implementations may raise NotImplementedError for methods that don't fit
    their paradigm — but only for methods documented as backend-specific.
    The synthesis methods are the only such methods today.
    """

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        """True when synthesize_next_* tools should be registered."""
        ...

    # ----- Observations (write + read) -----

    def observe(
        self,
        *,
        content: str,
        outcome: Outcome = "neutral",
        component: str | None = None,
        theme: str | None = None,
        tech: str | None = None,
        trigger_type: str | None = None,
        scope: Literal["project", "general"] = "project",
        project: str | None = None,
    ) -> str:
        """Record an observation. Returns the new observation id."""
        ...

    def retrieve(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        phase: Literal["planning", "implementation", "general"] | None = None,
        polarity: Literal["do", "dont", "neutral"] | None = None,
        limit_per_bucket: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve reflections grouped by polarity bucket (do/dont/neutral)."""
        ...

    def retrieve_observations(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        episode_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Drill-down retrieval of raw observations."""
        ...

    # ----- Reinforcement / outcome credit -----

    def record_use(self, *, id: str, outcome: Outcome) -> None:
        """Credit a memory's reinforcement counter. Raises if id not found."""
        ...

    # ----- Semantic memories (user-curated facts) -----

    def semantic_observe(
        self,
        *,
        content: str,
        component: str | None = None,
        scope: Literal["project", "general"] = "project",
        project: str | None = None,
    ) -> str:
        """Create a semantic memory. Returns its id."""
        ...

    def semantic_retrieve(
        self,
        *,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List semantic memories."""
        ...

    def semantic_update(
        self,
        *,
        id: str,
        content: str | None = None,
        component: str | None = None,
        scope: Literal["project", "general"] | None = None,
    ) -> None:
        """Update fields on a semantic memory."""
        ...

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic memory."""
        ...

    # ----- Episodes (session boundaries) -----

    def start_episode(self, *, session_id: str, project: str | None = None) -> dict[str, Any]:
        """Open or attach an episode for the given session. Returns episode dict."""
        ...

    def list_episodes(
        self,
        *,
        project: str | None = None,
        status: Literal["open", "closed"] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List episodes."""
        ...

    def close_episode(self, *, id: str, outcome: str = "unknown") -> None:
        """Close an episode by id."""
        ...

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, id: str) -> None:
        """Promote a project-scope reflection to general scope."""
        ...

    def retire_reflection(self, *, id: str) -> None:
        """Mark a reflection as retired (excluded from default retrieval)."""
        ...

    # ----- Session lifecycle -----

    def session_bootstrap(self, *, project: str | None = None) -> dict[str, Any]:
        """Build the SessionStart additionalContext envelope."""
        ...

    def list_session_exposures(self) -> dict[str, Any]:
        """List unrated memories the current session was exposed to."""
        ...

    def apply_session_ratings(
        self, *, ratings: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Atomically apply per-exposure ratings for the current session."""
        ...

    # ----- Synthesis (sqlite-only — guarded by supports_synthesis) -----

    def synthesize_next_get_context(
        self, *, project: str | None = None
    ) -> dict[str, Any]:
        """Pop the next pending episode for synthesis. Sqlite-only."""
        ...

    def synthesize_next_apply(self, *, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply synthesis decisions atomically. Sqlite-only."""
        ...
