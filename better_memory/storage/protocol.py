"""StorageBackend Protocol.

Fat protocol with high-level operations. Both SqliteBackend (Plan 1) and
AgentCoreBackend (Plan 2) implement it. Synthesis methods are sqlite-only —
the MCP server reads `supports_synthesis` to gate their tool registration.

Method shapes mirror the existing service surface verified at HEAD bff6506:
- observe / list_observations are async (ObservationService is async)
- retrieve is sync — wraps ReflectionSynthesisService.retrieve_reflections,
  which is sync with no embedder call (Plan 2 Task 0 amendment)
- record_use is sync
- semantic / episode / reflection lifecycle methods are sync
- synthesis methods are sync

session_id and project are held on the implementation (e.g. as constructor
state on SqliteBackend) rather than passed per-call, since one backend
instance serves exactly one MCP session.

Where a write method declares `project: str | None = None`, the backend
MUST resolve `None` to a concrete project from its own state before
persisting. Read methods (e.g. `list_episodes`) pass `project=None`
through to the underlying service so callers can query across all
projects when desired.
Where a method declares `session_id: str` as a required kwarg, that is
because the underlying service (e.g. EpisodeService, MemoryRatingService)
is process-global and the kwarg is forwarded to the service unchanged —
the Protocol is not declaring per-call session reconfiguration.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


# Outcome aliases are DEFINED here (not imported from services) to avoid a
# `better_memory.storage → services` import edge in the public surface. The same
# literal set also exists in `services/observation.py:76` and `search/hybrid.py:33`;
# consolidating into a single canonical source is tracked for a later cleanup.
Outcome = Literal["success", "failure", "neutral"]
# UseOutcome omits "neutral" because ObservationService.record_use
# (services/observation.py:445) only branches on success / failure — passing
# "neutral" would silently no-op.
UseOutcome = Literal["success", "failure"]


@runtime_checkable
class StorageBackend(Protocol):
    """High-level storage operations consumed by the MCP layer."""

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        """True when the synthesize_next_* MCP tools should be registered."""
        ...

    @property
    def supports_episodes(self) -> bool:
        """True when the backend exposes the episode-lifecycle methods as
        first-class operations. False when episodes are an internal
        implementation detail (e.g. agentcore mode, where AgentCore manages
        event grouping via sessionId). The management UI hides the Episodes
        tab when this is False."""
        ...

    # ----- Observations -----

    async def observe(
        self,
        *,
        content: str,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        scope: str = "project",
    ) -> str:
        """Record an observation. Returns the new observation id."""
        ...

    def retrieve(
        self,
        *,
        project: str | None = None,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        limit_per_bucket: int | None = 20,
        track_exposure: bool = True,
        query: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Bucketed reflection retrieval, keyed by polarity (do / dont / neutral).

        Each bucket is a list of reflection dicts: ``{id, title, phase,
        use_cases, hints (list[str]), confidence (float), tech,
        evidence_count, useful_count, times_overlooked, times_ignored,
        times_misled, updated_at}``. ``times_overlooked`` and
        ``times_ignored`` feed the Wilson-lower-bound prior in
        ``services/relevant.py``; both backends guarantee the keys are
        present, but agentcore's ``times_ignored`` is always ``0`` (it has
        no exposure/rating sweep to derive it from). Sync —
        no embedder call (reflections are pre-extracted in both backends;
        sqlite mode ranks by Wilson lower bound on (useful+overlooked)/rated,
        computed in Python; agentcore mode applies the legacy linear formula
        client-side over metadata counters).

        ``query`` optionally supplies a natural-language description of the
        task at hand. sqlite mode fuses a BM25 relevance ranking over
        title / use_cases / hints into the Wilson-prior via RRF; agentcore
        mode ignores it. Omitting it yields the Wilson-prior alone, which
        is identical for every caller regardless of the work being done.

        This method is the canonical path for the MCP ``memory.retrieve``
        tool handler and the ``memory.start_episode`` handler in
        ``better_memory/mcp/server.py``."""
        ...

    async def list_observations(
        self,
        *,
        project: str | None = None,
        episode_id: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Drill-down list of raw observations."""
        ...

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        """Credit an observation's reinforcement counter. Raises ValueError if missing."""
        ...

    # ----- Semantic memories -----

    def semantic_observe(
        self,
        *,
        content: str,
        project: str | None = None,
        scope: str = "project",
    ) -> str:
        """Create a semantic memory. Returns its id."""
        ...

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        """List semantic memories for the project. Returns list[SemanticMemory]."""
        ...

    def semantic_update_text(self, *, id: str, content: str) -> None:
        """Update the text of a semantic memory."""
        ...

    def semantic_set_scope(self, *, id: str, scope: str) -> None:
        """Change the scope (project/general) of a semantic memory."""
        ...

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic memory (idempotent)."""
        ...

    # ----- Episodes -----

    def open_background_episode(
        self,
        *,
        session_id: str,
        project: str,
    ) -> str:
        """Open a background episode for the given session. Returns episode id."""
        ...

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        """Start a foreground episode. Returns episode id."""
        ...

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close the active episode for this session."""
        ...

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        """Close a specific episode by id."""
        ...

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        """List episodes. Returns list[Episode]."""
        ...

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, reflection_id: str) -> None:
        """Promote a project-scope reflection to general scope."""
        ...

    def retire_reflection(self, *, reflection_id: str) -> None:
        """Retire a reflection (exclude from default retrieval)."""
        ...

    # ----- Session lifecycle -----

    def session_bootstrap(
        self,
        *,
        session_id: str,
        source: str | None = None,
        cwd: Any | None = None,
        project: str | None = None,
    ) -> Any:
        """Build the SessionStart additionalContext envelope. Returns BootstrapResult."""
        ...

    def list_session_exposures(
        self,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """List unrated memory exposures for the given session."""
        ...

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> Any:
        """Atomically apply per-exposure ratings. Returns ApplySessionRatingsResult."""
        ...

    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        """Record (kind, id) memory exposures for later rating. Sqlite writes
        session_memory_exposure rows; agentcore is a documented no-op (it has
        no exposure log - rating flows through credit_one)."""
        ...

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
        evidence: str | None = None,
    ) -> Any:
        """Apply a single rating for an exposed memory. Returns ApplyOutcome.

        `evidence` is a one-line string (what the memory changed, or a
        quote); non-ignored classes require a non-empty value —
        SqliteBackend forwards it to MemoryRatingService, which validates
        and stores it. AgentCoreBackend accepts the parameter for
        signature parity but has no evidence storage (no exposure table)
        and does not validate or persist it — see AgentCoreBackend.credit_one.
        """
        ...

    # ----- Synthesis (sqlite-only — guarded by supports_synthesis) -----

    def synthesize_next_get_context(
        self,
        *,
        project: str,
    ) -> Any:
        """Pop the next pending episode context. Returns EpisodeContext | None. Sqlite-only."""
        ...

    def synthesize_next_apply(
        self,
        *,
        episode_id: str,
        response: Any,
        project: str,
    ) -> Any:
        """Apply a SynthesisResponse. Returns SynthesisStep. Sqlite-only."""
        ...
