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

from collections.abc import Sequence
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

    @property
    def supports_observations(self) -> bool:
        """True when the backend stores raw observations as a first-class,
        listable record type (the Observations tab and observation drawers).
        False in agentcore mode, where AgentCore ingests events and exposes
        only extracted memory records -- there is no raw-observation store to
        list. The management UI hides the Observations tab when this is
        False."""
        ...

    @property
    def supports_provenance(self) -> bool:
        """True when a memory can be traced to the observations / episode it
        was synthesised from (the provenance joins shown in drawers). False in
        agentcore mode, where extraction happens inside AgentCore and no
        per-memory provenance chain is returned. The UI hides provenance
        sections when this is False."""
        ...

    @property
    def supports_retention_runs(self) -> bool:
        """True when retention / pruning executes as recorded local runs the
        UI can list (the Diagnostics retention panel). False in agentcore
        mode, where pruning is event-expiry managed by AgentCore with no local
        run ledger."""
        ...

    @property
    def supports_reflection_review(self) -> bool:
        """True when reflections pass through a local pending_review ->
        confirmed lifecycle the UI can action (the confirm control). False in
        agentcore mode, whose status vocabulary is active / promoted / retired
        with NO pending_review state -- reflections are born active."""
        ...

    @property
    def supports_reflection_text_edit(self) -> bool:
        """True when a reflection's use_cases / hints text is user-editable in
        place (the edit form). False in agentcore mode, where reflection
        bodies are AI-managed by AgentCore extraction and not free-text
        editable."""
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
        present (sqlite reads times_ignored column directly; agentcore reads
        ignored_count counter — body-first for migrated reflections, metadata
        numberValue for AWS-extracted). Sync —
        no embedder call (reflections are pre-extracted in both backends;
        both backends rank by the SAME Wilson lower bound on
        (useful+overlooked)/rated formula and tiebreaks — sqlite computes it
        in Python over its columns, agentcore computes it client-side over
        the metadata/body counters it read back; the exploration slot is
        identical on both).

        ``query`` optionally supplies a natural-language description of the
        task at hand. sqlite mode fuses a BM25 relevance ranking over
        title / use_cases / hints into the Wilson-prior via RRF; agentcore
        mode fuses a server-side semantic-search ranking instead
        (`RetrieveMemoryRecords`, no BM25 leg — the semantic search
        subsumes it), same RRF shape, degrading to the Wilson-only order on
        an AWS error. Omitting it yields the Wilson-prior alone, which is
        identical for every caller regardless of the work being done.

        This method is the canonical path for the MCP ``memory.retrieve``
        tool handler and the ``memory.start_episode`` handler in
        ``better_memory/mcp/server.py``."""
        ...

    def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
        """Single reflection row as a dict (ReflectionFull field shape), NO
        provenance; None when absent. sqlite reads its row; agentcore fetches +
        parses the record. The drawer route composes provenance separately
        (queries.reflection_provenance on the local conn, flag-gated in PR 3)."""
        ...

    def reflection_list(
        self,
        *,
        project: str | None = None,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        status: str | None = None,
        min_confidence: float = 0.0,
        useful_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Flat reflection list for the UI panel, ordered by the shared Wilson
        lower bound desc / confidence desc / updated_at desc. sqlite delegates to
        queries.reflection_list_for_ui; agentcore fans out over the reflections
        (+ retired, when the status set admits it) namespaces and filters/orders
        client-side. status=None admits the live set (sqlite pending_review/
        confirmed; agentcore active/promoted)."""
        ...

    def distinct_projects(self) -> list[str]:
        """Distinct project names for the Reflections project dropdown.

        sqlite: SELECT DISTINCT project FROM reflections. agentcore:
        ListActors UNION the migration-ledger namespaces (best-effort)."""
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

    def semantic_get(self, *, id: str) -> Any | None:
        """Single semantic memory (SemanticMemory) by id, or None when absent.
        sqlite delegates to SemanticMemoryService.get (a direct row SELECT by
        id, same field set list_for_project returns); agentcore fetches the
        record by id (GetMemoryRecord via the same lookup semantic_update_text
        / semantic_set_scope use) and maps it through the same
        summary-to-model path semantic_list uses, treating a
        ResourceNotFoundException as the None case."""
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
        """List unrated memory exposures for the given session.

        Backend-agnostic: both backends read the SAME local
        ``session_memory_exposure`` table via ``services.exposure_log``.
        SqliteBackend always has a connection; AgentCoreBackend uses one
        when available (``local_conn``, wired from the caller's local
        ``memory.db`` connection — see ``storage/factory.py`` and the
        ``contextual_inject`` / ``session_bootstrap`` hooks) and otherwise
        degrades to the empty envelope ``{"session_id": ..., "exposures":
        []}``. AgentCore's memory records carry no exposure log of their
        own — session-operational state like this always lives locally,
        never in AgentCore, regardless of where memory CONTENT lives."""
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
        items: Sequence[tuple[str, str, str | None]],
        source: str,
    ) -> None:
        """Record (kind, id, display) memory exposures for later rating.

        display is a snapshot of the memory's title/content at exposure
        time (None when unavailable); it makes agentcore-id exposures
        renderable without any local content row.

        Backend-agnostic: both backends write the SAME local
        ``session_memory_exposure`` table via ``services.exposure_log``.
        SqliteBackend always has a connection; AgentCoreBackend uses the
        local ledger connection when one is available (``local_conn``) and
        otherwise no-ops — AgentCore's memory records carry no exposure
        log of their own, so this table is always local session-operational
        state, never AgentCore-side content, regardless of backend."""
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
        quote); non-ignored classes require a non-empty value, validated
        identically on both backends (`services.memory_rating.validate_evidence`
        underneath both SqliteBackend and AgentCoreBackend). Both backends
        also reject `classification='ignored'` here — 'ignored' is the
        session-end sweep's exclusive write path
        (`apply_session_ratings`). On a successful AWS counter push,
        AgentCoreBackend best-effort stamps the local exposure row
        (`rated_at`/`classification`/`evidence`) when a local ledger is
        wired — see AgentCoreBackend.credit_one.
        """
        ...

    # ----- Contextual relevance -----

    def relevance_ranks(
        self,
        *,
        query: str,
        kinds: tuple[str, ...] = ("reflection", "semantic"),
        top_k: int = 50,
    ) -> dict[tuple[str, str], int] | None:
        """Server-side relevance rank map for the contextual evidence gate
        (``services/relevant.py``'s ``retrieve_relevant``). Returns
        ``{(kind, id): rank}`` -- 0 is the best match, per kind (ranks are
        not comparable ACROSS kinds; each (kind, id) is only ever looked up
        for its own candidate). Never raises.

        The empty-dict / ``None`` distinction is load-bearing, NOT
        interchangeable: ``{}`` means the lookup ran and genuinely found no
        matches (a legitimate negative result -- the caller's evidence gate
        must respect it, not paper over it with a keyword-hit fallback).
        ``None`` means the lookup itself could not run/complete (e.g. an
        AWS error on every namespace) -- THIS is the caller's designated
        signal to degrade to the keyword-hit fallback. Conflating the two
        would let keyword overlap re-qualify memories the server-side
        search legitimately rejected (design spec
        2026-07-24-agentcore-parity-design.md §3).

        SqliteBackend: a thin wrapper over its existing BM25
        (``reflection_fts``) leg only -- the vector leg that used to also
        cover semantics was removed in remove-ollama-embeddings Task 7, so
        ``"semantic"`` in ``kinds`` never contributes any ranks here. Its
        own leg never raises (it degrades internally), so it always
        returns a dict, never ``None``. Provided for protocol completeness
        only -- ``retrieve_relevant``'s sqlite path keeps calling that leg
        directly against its own ``conn`` parameter and never calls this
        method, so sqlite's contextual-gate behavior is unaffected by this
        method's existence.

        AgentCoreBackend: server-side semantic search
        (``retrieve_memory_records``) fanned out per kind's project +
        general namespace pair (reusing ``_relevance_rank_map`` /
        ``_merge_relevance_rank_maps``, the same machinery ``retrieve``
        uses for its own RRF fusion with the Wilson prior), ranked by
        result order. Best-effort per namespace: a kind whose EVERY
        namespace call fails contributes ``None`` (propagated up to an
        overall ``None`` only if every requested kind fails that way);
        any kind with at least one successful namespace call contributes
        its (possibly empty) results to the overall dict.
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
