"""AgentCoreBackend — boto3 wrapper satisfying StorageBackend Protocol.

Constructor takes pre-built boto3 clients (data plane + control plane)
plus the loaded AgentCoreConfig. Tests inject MagicMock clients; the
factory (Task 13) constructs real clients via boto3.client(...). This
inversion keeps tests fast and free of botocore deps.

Capability flags:
- supports_synthesis = False — AgentCore's built-in episodicMemoryStrategy
  performs extraction internally; the MCP synthesize_next_* tools are not
  registered in agentcore mode.
- supports_episodes = False — AgentCore manages event grouping via
  sessionId; the better-memory episodes table has no equivalent record
  type. Episode lifecycle methods are no-ops returning synthetic ids /
  empty results; the management UI hides the Episodes tab.
- supports_observations = False -- AgentCore ingests events and exposes only
  extracted memory records; there is no raw-observation store to list.
- supports_provenance = False -- extraction happens inside AgentCore, so no
  per-memory provenance chain (observations / episode a memory came from)
  is returned.
- supports_retention_runs = False -- pruning is event-expiry managed by
  AgentCore; there is no local retention-run ledger for the UI to list.
- supports_reflection_review = False -- AgentCore's status vocabulary is
  active / promoted / retired with no pending_review state; reflections
  are born active.
- supports_reflection_text_edit = False -- reflection bodies are AI-managed
  by AgentCore extraction and are not free-text editable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from botocore.exceptions import ClientError as _ClientError
else:
    try:
        from botocore.exceptions import ClientError as _ClientError
    except ImportError:  # pragma: no cover - botocore absent in unit-test env
        class _ClientError(Exception):
            response: dict[str, Any]

from better_memory.services.scoring import wilson_lower_bound
from better_memory.storage.agentcore_persistence import AgentCoreConfig
from better_memory.storage.protocol import Outcome, UseOutcome
from better_memory.storage.session import (
    parse_hints_prose,
    resolve_actor_id,
    resolve_namespace,
)

_POLARITIES: tuple[str, str, str] = ("do", "dont", "neutral")
_AGENTCORE_SYSTEM_METADATA_PREFIX = "x-amz-agentcore-memory-"
_RATING_TO_COUNTER: dict[str, str] = {
    "cited": "useful_count",
    "shaped": "useful_count",
    "ignored": "ignored_count",
    "misled": "times_misled",
    "overlooked": "overlooked_count",
}

# For migrated (SQLite-origin) reflection records, rating state lives in the
# JSON content body under the SQLite column names (design §1b/§3.1). The
# AgentCore metadata counter key `overlooked_count` is stored in the body under
# the SQLite column name `times_overlooked`; every other counter key keeps the
# same name in the body. Used to translate a metadata counter key to its body
# field when read-modify-writing a migrated record's content.
_COUNTER_KEY_TO_BODY_FIELD: dict[str, str] = {
    "overlooked_count": "times_overlooked",
}


class AgentCoreBackend:
    """boto3-backed StorageBackend implementation."""

    def __init__(
        self,
        *,
        config: AgentCoreConfig,
        data_client: Any,
        control_client: Any,
        session_id: str | None,
        project: str,
        local_conn: sqlite3.Connection | None = None,
    ) -> None:
        self._cfg = config
        self._data = data_client
        self._control = control_client
        self._session_id = session_id
        self._project = project
        # Local exposure ledger (session_memory_exposure table in the local
        # memory.db). Agentcore mode never stores memory CONTENT locally —
        # reflections/semantic memories/observations live in AgentCore — but
        # session-operational state (the exposure ledger, the migration
        # ledger, hook_errors) lives alongside the sqlite backend's own
        # database regardless of which backend holds the content. None when
        # no local db is available (e.g. some unit tests); record_exposures
        # / list_session_exposures degrade to their pre-existing no-op /
        # empty-envelope behaviour in that case.
        self._local_conn = local_conn

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return False

    @property
    def supports_episodes(self) -> bool:
        return False

    @property
    def supports_observations(self) -> bool:
        return False

    @property
    def supports_provenance(self) -> bool:
        return False

    @property
    def supports_retention_runs(self) -> bool:
        return False

    @property
    def supports_reflection_review(self) -> bool:
        return False

    @property
    def supports_reflection_text_edit(self) -> bool:
        return False

    # ----- Session-id per-operation re-resolution -----

    def _require_session_id(self, operation: str) -> str:
        """Resolve the session id fresh on EVERY operation — never freeze.

        The MCP server resolves the session id once at startup, but real
        Claude Code does not propagate CLAUDE_SESSION_ID into the spawned
        stdio server's env; the server may spawn BEFORE the SessionStart
        hook writes the marker file, and — the live-review major — a
        long-lived server process outlives Claude sessions, so a marker
        adopted once would go stale for the rest of the process lifetime.

        Resolution order per call:
        1. live sources: CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID env,
           then the SessionStart marker under the resolved
           BETTER_MEMORY_HOME (a fresh marker always beats any value seen
           at construction time);
        2. the construction-time session_id, as a fallback when no live
           source resolves;
        3. otherwise raise WITHOUT any wire call (no uuid4 fallback
           fabricating identities).

        The result is deliberately NOT cached on self — the whole point is
        that a new session's marker takes effect on the next operation.
        """
        # Local import: keeps the storage layer free of an mcp import
        # edge at module scope (and off the hooks' lightweight-import
        # path — this only loads when an event operation runs).
        from better_memory._common import resolve_home
        from better_memory.mcp._util import resolve_session_id

        live = resolve_session_id(resolve_home())
        if live is not None:
            return live
        if self._session_id is not None:
            return self._session_id
        raise ValueError(
            f"AgentCoreBackend.{operation} requires session_id: none was "
            "available at construction time and re-resolution found "
            "neither CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID in the "
            "environment nor a SessionStart marker file under "
            "BETTER_MEMORY_HOME (the SessionStart hook writes it; if you "
            "see this in production the hook has not run yet)."
        )

    # ----- Observations: filled in by Tasks 5-6 -----

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
        """Write an observation as a CreateEvent against the episodic memory.

        sessionId is the backend's held session id, lazily re-resolved from
        env / SessionStart marker when construction saw None (raised, with
        zero wire calls, when nothing resolves — events require a real
        session). actorId is resolved from project (or "general" when no
        project is in scope). Returns the AgentCore eventId."""
        session_id = self._require_session_id("observe")
        actor_id = resolve_actor_id(project or self._project)

        # Event-level metadata is stringValue-only (verified API surface);
        # richer typing only on memory record metadata. Drop None values.
        metadata: dict[str, dict[str, Any]] = {}
        raw = {
            "outcome": outcome,
            "component": component,
            "theme": theme,
            "trigger_type": trigger_type,
            "tech": tech,
            "scope": scope,
            "scope_path": scope_path,
        }
        for key, value in raw.items():
            if value is None:
                continue
            metadata[key] = {"stringValue": str(value)}

        # boto3 create_event is synchronous I/O; offload to a thread so the
        # MCP server's asyncio event loop is not blocked during the HTTP
        # round-trip. Without this, every await observe(...) freezes the
        # loop for the duration of the AWS call.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._data.create_event(
                memoryId=self._cfg.episodic.memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(UTC),
                payload=[
                    {
                        "conversational": {
                            "role": "USER",
                            "content": {"text": content},
                        }
                    }
                ],
                metadata=metadata,
            ),
        )
        return response["event"]["eventId"]

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
        """Bucketed reflection retrieval matching ReflectionSynthesisService.retrieve_reflections.

        One list_memory_records per reflections namespace (project +
        general — the promoted/cross-project merge, mirroring the sqlite
        ``project = ? OR scope = 'general'`` clause), parse JSON content,
        bucket by polarity CLIENT-SIDE (live AWS rejects ``polarity`` as a
        metadata filter key — only the CreateMemory indexedKeys are legal),
        rank via the SHARED Wilson ordering (``services/scoring.py``):
        ``wilson_lower_bound(useful+overlooked, useful+overlooked+ignored)``
        DESC, confidence DESC, updated_at DESC — identical formula/tiebreaks
        to sqlite's ``ReflectionSynthesisService.retrieve_reflections``,
        including its per-bucket reserved exploration slot for under-rated
        candidates (``EXPLORATION_RATED_FLOOR``).
        Returns dict[polarity, list[reflection_dict]] in the same shape sqlite mode
        returns; the MCP memory.retrieve handler json-dumps this directly to Claude.

        ``query``, when non-blank, is fused into the ordering above via
        server-side semantic search: ``_relevance_rank_map`` calls
        ``retrieve_memory_records`` PER namespace (project + general/
        promoted — same fan-out the Wilson fetch uses), the per-namespace
        results are merged (``_merge_relevance_rank_maps``), and the merged
        rank is RRF-combined with the Wilson rank (constant 60) — see
        ``_fetch_reflection_buckets`` for the fusion formula. An empty/
        failed lookup or a blank query leaves ordering as pure Wilson
        (today's Task-4 behaviour).

        track_exposure: when True (default) and a local exposure ledger is
        available (``self._local_conn`` — see ``record_exposures``), writes
        a ``source='retrieve'`` row per returned reflection id, tagging the
        slot-taker(s) as ``via_exploration``. Best-effort: never raises, and
        never affects the returned buckets. Set False to suppress (mirrors
        sqlite's ``track_exposure`` contract, used by callers that manage
        their own exposure tracking).
        """
        actor_id = resolve_actor_id(project or self._project)
        effective_limit = limit_per_bucket if limit_per_bucket is not None else 20
        exploration_ids: set[str] = set()
        buckets = self._fetch_reflection_buckets(
            actor_id=actor_id,
            limit_per_bucket=effective_limit,
            tech=tech,
            phase=phase,
            polarity=polarity if polarity in _POLARITIES else None,
            exploration_ids=exploration_ids,
            query=query,
        )
        if track_exposure:
            self._record_retrieve_exposures(buckets, exploration_ids)
        return buckets

    def _record_retrieve_exposures(
        self,
        buckets: dict[str, list[dict[str, Any]]],
        exploration_ids: set[str],
    ) -> None:
        """Best-effort ``source='retrieve'`` exposure write to the local
        ledger (see ``record_exposures``'s docstring for the shared-ledger
        rationale). No-op when no local ledger is wired or no session id
        resolves; never raises and never affects the returned buckets."""
        if self._local_conn is None:
            return
        try:
            sid = self._require_session_id("retrieve")
        except ValueError:
            return
        if not sid:
            return
        all_ids = [r["id"] for bucket in buckets.values() for r in bucket]
        if not all_ids:
            return
        try:
            from better_memory.services import exposure_log

            exposure_log.record(
                self._local_conn,
                session_id=sid,
                items=[("reflection", rid) for rid in all_ids],
                source="retrieve",
                now=datetime.now(UTC).isoformat(),
                exploration_ids=frozenset(exploration_ids),
            )
            self._local_conn.commit()
        except Exception:  # noqa: BLE001 - exposures must never block retrieve
            pass

    def _fetch_reflection_buckets(
        self,
        *,
        actor_id: str,
        limit_per_bucket: int,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        exploration_ids: set[str] | None = None,
        query: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Two-namespace reflection fan-out shared by retrieve() and
        session_bootstrap().

        Wire shape (live-verified dialect, aws_record_dialect.md):

        * NO ``metadataFilters`` — ``polarity`` is not a legal filter key
          (only the indexedKeys status / last_credited_at /
          overlooked_count are), and a server-side ``status`` EQUALS_TO
          filter would hide AgentCore-extracted reflections that carry no
          status metadata at all. All polarity/status filtering happens
          client-side on the parsed records.
        * One call per namespace: ``projects/{actor}/reflections/`` and
          ``general/reflections/`` (promoted reflections move to general/
          with status=promoted — without this second call they become
          invisible). The general call is skipped when actor == general
          (same namespace).

        Client-side status semantics (mirrors sqlite's
        ``(project = ? OR scope='general') AND status IN (...)``):
        project namespace admits status == active; the general namespace
        admits active AND promoted. Records with no status metadata parse
        as active; records with no/unknown polarity bucket as neutral —
        AgentCore's own extraction strategy writes neither key, and those
        records must still be retrievable. Dedup by record id across the
        two calls (a just-promoted record can appear in both while the
        list index lags), project namespace winning.

        Ranking: shared Wilson ordering (``services/scoring.py``) with the
        same per-bucket exploration slot as sqlite's
        ``ReflectionSynthesisService.retrieve_reflections`` — see the sort
        loop below. ``exploration_ids``, when passed, is populated in place
        with the id of every reflection that took the reserved slot in any
        bucket (used by ``retrieve()`` to tag exposure rows).

        ``query``, when non-blank, fetches a server-side semantic rank map
        (``_relevance_rank_map``, called once PER namespace over the same
        project + general/promoted fan-out as the Wilson fetch above, then
        merged via ``_merge_relevance_rank_maps``) BEFORE the per-bucket
        fill, then RRF-fuses it with the Wilson order per bucket: ``score =
        1/(60 + wilson_rank) + 1/(60 + relevance_rank)``, where
        ``wilson_rank`` is the item's position in
        the just-computed Wilson order and a missing relevance leg (record
        absent from the rank map) contributes nothing. The two-pass
        exploration fill below then runs over this fused order, so the
        reserved slot is chosen from fused-order untested candidates. An
        empty/failed rank map (or blank query) leaves the fusion step a
        no-op — pure Wilson order, unchanged from Task 4.
        """
        project_ns = resolve_namespace(actor_id, "reflections")
        general_ns = resolve_namespace("general", "reflections")
        allowed_general = frozenset({"active", "promoted"})
        allowed_project = (
            allowed_general if actor_id == "general" else frozenset({"active"})
        )
        namespaces: list[tuple[str, frozenset[str]]] = [
            (project_ns, allowed_project)
        ]
        if general_ns != project_ns:
            namespaces.append((general_ns, allowed_general))
        # Scale the fetch budget with the bucket count. The real service
        # caps maxResults at 100 (live ValidationException above that), so
        # page with nextToken until the budget is met or the index is dry.
        max_results = limit_per_bucket * len(_POLARITIES) * 2

        def _fetch(namespace: str) -> list[dict[str, Any]]:
            summaries: list[dict[str, Any]] = []
            token: str | None = None
            while len(summaries) < max_results:
                kwargs: dict[str, Any] = {
                    "memoryId": self._cfg.episodic.memory_id,
                    "namespace": namespace,
                    "maxResults": min(100, max_results - len(summaries)),
                }
                if token:
                    kwargs["nextToken"] = token
                response = self._data.list_memory_records(**kwargs)
                summaries.extend(response.get("memoryRecordSummaries", []))
                token = response.get("nextToken")
                if not token:
                    break
            return summaries

        # Parallel fan-out via a thread pool. This path is sync but is called
        # from inside the MCP server's async `_call_tool`, so we cannot use
        # asyncio.run here (would raise "event loop is already running").
        # ThreadPoolExecutor gives the same wire-level parallelism without
        # touching the outer event loop.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(namespaces)
        ) as pool:
            futures = [
                (allowed, pool.submit(_fetch, namespace))
                for namespace, allowed in namespaces
            ]
            fetched = [(allowed, future.result()) for allowed, future in futures]

        buckets: dict[str, list[dict[str, Any]]] = {p: [] for p in _POLARITIES}
        seen_ids: set[str] = set()
        for allowed_statuses, summaries in fetched:
            for rec in summaries:
                parsed = self._parse_reflection_record(
                    rec, tech_filter=tech, phase_filter=phase
                )
                if parsed is None:
                    continue
                if parsed["id"] in seen_ids:
                    continue
                if parsed["_status"] not in allowed_statuses:
                    continue
                bucket = parsed["_polarity"]
                if polarity is not None and bucket != polarity:
                    continue
                seen_ids.add(parsed["id"])
                buckets[bucket].append(parsed)

        # Local import: EXPLORATION_RATED_FLOOR lives in the reflection
        # synthesis service (services/reflection.py), which pulls in
        # sqlite_vec and friends — keep that off agentcore's module-level
        # import path (mirrors the local exposure_log imports elsewhere in
        # this class).
        from better_memory.services.reflection import EXPLORATION_RATED_FLOOR

        # Only worth reserving an exploration slot when there's room for at
        # least one proven row alongside it — mirrors reflection.py's own
        # `reserve` gate (cap < 2 means the untested row would consume the
        # entire bucket).
        reserve = limit_per_bucket >= 2

        # Server-side semantic rank map for RRF fusion, fetched ONCE per
        # namespace (not per-bucket — one retrieve_memory_records call per
        # namespace covers every polarity) BEFORE the per-bucket fill so the
        # exploration fill below operates on the fused order rather than
        # pure Wilson. Design spec (2026-07-24-agentcore-parity-design.md
        # §3): RetrieveMemoryRecords is called PER namespace — same
        # project + general/promoted fan-out the Wilson fetch above already
        # uses (`namespaces`) — so a promoted/general-scope reflection gets
        # the same relevance boost as a project-scoped one. Each namespace
        # call is independently best-effort (`_relevance_rank_map` never
        # raises); one namespace failing still lets the other's results
        # through the merge. Blank query or every namespace failing -> the
        # merge returns None -- Task 5's own fusion contract collapses that
        # to {} here (a no-op fusion step, degrading to pure Wilson order);
        # this is DIFFERENT from Task 6's relevance_ranks, which surfaces
        # the None/{} distinction to its caller instead of collapsing it —
        # see relevance_ranks below.
        rank_map: dict[str, int] = {}
        if query is not None and query.strip():
            q = query.strip()
            rank_map = self._merge_relevance_rank_maps(
                [self._relevance_rank_map(q, ns) for ns, _ in namespaces]
            ) or {}

        for bucket_name, items in buckets.items():
            # Shared Wilson ordering (services/scoring.py): the lower bound
            # of the Wilson score interval on useful+overlooked / rated,
            # then confidence, then recency — identical formula/tiebreaks to
            # sqlite's ReflectionSynthesisService.retrieve_reflections.
            items.sort(
                key=lambda r: (
                    -wilson_lower_bound(
                        r["useful_count"] + r["times_overlooked"],
                        r["useful_count"] + r["times_overlooked"] + r["times_ignored"],
                    ),
                    -r["confidence"],
                    -r["_updated_at_ts"],
                )
            )

            if rank_map:
                # RRF fusion (constant 60): score = 1/(60 + wilson_rank) +
                # 1/(60 + relevance_rank); a record absent from rank_map
                # contributes nothing for the relevance leg. `items` is
                # already Wilson-ordered above, so enumerating it supplies
                # wilson_rank directly, and the sort below is stable — equal
                # scores (e.g. neither leg present) keep their Wilson order.
                items = [
                    r
                    for _, r in sorted(
                        enumerate(items),
                        key=lambda pair: -(
                            1.0 / (60 + pair[0])
                            + (
                                1.0 / (60 + rank_map[pair[1]["id"]])
                                if pair[1]["id"] in rank_map
                                else 0.0
                            )
                        ),
                    )
                ]

            if not reserve:
                chosen = items[:limit_per_bucket]
            else:
                # Two-pass fill (reflection.py parity): cap-1 best TESTED
                # rows in ranked order, one reserved slot for the best
                # UNTESTED row (rated < EXPLORATION_RATED_FLOOR) if any,
                # else top up from the remainder — preserving ranked order
                # via an index sort at the end.
                def _rated(r: dict[str, Any]) -> int:
                    return (
                        r["useful_count"] + r["times_overlooked"]
                        + r["times_ignored"]
                    )

                tested_idx = [
                    i for i, r in enumerate(items)
                    if _rated(r) >= EXPLORATION_RATED_FLOOR
                ]
                untested_idx = [
                    i for i, r in enumerate(items)
                    if _rated(r) < EXPLORATION_RATED_FLOOR
                ]
                chosen_idx = tested_idx[: limit_per_bucket - 1]
                if untested_idx:
                    chosen_idx.append(untested_idx[0])
                    if exploration_ids is not None:
                        exploration_ids.add(items[untested_idx[0]]["id"])
                if len(chosen_idx) < limit_per_bucket:  # top up from the remainder
                    taken = set(chosen_idx)
                    for i in range(len(items)):
                        if len(chosen_idx) >= limit_per_bucket:
                            break
                        if i not in taken:
                            chosen_idx.append(i)
                chosen_idx.sort()  # preserve ranked order
                chosen = [items[i] for i in chosen_idx]

            # Strip the internal ranking/bucketing helpers — the payload
            # must be key-identical to the sqlite reflection dicts.
            buckets[bucket_name] = [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in chosen
            ]
        return buckets

    def _relevance_rank_map(
        self,
        query: str,
        namespace: str,
        top_k: int = 50,
        *,
        memory_id: str | None = None,
    ) -> dict[str, int] | None:
        """Server-side semantic search rank map for RRF fusion with the
        shared Wilson ordering (see ``_fetch_reflection_buckets``), and —
        via ``relevance_ranks`` — with the contextual evidence gate in
        ``services/relevant.py``.

        Same call shape as ``semantic_list``'s search path
        (``retrieve_memory_records`` with ``searchCriteria={"searchQuery",
        "topK"}``). ``memory_id`` defaults to the episodic (reflections)
        memory when omitted, matching every existing caller
        (``_fetch_reflection_buckets``); ``relevance_ranks`` passes the
        semantic memory's id explicitly for "semantic"-kind lookups.
        Returns ``{memoryRecordId: rank}`` from result order (0 = best
        match) on success -- including a genuinely empty ``{}`` when the
        call succeeds but nothing matches. Best-effort: any exception (AWS
        error, malformed response, ...) degrades to ``None`` -- NOT ``{}``
        -- so callers can distinguish "this namespace's lookup failed"
        from "it ran and found nothing" (``_merge_relevance_rank_maps``
        skips ``None`` legs when merging; ``relevance_ranks`` surfaces the
        distinction to its own caller, ``retrieve_relevant``'s keyword-
        fallback gate). Never raises."""
        try:
            response = self._data.retrieve_memory_records(
                memoryId=memory_id or self._cfg.episodic.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": query,
                    "topK": top_k,
                },
            )
            return {
                rec["memoryRecordId"]: rank
                for rank, rec in enumerate(response.get("memoryRecordSummaries", []))
                if "memoryRecordId" in rec
            }
        except Exception:  # noqa: BLE001 - best-effort; None signals "lookup failed"
            return None

    def _merge_relevance_rank_maps(
        self, rank_maps: list[dict[str, int] | None]
    ) -> dict[str, int] | None:
        """Merge one ``_relevance_rank_map`` result per namespace (see the
        call site in ``_fetch_reflection_buckets``, which fans
        ``_relevance_rank_map`` out over every namespace the Wilson fetch
        itself uses — project + general/promoted) into a single global rank
        map.

        ``None`` legs (a namespace whose lookup itself failed — see
        ``_relevance_rank_map``) are skipped when merging: one namespace
        failing still lets a successful sibling's results through. Only
        when EVERY leg is ``None`` does this method itself return ``None``
        — propagating "the whole lookup failed" up to the caller, which
        for ``relevance_ranks`` is the signal that distinguishes an AWS
        error from a legitimate empty result. A leg list that is entirely
        empty dicts (every namespace ran fine, none matched) correctly
        returns ``{}``, not ``None``.

        RetrieveMemoryRecords' response shape has no ``score`` field
        verified anywhere in this codebase's fixtures/docs (only prose
        mentions of a "cosine score" in the design doc) — rather than parse
        an unconfirmed key, this interleaves each namespace's already
        rank-ordered id list round-robin (best-of-namespace-1, best-of-
        namespace-2, next-of-namespace-1, ...) and assigns global ranks
        0..n-1 in that interleaved order. This is a namespace-fair merge
        that needs no cross-namespace score comparison. A duplicate id (a
        just-promoted record can transiently appear in both namespaces,
        mirroring the id-dedup in ``_fetch_reflection_buckets``) keeps its
        FIRST (better) global rank."""
        succeeded = [rank_map for rank_map in rank_maps if rank_map is not None]
        if not succeeded:
            return None
        ordered_ids = [
            sorted(rank_map, key=lambda rid: rank_map[rid]) for rank_map in succeeded
        ]
        merged: dict[str, int] = {}
        cursors = [0] * len(ordered_ids)
        advanced = True
        while advanced:
            advanced = False
            for i, ids in enumerate(ordered_ids):
                if cursors[i] < len(ids):
                    advanced = True
                    rid = ids[cursors[i]]
                    cursors[i] += 1
                    if rid not in merged:
                        merged[rid] = len(merged)
        return merged

    def relevance_ranks(
        self,
        *,
        query: str,
        kinds: tuple[str, ...] = ("reflection", "semantic"),
        top_k: int = 50,
    ) -> dict[tuple[str, str], int] | None:
        """Server-side relevance rank map for the contextual evidence gate
        in ``services/relevant.py`` (``retrieve_relevant``'s agentcore
        branch, gated on ``conn is None`` + ``supports_synthesis is
        False``).

        Reuses the same fan-out/merge machinery ``_fetch_reflection_
        buckets`` uses for its own Wilson/relevance RRF fusion
        (``_relevance_rank_map`` + ``_merge_relevance_rank_maps``), applied
        per requested kind against that kind's own memory + namespace
        pair: "reflection" -> the episodic memory's
        projects/{actor}/reflections/ + general/reflections/ namespaces
        (mirroring ``retrieve``'s own fan-out); "semantic" -> the semantic
        memory's projects/{actor}/semantic/ + general/semantic/ namespaces
        (mirroring ``semantic_list``'s namespace resolution). The two
        namespace results are merged via ``_merge_relevance_rank_maps``
        (namespace-fair round robin) into one rank map per kind, then
        combined into the returned ``(kind, id) -> rank`` map — ranks are
        only ever compared within their own kind by the caller, never
        across kinds.

        The ``None`` vs ``{}`` distinction is load-bearing (see the
        Protocol docstring): a kind whose EVERY namespace lookup fails
        contributes nothing and is treated as failed; a kind with at
        least one successful namespace lookup contributes its (possibly
        empty) results normally. Blank query, or EVERY requested kind
        failing that way, returns ``None`` / ``{}`` respectively — see
        below. A blank query short-circuits to ``{}`` (nothing was
        searched, not an error) without any wire call."""
        if not query or not query.strip():
            return {}
        q = query.strip()
        actor_id = resolve_actor_id(self._project)

        out: dict[tuple[str, str], int] = {}
        any_kind_succeeded = False
        for kind in kinds:
            if kind == "reflection":
                memory_id = self._cfg.episodic.memory_id
                ns_kind = "reflections"
            elif kind == "semantic":
                memory_id = self._cfg.semantic.memory_id
                ns_kind = "semantic"
            else:
                continue

            project_ns = resolve_namespace(actor_id, ns_kind)
            general_ns = resolve_namespace("general", ns_kind)
            namespaces = [project_ns]
            if general_ns != project_ns:
                namespaces.append(general_ns)

            merged = self._merge_relevance_rank_maps([
                self._relevance_rank_map(q, ns, top_k, memory_id=memory_id)
                for ns in namespaces
            ])
            if merged is None:
                # Every namespace call for this kind failed -- this kind
                # contributes nothing (not even an empty-result entry);
                # any_kind_succeeded stays whatever the other kinds set it
                # to, so one kind failing doesn't blank out a sibling
                # kind's genuine results.
                continue
            any_kind_succeeded = True
            for rid, rank in merged.items():
                out[(kind, rid)] = rank
        return out if any_kind_succeeded else None

    def _parse_reflection_record(
        self,
        rec: dict[str, Any],
        *,
        tech_filter: str | None = None,
        phase_filter: str | None = None,
    ) -> dict[str, Any] | None:
        """Map MemoryRecordSummary -> sqlite-shaped reflection dict.

        Returns None if tech_filter / phase_filter excludes this record.

        Underscore-prefixed keys are internal (ranking + client-side
        polarity/status bucketing); _fetch_reflection_buckets strips them
        before the payload leaves the backend."""
        text = rec.get("content", {}).get("text", "")
        try:
            body = json.loads(text) if isinstance(text, str) else {}
        except json.JSONDecodeError:
            body = {}

        metadata_raw = rec.get("metadata", {})
        # Migrated (SQLite-origin) records carry all reflection state in the
        # JSON content body; AWS-extracted records carry it in metadata (and
        # never in the body). Every field below therefore resolves BODY-FIRST
        # with a METADATA FALLBACK: a body without the key is exactly the
        # AWS-extracted shape, so the fallback reproduces today's behavior
        # byte-for-byte (no regression). See migration design §1b/§6.
        body_dict: dict[str, Any] = body if isinstance(body, dict) else {}

        def _num(key: str) -> float:
            return float(metadata_raw.get(key, {}).get("numberValue", 0))

        def _count_body_first(body_key: str, meta_key: str) -> int:
            """Integer counter, body value preferred over metadata numberValue.

            Body absent (or non-numeric) -> the existing metadata numberValue
            path, preserving AWS-extracted-record behavior exactly."""
            body_val = body_dict.get(body_key)
            if body_val is not None:
                try:
                    return int(body_val)
                except (TypeError, ValueError):
                    pass
            return int(_num(meta_key))

        tech_value: str | None = body.get("tech") if isinstance(body, dict) else None
        if tech_filter is not None and tech_value != tech_filter:
            return None

        phase_value = body.get("phase", "general") if isinstance(body, dict) else "general"
        if phase_filter is not None and phase_value != phase_filter:
            return None

        hints_value = body.get("hints", "") if isinstance(body, dict) else ""
        hints_list = (
            parse_hints_prose(hints_value) if isinstance(hints_value, str)
            else list(hints_value) if isinstance(hints_value, list)
            else []
        )

        try:
            confidence = float(body.get("confidence", 0)) if isinstance(body, dict) else 0.0
        except (TypeError, ValueError):
            confidence = 0.0

        # updated_at, body-first (§6.2). Migrated records carry an ISO-8601
        # string in the body; AWS-extracted records have NO updatedAt on the
        # record shape (createdAt only) — recency lives in the system metadata
        # key x-amz-agentcore-memory-updatedAt (dateTimeValue), falling back to
        # createdAt. Body absent -> identical to the pre-migration path.
        body_updated = body_dict.get("updated_at")
        sys_updated = metadata_raw.get(
            f"{_AGENTCORE_SYSTEM_METADATA_PREFIX}updatedAt", {}
        ).get("dateTimeValue")
        updated_at_raw = (
            body_updated
            or sys_updated
            or rec.get("updatedAt")
            or rec.get("createdAt")
        )
        if isinstance(updated_at_raw, datetime):
            updated_at_value: str | None = updated_at_raw.isoformat()
            updated_at_ts = updated_at_raw.timestamp()
        elif isinstance(updated_at_raw, str):
            updated_at_value = updated_at_raw
            try:
                updated_at_ts = datetime.fromisoformat(updated_at_raw).timestamp()
            except ValueError:
                updated_at_ts = 0.0
        else:
            updated_at_value = None
            updated_at_ts = 0.0

        # Client-side bucketing inputs. Missing/unknown polarity buckets as
        # neutral and missing status parses as active — AgentCore's own
        # extraction strategy writes neither key, and those records must
        # still be retrievable (see _fetch_reflection_buckets).
        # polarity is the bucket selector and is CRITICAL for migrated
        # records — they carry it only in the body. Body-first, metadata
        # fallback (AWS-extracted records carry neither -> neutral).
        polarity_value = (
            body_dict.get("polarity")
            or metadata_raw.get("polarity", {}).get("stringValue")
        )
        if polarity_value not in _POLARITIES:
            polarity_value = "neutral"
        status_value = (
            body_dict.get("status")
            or metadata_raw.get("status", {}).get("stringValue")
            or "active"
        )

        # evidence_count body-first (§6.1). Prefer the stored body value
        # (migrated, synthesis-recomputed source count); else the existing
        # computed metadata useful+missed fallback (AWS-extracted records).
        body_ec = body_dict.get("evidence_count")
        if body_ec is not None:
            try:
                evidence_count = int(body_ec)
            except (TypeError, ValueError):
                evidence_count = int(_num("useful_count")) + int(_num("missed_count"))
        else:
            evidence_count = int(_num("useful_count")) + int(_num("missed_count"))

        overlooked_count = _count_body_first("times_overlooked", "overlooked_count")

        return {
            # Public shape — must match ReflectionSynthesisService.retrieve_reflections
            # return: {id, title, phase, use_cases, hints (list), confidence (float),
            #          tech, evidence_count, useful_count, times_overlooked,
            #          times_ignored, times_misled, updated_at}
            "id": rec["memoryRecordId"],
            "title": body.get("title", "") if isinstance(body, dict) else "",
            "phase": phase_value,
            "use_cases": body.get("use_cases", "") if isinstance(body, dict) else "",
            "hints": hints_list,
            "confidence": confidence,
            "tech": tech_value,
            "evidence_count": evidence_count,
            "useful_count": _count_body_first("useful_count", "useful_count"),
            # Copied from the internal ranking counter below so the Wilson
            # prior in services/relevant.py sees the real overlooked count
            # instead of silently defaulting to 0 (PR #84 review).
            "times_overlooked": overlooked_count,
            "times_ignored": _count_body_first("ignored_count", "ignored_count"),
            "times_misled": _count_body_first("times_misled", "times_misled"),
            "updated_at": updated_at_value,
            # Internal ranking/bucketing helpers — stripped by
            # _fetch_reflection_buckets before the payload leaves the backend.
            "_overlooked_count": overlooked_count,
            "_updated_at_ts": updated_at_ts,
            "_polarity": polarity_value,
            "_status": status_value,
        }

    def _list_records_paginated(
        self, namespace: str, max_results: int = 200
    ) -> list[dict[str, Any]]:
        """Page ``list_memory_records`` for one namespace until either
        ``max_results`` rows have been collected or the namespace's index
        is exhausted. Mirrors the inner ``_fetch`` closure in
        ``_fetch_reflection_buckets`` (same 100-row-per-call cap, same
        nextToken loop) as a reusable method for callers -- like
        ``reflection_list`` -- that need it outside that closure's scope."""
        summaries: list[dict[str, Any]] = []
        token: str | None = None
        while len(summaries) < max_results:
            kwargs: dict[str, Any] = {
                "memoryId": self._cfg.episodic.memory_id,
                "namespace": namespace,
                "maxResults": min(100, max_results - len(summaries)),
            }
            if token:
                kwargs["nextToken"] = token
            response = self._data.list_memory_records(**kwargs)
            summaries.extend(response.get("memoryRecordSummaries", []))
            token = response.get("nextToken")
            if not token:
                break
        return summaries

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
        """Flat, Wilson-ordered reflection list for the UI panel.

        Fans out ``list_memory_records`` over the reflections namespaces
        (project + general, same pair ``retrieve`` uses) and, only when the
        resolved status set admits ``"retired"``, the retired namespaces too
        -- querying retired/ unconditionally would waste a wire call on
        every default-view page load. Each summary is parsed via
        ``_parse_reflection_record`` (the same sqlite-shaped mapping
        ``retrieve``/``reflection_get`` use), deduped by id (project
        namespace wins, mirroring ``_fetch_reflection_buckets``), then
        filtered client-side on tech/phase (inside the parse call),
        polarity/min_confidence/useful_only/status-set membership.

        Status remap: agentcore's vocabulary is active/promoted/retired
        (no pending_review). ``status=None`` resolves to ``{"active",
        "promoted"}`` -- sqlite's own default set (``pending_review``,
        ``confirmed``) lives entirely inside ``queries.reflection_list_for_ui``
        and is never used here; the two defaults are intentionally
        different views of "the live/active reflections" for their
        respective status vocabularies.

        Ordering: shared Wilson lower bound (``services/scoring.py``) on
        (useful+overlooked)/(useful+overlooked+ignored) DESC, confidence
        DESC, updated_at DESC -- identical formula/tiebreaks to
        ``retrieve``'s per-bucket ordering, just flattened across polarity
        instead of bucketed.

        Best-effort per namespace: a namespace whose ``list_memory_records``
        call raises is skipped (degrade, not a 500) -- the surviving
        namespaces' rows are still returned."""
        actor_id = resolve_actor_id(project or self._project)
        wanted = {"active", "promoted"} if status is None else {status}

        refl_project = resolve_namespace(actor_id, "reflections")
        refl_general = resolve_namespace("general", "reflections")
        namespaces = [refl_project]
        if refl_general != refl_project:
            namespaces.append(refl_general)
        if "retired" in wanted:
            ret_project = resolve_namespace(actor_id, "retired")
            ret_general = resolve_namespace("general", "retired")
            namespaces.append(ret_project)
            if ret_general != ret_project:
                namespaces.append(ret_general)

        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for namespace in namespaces:
            try:
                summaries = self._list_records_paginated(namespace, max_results=limit * 2)
            except Exception:  # noqa: BLE001 - best-effort; one namespace failing must not 500
                continue
            for rec in summaries:
                parsed = self._parse_reflection_record(rec, tech_filter=tech, phase_filter=phase)
                if parsed is None or parsed["id"] in seen:
                    continue
                if parsed["_status"] not in wanted:
                    continue
                if polarity is not None and parsed["_polarity"] != polarity:
                    continue
                if parsed["confidence"] < min_confidence:
                    continue
                if useful_only and parsed["useful_count"] <= 0:
                    continue
                seen.add(parsed["id"])
                rows.append(parsed)

        rows.sort(key=lambda r: (
            -wilson_lower_bound(
                r["useful_count"] + r["times_overlooked"],
                r["useful_count"] + r["times_overlooked"] + r["times_ignored"],
            ),
            -r["confidence"],
            -r["_updated_at_ts"],
        ))
        resolved_project = project or self._project
        return [
            {
                "id": r["id"], "title": r["title"], "project": resolved_project,
                "tech": r["tech"], "phase": r["phase"], "polarity": r["_polarity"],
                "confidence": r["confidence"], "status": r["_status"],
                "use_cases": r["use_cases"], "evidence_count": r["evidence_count"],
                "updated_at": r["updated_at"], "useful_count": r["useful_count"],
                "times_misled": r["times_misled"], "times_overlooked": r["times_overlooked"],
            }
            for r in rows[:limit]
        ]

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
        """List raw events from the CURRENT session as observations. Cross-
        session enumeration is deferred (ListEvents requires sessionId).
        Same lazy session-id re-resolution contract as ``observe``."""
        session_id = self._require_session_id("list_observations")
        actor_id = resolve_actor_id(project or self._project)

        # boto3 list_events is synchronous I/O; offload to a thread so the
        # MCP server's asyncio event loop is not blocked during the HTTP
        # round-trip. Post-filter / mapping below is pure dict work and
        # stays inline.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._data.list_events(
                memoryId=self._cfg.episodic.memory_id,
                actorId=actor_id,
                sessionId=session_id,
                # Real service caps maxResults at 100.
                maxResults=min(limit, 100),
                includePayloads=True,
            ),
        )

        results: list[dict[str, Any]] = []
        for event in response.get("events", []):
            payload_text = ""
            for block in event.get("payload", []):
                conv = block.get("conversational")
                if conv:
                    payload_text = conv.get("content", {}).get("text", "")
                    break

            flat_metadata = {
                k: v.get("stringValue") for k, v in event.get("metadata", {}).items()
            }

            event_ts = event.get("eventTimestamp")
            results.append(
                {
                    # Key parity with the sqlite rows list_observations
                    # returns ({id, content, component, theme, outcome,
                    # reinforcement_score, created_at}) — no extra
                    # agentcore-only keys leak to the MCP payload.
                    # reinforcement_score has no event-plane equivalent;
                    # stable None placeholder (same convention as the
                    # semantic_retrieve UD-2 payload contract).
                    "id": event["eventId"],
                    "content": payload_text,
                    "component": flat_metadata.get("component"),
                    "theme": flat_metadata.get("theme"),
                    "outcome": flat_metadata.get("outcome"),
                    "reinforcement_score": None,
                    "created_at": (
                        event_ts.isoformat()
                        if isinstance(event_ts, datetime)
                        else event_ts
                    ),
                }
            )

        # Apply post-filter for theme/component/outcome since ListEvents.filter
        # surface is limited to branch/eventType — not the per-event metadata
        # keys we set. Filter client-side.
        if theme is not None:
            results = [r for r in results if r.get("theme") == theme]
        if component is not None:
            results = [r for r in results if r.get("component") == component]
        if outcome is not None:
            results = [r for r in results if r.get("outcome") == outcome]
        if query is not None and query.strip():
            q = query.lower()
            results = [r for r in results if q in r.get("content", "").lower()]
        return results

    def _get_record(self, record_id: str) -> dict[str, Any]:
        """Fetch a single memory record from the EPISODIC memory.

        Tries get_memory_record first; falls back to list_memory_records
        with metadataFilters if the record is a BASE record (spike Finding 3:
        get-memory-record returns 404 for BASE records). For our use
        (record_use against extracted reflections), get_memory_record is
        the right call — BASE records aren't ratable."""
        return self._data.get_memory_record(
            memoryId=self._cfg.episodic.memory_id,
            memoryRecordId=record_id,
        )["memoryRecord"]

    def _retry_on_transient_404(
        self,
        call: Callable[[], dict[str, Any]],
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Retry a boto3 call on transient ResourceNotFoundException.

        Live-AWS smoke (spec § "Live-AWS smoke findings") showed that
        ``batch_update_memory_records`` issued immediately after
        ``batch_create_memory_records`` can fail with ResourceNotFoundException
        on the first attempt and succeed on the second. Lag is ~10s typical.

        Permanent 404s (the record really doesn't exist) will still raise
        after ``max_attempts``."""
        for attempt in range(1, max_attempts + 1):
            try:
                return call()
            except _ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ResourceNotFoundException" or attempt == max_attempts:
                    raise
                time.sleep(backoff_seconds)
        # Unreachable but satisfies type checker
        raise RuntimeError("retry loop exited unexpectedly")

    def _full_metadata_snapshot(
        self, current: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Produce a full metadata snapshot for BatchUpdateMemoryRecords.

        Two rules baked in here:

        1. **Merge-vs-replace semantics are undetermined** (spike Finding 5);
           always send the FULL snapshot.

        2. **Strip system-managed keys** before sending. ``list_memory_records``
           and ``get_memory_record`` responses include
           ``x-amz-agentcore-memory-{createdAt,updatedAt,recordType}`` mixed
           into the returned metadata dict. If echoed back via
           ``batch_update_memory_records``, AWS rejects the call with
           ``code 400 — Metadata keys cannot use reserved names or prefixes``.
           Caught by the live-AWS smoke; see spec § "Live-AWS smoke findings".

        ``current`` is the existing metadata dict from MemoryRecord;
        ``updates`` is the diff to apply (already in the wire-shape dict form
        with stringValue/numberValue/etc.)."""
        snapshot = {
            k: v for k, v in current.items()
            if not k.startswith(_AGENTCORE_SYSTEM_METADATA_PREFIX)
        }
        snapshot.update(updates)
        return snapshot

    @staticmethod
    def _record_body(record: dict[str, Any]) -> dict[str, Any]:
        """Parse a memory record's JSON content body into a dict.

        Returns ``{}`` for non-JSON / non-object bodies. Migrated
        (SQLite-origin) records carry ``source_backend='sqlite'`` here; the
        rating paths use that marker to decide whether reflection state lives
        in the content body (migrated) or in metadata (AWS-extracted)."""
        text = record.get("content", {}).get("text", "")
        try:
            body = json.loads(text) if isinstance(text, str) else {}
        except json.JSONDecodeError:
            body = {}
        return body if isinstance(body, dict) else {}

    def _credit_body_counter(
        self,
        *,
        memory_id: str,
        record_id: str,
        body: dict[str, Any],
        counter_key: str,
    ) -> dict[str, Any]:
        """Read-modify-write a migrated record's JSON content BODY: bump the
        counter, refresh ``last_credited_at``, and persist via a CONTENT update.

        ``updated_at`` is deliberately NOT refreshed here. SQLite crediting
        (``memory_rating.py``) bumps only the counter + ``last_*_at`` and never
        ``reflections.updated_at``; because the reader resolves ``updated_at``
        body-first (design §6.2), bumping it here would drift a migrated
        reflection's synthesis-time ``updated_at`` forward on every rating,
        corrupting ``age_days`` (relevant.py) and the ``updated_at DESC``
        ranking tiebreak relative to the SQLite source — the exact parity §6.2
        exists to preserve. ``_mutate_namespace_and_status`` DOES bump
        ``updated_at`` on purpose: sqlite promote/retire bump it too, so that
        path is correct parity; only rating must leave it untouched.

        AWS silently drops the custom metadata map on client-authored BASE
        records in the episodic reflections namespace (design §1b, proven
        live), so a metadata write is a no-op there — rating state must live in
        the content body, which DOES persist. The metadata counter key is
        translated to its body field name (``overlooked_count`` →
        ``times_overlooked``; all others unchanged).

        Concurrency: last-writer-wins. ``body`` was just read via
        ``get_memory_record`` (read-your-write), and the data plane exposes no
        conditional / optimistic-concurrency update, so a concurrent crediter's
        increment can be lost. Acceptable for the low-contention rating path
        (design §1b, documented)."""
        body_field = _COUNTER_KEY_TO_BODY_FIELD.get(counter_key, counter_key)
        try:
            current = int(body.get(body_field, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        now = datetime.now(UTC).isoformat()
        new_body = dict(body)
        new_body[body_field] = current + 1
        # last_credited_at lives in the body for migrated records (design §1b).
        # updated_at is intentionally left unchanged (see docstring / §6.2).
        new_body["last_credited_at"] = now
        return self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=memory_id,
                records=[
                    {
                        "memoryRecordId": record_id,
                        "timestamp": datetime.now(UTC),
                        "content": {"text": json.dumps(new_body)},
                    }
                ],
            )
        )

    def record_use(
        self, observation_id: str, *, outcome: UseOutcome | None = None
    ) -> None:
        """Credit a record's reinforcement counter. outcome=None is a no-op
        (no classification, no counter change)."""
        if outcome is None:
            return

        record = self._get_record(observation_id)
        counter_key = "useful_count" if outcome == "success" else "missed_count"

        # Migrated (SQLite-origin) reflections carry rating state in the content
        # body; a metadata write is silently dropped by AWS (design §1b).
        # Read-modify-write the body instead. AWS-extracted records (no marker)
        # keep the metadata path below unchanged.
        body = self._record_body(record)
        if body.get("source_backend") == "sqlite":
            response = self._credit_body_counter(
                memory_id=self._cfg.episodic.memory_id,
                record_id=observation_id,
                body=body,
                counter_key=counter_key,
            )
            failed = response.get("failedRecords", [])
            if failed:
                raise RuntimeError(
                    f"AgentCore record_use failed for {observation_id}: "
                    f"{failed[0].get('errorMessage', 'unknown')}"
                )
            return

        metadata = record.get("metadata", {})
        current_count = float(
            metadata.get(counter_key, {}).get("numberValue", 0)
        )

        updates: dict[str, dict[str, Any]] = {
            counter_key: {"numberValue": current_count + 1},
            # last_credited_at is declared STRING in the CreateMemory
            # indexedKeys; a dateTimeValue fails the whole record update
            # ("value type does not match declared indexed key type") —
            # the live root cause of apply_session_ratings applied:0.
            "last_credited_at": {
                "stringValue": datetime.now(UTC).isoformat()
            },
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=self._cfg.episodic.memory_id,
                records=[
                    {
                        "memoryRecordId": observation_id,
                        "timestamp": datetime.now(UTC),
                        "metadata": snapshot,
                    }
                ],
            )
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore record_use failed for {observation_id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    # ----- Semantic memories: Task 11 -----

    def _semantic_initial_metadata(self) -> dict[str, dict[str, Any]]:
        """Initial metadata snapshot for a newly-created semantic record."""
        return {
            "useful_count": {"numberValue": 0},
            "missed_count": {"numberValue": 0},
            "ignored_count": {"numberValue": 0},
            "times_misled": {"numberValue": 0},
            "overlooked_count": {"numberValue": 0},
            "status": {"stringValue": "active"},
        }

    def _get_semantic_record(self, record_id: str) -> dict[str, Any]:
        return self._data.get_memory_record(
            memoryId=self._cfg.semantic.memory_id,
            memoryRecordId=record_id,
        )["memoryRecord"]

    def semantic_observe(
        self,
        *,
        content: str,
        project: str | None = None,
        scope: str = "project",
    ) -> str:
        """Create a semantic memory record. Bypasses LLM extraction —
        the content is the preference text directly, written under the
        userPreferenceMemoryStrategy so AWS applies its schema validation."""
        actor_id = resolve_actor_id(project or self._project)
        if scope == "general":
            namespace = resolve_namespace("general", "semantic")
        else:
            namespace = resolve_namespace(actor_id, "semantic")

        # requestIdentifier: max 80 chars, content-hash for natural dedup
        # if the same preference is observed twice in quick succession.
        req_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:80]

        response = self._data.batch_create_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            records=[
                {
                    "requestIdentifier": req_id,
                    "namespaces": [namespace],
                    "content": {"text": content},
                    "timestamp": datetime.now(UTC),
                    "memoryStrategyId": self._cfg.semantic.strategy_id,
                    "metadata": self._semantic_initial_metadata(),
                }
            ],
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore semantic_observe failed: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )
        successful = response.get("successfulRecords", [])
        if not successful:
            raise RuntimeError(
                f"AgentCore batch_create_memory_records returned no successful "
                f"records; response: {response}"
            )
        return successful[0]["memoryRecordId"]

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        """List semantic records. With search -> retrieve_memory_records;
        without -> list_memory_records.

        scope_filter=None mirrors the sqlite default view
        (project OR scope='general'): fan out over BOTH the project/semantic and
        general/semantic namespaces and dedup by record id (project wins). A
        single-scope filter queries only that one namespace."""
        actor_id = resolve_actor_id(project or self._project)
        project_ns = resolve_namespace(actor_id, "semantic")
        general_ns = resolve_namespace("general", "semantic")
        if scope_filter == "general":
            namespaces = [general_ns]
        elif scope_filter == "project":
            namespaces = [project_ns]
        else:
            namespaces = [project_ns]
            if general_ns != project_ns:
                namespaces.append(general_ns)

        seen: set[str] = set()
        results: list[Any] = []
        for namespace in namespaces:
            if search and search.strip():
                response = self._data.retrieve_memory_records(
                    memoryId=self._cfg.semantic.memory_id,
                    namespace=namespace,
                    searchCriteria={"searchQuery": search.strip(), "topK": 50},
                )
            else:
                response = self._data.list_memory_records(
                    memoryId=self._cfg.semantic.memory_id,
                    namespace=namespace,
                    maxResults=100,
                )
            for rec in response.get("memoryRecordSummaries", []):
                rid = rec.get("memoryRecordId")
                if rid in seen:
                    continue
                seen.add(rid)
                results.append(
                    self._semantic_summary_to_model(rec, project=project or self._project)
                )
        return results

    def _semantic_summary_to_model(
        self, rec: dict[str, Any], *, project: str
    ) -> Any:
        """Map a MemoryRecordSummary -> a SemanticMemory-shaped read model.

        §6.3: relevant.py (retrieve_relevant) reads semantic memories via
        ATTRIBUTE access (getattr(s, 'content'), getattr(s, 'useful_count'),
        ...). Returning plain dicts made every getattr silently resolve to its
        default ('' / 0), so agentcore-mode semantic memories never cleared the
        min-hits floor and semantic injection was dead. Returning the same
        SemanticMemory dataclass the SQLite read model returns (services/
        semantic.py) fixes that: attribute access resolves to real content and
        the declared metadata counters the migrator writes.

        Counters come from the declared numberValue metadata keys
        (useful_count / times_misled / overlooked_count → times_overlooked /
        ignored_count → times_ignored); the collapsed rating timestamp comes
        from last_credited_at stringValue. Absent metadata (AWS-extracted or
        freshly-created records) → zeroed counters, never None."""
        # Local import: the SemanticMemory read model lives in the services
        # layer; importing at module scope would invert the storage→services
        # layering. This is a lightweight frozen dataclass, imported lazily.
        from better_memory.services.semantic import SemanticMemory

        metadata = rec.get("metadata", {}) or {}

        def _count(key: str) -> int:
            raw = metadata.get(key, {})
            if not isinstance(raw, dict):
                return 0
            try:
                return int(raw.get("numberValue", 0) or 0)
            except (TypeError, ValueError):
                return 0

        def _str_meta(key: str) -> str | None:
            raw = metadata.get(key, {})
            if isinstance(raw, dict):
                return raw.get("stringValue")
            return None

        namespaces = rec.get("namespaces") or []
        # Live dialect: stored namespaces gain a leading slash
        # ("/general/semantic/") — normalize before classifying or every
        # read-back record misreports as project scope.
        first_ns = next(iter(namespaces or [""]), "")
        scope = (
            "general"
            if first_ns.lstrip("/").startswith("general/")
            else "project"
        )

        def _iso(value: Any) -> str | None:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, str):
                return value
            return None

        created_at = _iso(rec.get("createdAt")) or ""
        # updated_at drives relevant.py age_days. Prefer the system-managed
        # updatedAt (dateTimeValue), then the record's updatedAt/createdAt.
        sys_updated = metadata.get(
            f"{_AGENTCORE_SYSTEM_METADATA_PREFIX}updatedAt", {}
        )
        sys_updated_val = (
            sys_updated.get("dateTimeValue")
            if isinstance(sys_updated, dict)
            else None
        )
        updated_at = (
            _iso(sys_updated_val)
            or _iso(rec.get("updatedAt"))
            or created_at
        )

        # The three per-class rating timestamps collapsed to one
        # last_credited_at on write (design §3.2); surface it on the
        # useful-at slot as best-effort recency signal.
        last_credited_at = _str_meta("last_credited_at")

        return SemanticMemory(
            id=rec["memoryRecordId"],
            content=rec.get("content", {}).get("text", ""),
            project=project,
            scope=scope,
            created_at=created_at,
            updated_at=updated_at,
            useful_count=_count("useful_count"),
            last_useful_at=last_credited_at,
            times_misled=_count("times_misled"),
            last_misled_at=None,
            times_overlooked=_count("overlooked_count"),
            last_overlooked_at=None,
            times_ignored=_count("ignored_count"),
            last_ignored_at=None,
        )

    def semantic_update_text(self, *, id: str, content: str) -> None:
        """Update the text of a semantic record. Metadata snapshot unchanged
        (but full — system keys stripped via _full_metadata_snapshot)."""
        record = self._get_semantic_record(id)
        metadata = record.get("metadata", {})
        snapshot = self._full_metadata_snapshot(metadata, {})

        response = self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                records=[
                    {
                        "memoryRecordId": id,
                        "timestamp": datetime.now(UTC),
                        "content": {"text": content},
                        "metadata": snapshot,
                    }
                ],
            )
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore semantic_update_text failed for {id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    def semantic_set_scope(self, *, id: str, scope: str) -> None:
        """Move a semantic record between project and general namespaces."""
        if scope not in ("project", "general"):
            raise ValueError(f"scope must be 'project' or 'general', got {scope!r}")
        record = self._get_semantic_record(id)
        metadata = record.get("metadata", {})
        snapshot = self._full_metadata_snapshot(metadata, {})

        target_namespace = (
            resolve_namespace("general", "semantic")
            if scope == "general"
            else resolve_namespace(resolve_actor_id(self._project), "semantic")
        )

        response = self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                records=[
                    {
                        "memoryRecordId": id,
                        "timestamp": datetime.now(UTC),
                        "namespaces": [target_namespace],
                        "metadata": snapshot,
                    }
                ],
            )
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore semantic_set_scope failed for {id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic record."""
        response = self._data.batch_delete_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            records=[{"memoryRecordId": id}],
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore semantic_delete failed for {id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    # ----- Episodes: no-ops in agentcore mode (this task) -----

    def open_background_episode(
        self, *, session_id: str, project: str
    ) -> str:
        # No real episode; return a synthetic id so the MCP tool path
        # works. The id is not used anywhere downstream in agentcore mode.
        return f"agentcore-noop-bg-{uuid4().hex[:12]}"

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        return f"agentcore-noop-fg-{uuid4().hex[:12]}"

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return ""

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        return []

    # ----- Reflection lifecycle: Task 10 -----

    def _mutate_namespace_and_status(
        self,
        *,
        reflection_id: str,
        new_namespaces: list[str],
        new_status: str,
    ) -> None:
        """Shared helper: read current metadata, mutate namespace + status,
        write back with full metadata snapshot.

        Migrated (SQLite-origin) reflections carry status in the content body,
        not metadata (design §1b) — a metadata status write is silently dropped
        by AWS. For those records the status change is a body read-modify-write
        (CONTENT update); the namespace move is still applied via the
        ``namespaces`` field, which is not part of the dropped metadata map.
        AWS-extracted records keep the metadata path below unchanged."""
        record = self._get_record(reflection_id)

        body = self._record_body(record)
        if body.get("source_backend") == "sqlite":
            new_body = dict(body)
            new_body["status"] = new_status
            new_body["updated_at"] = datetime.now(UTC).isoformat()
            response = self._retry_on_transient_404(
                lambda: self._data.batch_update_memory_records(
                    memoryId=self._cfg.episodic.memory_id,
                    records=[
                        {
                            "memoryRecordId": reflection_id,
                            "timestamp": datetime.now(UTC),
                            "namespaces": new_namespaces,
                            "content": {"text": json.dumps(new_body)},
                        }
                    ],
                )
            )
            failed = response.get("failedRecords", [])
            if failed:
                raise RuntimeError(
                    f"AgentCore reflection mutation failed for {reflection_id}: "
                    f"{failed[0].get('errorMessage', 'unknown')}"
                )
            return

        metadata = record.get("metadata", {})

        updates: dict[str, dict[str, Any]] = {
            "status": {"stringValue": new_status},
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=self._cfg.episodic.memory_id,
                records=[
                    {
                        "memoryRecordId": reflection_id,
                        "timestamp": datetime.now(UTC),
                        "namespaces": new_namespaces,
                        "metadata": snapshot,
                    }
                ],
            )
        )
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore reflection mutation failed for {reflection_id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    def promote_reflection(self, *, reflection_id: str) -> None:
        self._mutate_namespace_and_status(
            reflection_id=reflection_id,
            new_namespaces=[resolve_namespace("general", "reflections")],
            new_status="promoted",
        )

    def retire_reflection(self, *, reflection_id: str) -> None:
        actor_id = resolve_actor_id(self._project)
        self._mutate_namespace_and_status(
            reflection_id=reflection_id,
            new_namespaces=[resolve_namespace(actor_id, "retired")],
            new_status="retired",
        )

    def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
        """Row-only accessor (no provenance): fetch + parse a single
        reflection record. None on a hard 404. Maps the internal
        _parse_reflection_record shape to the ReflectionFull dict keys the
        drawer needs: scope is derived from the record's namespaces, hints
        is re-serialised to a JSON string (matching the sqlite column
        shape so the drawer's decode_hints filter works unchanged), and
        the four last_*_at timestamps -- which AgentCore does not track --
        are always None."""
        try:
            record = self._get_record(reflection_id)
        except _ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") == "ResourceNotFoundException":
                return None
            raise
        parsed = self._parse_reflection_record(record)
        if parsed is None:
            return None
        namespaces = record.get("namespaces") or []
        first_ns = next(iter(namespaces or [""]), "")
        scope = "general" if first_ns.lstrip("/").startswith("general/") else "project"
        created = record.get("createdAt")
        created_at = created.isoformat() if isinstance(created, datetime) else (created or "")
        return {
            "id": parsed["id"],
            "title": parsed["title"],
            "project": self._project,
            "tech": parsed["tech"],
            "phase": parsed["phase"],
            "polarity": parsed["_polarity"],
            "confidence": parsed["confidence"],
            "status": parsed["_status"],
            "use_cases": parsed["use_cases"],
            "hints": json.dumps(parsed["hints"]),
            "evidence_count": parsed["evidence_count"],
            "scope": scope,
            "created_at": created_at,
            "updated_at": parsed["updated_at"],
            "useful_count": parsed["useful_count"],
            "last_useful_at": None,
            "times_misled": parsed["times_misled"],
            "last_misled_at": None,
            "times_overlooked": parsed["times_overlooked"],
            "last_overlooked_at": None,
        }

    # ----- Session lifecycle: Tasks 9, 12 -----

    def session_bootstrap(
        self,
        *,
        session_id: str,
        source: str | None = None,
        cwd: Any | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Fetch top-N reflections per polarity bucket + top-N semantic
        preferences IN PARALLEL; assemble the additional_context envelope
        matching the BootstrapResult shape the MCP handler at
        server.py:1398-1411 unwraps.

        Reflections go through the same two-namespace fan-out as retrieve()
        (_fetch_reflection_buckets: project + general/promoted merge,
        client-side polarity bucketing — live AWS rejects polarity as a
        metadata filter key). Uses list_memory_records (not
        retrieve_memory_records) — bootstrap is metadata-only, no semantic
        search query. The boto3 calls are independent network round-trips,
        so we fan out via concurrent.futures.ThreadPoolExecutor.
        The Protocol signature is sync, but session_bootstrap is reached via
        the MCP server's async `_call_tool` — meaning an event loop is already
        running; asyncio.run() would raise. ThreadPoolExecutor provides the
        same parallelism without touching the outer loop.

        Project resolution honours the cwd param (sqlite-parity): explicit
        project wins, then project_name(cwd), then the construction-time
        project."""
        if project is None and cwd is not None:
            from pathlib import Path

            from better_memory.config import project_name

            project = project_name(Path(cwd))
        actor_id = resolve_actor_id(project or self._project)
        semantic_namespace = resolve_namespace(actor_id, "semantic")

        def _fetch_semantic() -> dict[str, Any]:
            return self._data.list_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=semantic_namespace,
                maxResults=10,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reflections_future = pool.submit(
                self._fetch_reflection_buckets,
                actor_id=actor_id,
                limit_per_bucket=5,
            )
            semantic_future = pool.submit(_fetch_semantic)

            reflection_buckets = reflections_future.result()
            semantic_response = semantic_future.result()

        reflections_counts = {
            polarity: len(reflection_buckets[polarity])
            for polarity in _POLARITIES
        }
        semantic_items = semantic_response.get("memoryRecordSummaries", [])
        semantic_count = len(semantic_items)

        # additional_context is the JSON-serialized payload Claude sees on
        # SessionStart. Format matches what the MCP handler emits today: a
        # short text summary of reflection + semantic counts. The handler at
        # server.py:1398 wraps this under the `additionalContext` key in the
        # final tool payload.
        reflection_lines = [
            f"{polarity}: {reflections_counts[polarity]} reflections"
            for polarity in _POLARITIES
        ]
        additional_context = (
            f"Project: {actor_id}\n"
            f"Reflections — {', '.join(reflection_lines)}\n"
            f"Semantic memories: {semantic_count}"
        )

        return {
            # Match sqlite-mode BootstrapResult exactly (server.py:1398-1411
            # unwraps these keys). In agentcore mode there is no real episode,
            # so episode_id is the placeholder session_id and episode_action
            # is always "opened" (the bootstrap handler treats agentcore-mode
            # sessions as fresh each time; AgentCore-side episode tracking is
            # internal and invisible to the bootstrap wire shape).
            "additional_context": additional_context,
            "project": actor_id,
            "source": source or "",
            "episode_id": session_id,
            "episode_action": "opened",
            "semantic_count": semantic_count,
            "reflections_counts": reflections_counts,
            # pending_synthesis intentionally omitted in agentcore mode;
            # the MCP handler must branch on its absence (backend.supports_synthesis
            # already signals this at the Protocol level).
        }

    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        """Write exposures to the local ledger when one is available.

        Agentcore mode has no AWS-side exposure log (memory records carry
        no per-session concept); the SAME shared ``exposure_log.record``
        primitive sqlite mode uses is delegated to against the local
        ``memory.db`` instead, keyed by the backend's ``local_conn`` (wired
        by ``storage/factory.py`` from the caller's local connection — see
        ``hooks/contextual_inject.py`` / ``hooks/session_bootstrap.py``).

        Falls back to the pre-existing no-op when ``local_conn`` is None
        (no local db available, e.g. plain unit-test construction) or when
        ``session_id`` is empty — matching ``exposure_log.record``'s own
        best-effort short-circuit. Best-effort overall: any local-db error
        is swallowed rather than raised, so a ledger write can never block
        the caller's exposure-tracking path (mirrors the contextual_inject
        hook's own best-effort wrapper around this call).
        """
        if self._local_conn is None or not session_id:
            return
        try:
            from better_memory.services import exposure_log

            exposure_log.record(
                self._local_conn,
                session_id=session_id,
                items=items,
                source=source,
                now=datetime.now(UTC).isoformat(),
            )
            self._local_conn.commit()
        except Exception:  # noqa: BLE001 - exposures must never block
            pass

    def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
        """List unrated exposures from the local ledger when one is available.

        ``session_id`` re-resolution mirrors ``_require_session_id``'s
        fallback chain (live env/marker, then the construction-time id) but
        NEVER raises: an empty/unresolvable session id degrades to the
        empty envelope ``{"session_id": None, "exposures": []}``, matching
        sqlite mode's own no-session shape
        (``SessionBootstrapService.list_session_exposures``).

        When no local ledger is available (``local_conn`` is None — no AWS
        exposure log exists to fall back to), returns the envelope with an
        empty exposures list — the pre-existing agentcore-mode contract.
        Otherwise builds the same envelope shape as sqlite mode over
        ``exposure_log.list_unrated``'s grouped/deduped/display-joined rows.
        """
        sid = session_id
        if not sid:
            try:
                sid = self._require_session_id("list_session_exposures")
            except ValueError:
                return {"session_id": None, "exposures": []}

        if self._local_conn is None:
            return {"session_id": sid, "exposures": []}

        from better_memory.services import exposure_log

        rows = exposure_log.list_unrated(self._local_conn, session_id=sid)
        return {
            "session_id": sid,
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

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        """Apply one classification → counter increment on a record,
        mid-session (the memory.credit MCP tool path).

        Counter mapping (spec Rating model section):
            cited / shaped → useful_count
            misled         → times_misled
            overlooked     → overlooked_count

        Sqlite parity (services/memory_rating.py MemoryRatingService.
        credit_one), added by agentcore-parity Task 7:
        - classification='ignored' is REJECTED here with the same error
          text sqlite uses — 'ignored' is the session-end sweep's
          exclusive write path (apply_session_ratings); accepting it here
          would let a single mid-session call bump ignored_count outside
          the sweep's skip-bucket accounting.
        - `evidence` is now validated via the shared
          services.memory_rating.validate_evidence helper (non-empty,
          post-strip, <=EVIDENCE_MAX_CHARS) — the same contract sqlite
          enforces. `evidence` defaults to None only as a compat shim for
          callers not yet updated to pass it; None fails validation with a
          clear ValueError rather than a crash (mirrors sqlite's
          credit_one docstring).

        On a successful AWS counter push, best-effort stamps the local
        exposure row (rated_at/classification/evidence) via
        exposure_log.stamp when a local ledger is wired (self._local_conn)
        — never raises; mirrors the write-then-best-effort-ledger pattern
        used elsewhere in this class (record_exposures,
        _record_retrieve_exposures). Agentcore has no skip-bucket concept
        on this path (not_exposed/already_rated are sweep-only — see
        apply_session_ratings); exposure_log.stamp is itself a no-op when
        no matching unrated row exists, so this call is safe regardless of
        ledger state.
        """
        from better_memory.services.memory_rating import validate_evidence

        if classification == "ignored":
            raise ValueError(
                "credit_one does not accept classification='ignored'; "
                "'ignored' is the session-end sweep default."
            )
        if classification not in _RATING_TO_COUNTER:
            raise ValueError(
                f"classification={classification!r} is not one of "
                f"{sorted(_RATING_TO_COUNTER)}"
            )
        trimmed_evidence = validate_evidence(classification, evidence, where="credit")

        result = self._credit_counter(kind=kind, id=id, classification=classification)

        if result["applied"] is not None and self._local_conn is not None:
            try:
                from better_memory.services import exposure_log

                exposure_log.stamp(
                    self._local_conn,
                    session_id=session_id,
                    kind=kind,
                    memory_id=id,
                    classification=classification,
                    evidence=trimmed_evidence,
                    now=datetime.now(UTC).isoformat(),
                )
                self._local_conn.commit()
            except Exception:  # noqa: BLE001 - local stamp must never block credit_one
                pass

        return result

    def _credit_counter(
        self, *, kind: str, id: str, classification: str
    ) -> dict[str, Any]:
        """AWS-side counter bump for one (kind, id, classification) — the
        wire mechanics shared by credit_one (mid-session, rejects
        'ignored') and apply_session_ratings (the session-end sweep, the
        only caller that reaches this with classification='ignored' —
        'ignored' → ignored_count is legitimate only via the sweep, design
        §3.2/§4). Callers are responsible for validating `classification`
        before calling this (it is a dict lookup here, not re-validated).

        Routes by kind (semantic vs episodic memory), and — for episodic
        reflections — by source_backend: migrated (SQLite-origin) records
        read-modify-write the JSON content BODY (metadata writes are
        silently dropped there); AWS-extracted / semantic records use the
        declared metadata path.

        Returns {"applied": classification, "skipped": None} on success or
        {"applied": None, "skipped": <error message>} on a per-record
        batch-update failure (failedRecords). Raises _ClientError on a hard
        404 (ResourceNotFoundException) after the transient-404 retry is
        exhausted — callers decide whether that counts as memory_missing.
        """
        counter_key = _RATING_TO_COUNTER[classification]

        # Route the lookup + update to the right memory based on kind.
        # Semantic records live in self._cfg.semantic.memory_id; looking
        # them up against episodic raises ResourceNotFoundException.
        if kind == "semantic":
            record = self._get_semantic_record(id)
            memory_id = self._cfg.semantic.memory_id
        else:
            record = self._get_record(id)
            memory_id = self._cfg.episodic.memory_id

        # Migrated (SQLite-origin) EPISODIC reflections carry rating state in
        # the content body; a metadata write is silently dropped by AWS in the
        # episodic reflections namespace (design §1b). Read-modify-write the
        # body instead. Semantic migrated records use DECLARED metadata (the
        # userPreference schema governs client writes — design §1b), so they
        # stay on the metadata path below; AWS-extracted reflections likewise.
        if kind != "semantic":
            body = self._record_body(record)
            if body.get("source_backend") == "sqlite":
                response = self._credit_body_counter(
                    memory_id=memory_id,
                    record_id=id,
                    body=body,
                    counter_key=counter_key,
                )
                failed = response.get("failedRecords", [])
                if failed:
                    return {
                        "applied": None,
                        "skipped": failed[0].get("errorMessage", "unknown"),
                    }
                return {"applied": classification, "skipped": None}

        metadata = record.get("metadata", {})
        current = float(metadata.get(counter_key, {}).get("numberValue", 0))

        updates: dict[str, dict[str, Any]] = {
            counter_key: {"numberValue": current + 1},
            # STRING indexed key — dateTimeValue is rejected (see record_use).
            "last_credited_at": {
                "stringValue": datetime.now(UTC).isoformat()
            },
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._retry_on_transient_404(
            lambda: self._data.batch_update_memory_records(
                memoryId=memory_id,
                records=[
                    {
                        "memoryRecordId": id,
                        "timestamp": datetime.now(UTC),
                        "metadata": snapshot,
                    }
                ],
            )
        )
        failed = response.get("failedRecords", [])
        if failed:
            return {
                "applied": None,
                "skipped": failed[0].get("errorMessage", "unknown"),
            }
        # Sqlite parity: MemoryRatingService.credit_one returns the applied
        # CLASSIFICATION (not the memory id) on success.
        return {"applied": classification, "skipped": None}

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The real session-end rating sweep (agentcore-parity Task 7,
        design §2). Runs, in order:

        (a) Validate the WHOLE batch via the shared
            services.memory_rating.validate_ratings_batch helper — batch-
            atomic (ValueError on the first violation, zero wire calls,
            nothing applied), identical error text and evidence contract
            to sqlite's MemoryRatingService.apply_session_ratings.
        (b) Per entry, when a local exposure ledger is wired
            (self._local_conn): look up its exposure rows for
            (session_id, kind, id). No rows → skipped.not_exposed. Rows
            all already rated → skipped.already_rated. Otherwise stamp
            every unrated row via exposure_log.stamp (rated_at,
            classification, evidence) BEFORE the AWS push — a later AWS
            counter failure does not roll this stamp back (best-effort,
            per-item, matching this file's established style elsewhere).
            Without a local ledger (self._local_conn is None), this step
            is skipped entirely — not_exposed/already_rated never fire,
            matching the pre-Task-7 behaviour.
        (c) Push the counter bump to AWS via the shared `_credit_counter`
            wire mechanics credit_one also uses — INCLUDING
            classification='ignored' → ignored_count; the sweep is the
            only legitimate writer of ignores (credit_one rejects it). A
            hard 404 (ResourceNotFoundException, or a per-record
            batch-update failedRecords entry) counts as
            skipped.memory_missing. Any OTHER AWS failure (throttling,
            access-denied, a transient network error — batch APIs share a
            20 TPS pool, so throttling is plausible) is best-effort: the
            local stamp from (b) already committed and is NOT lost, the
            entry counts as applied (design's error-handling section:
            "All AWS calls best-effort ... counters are statistics, not
            ledgers"), and the sweep keeps going. Nothing raised by step
            (c) ever aborts the sweep.
        (d) After the loop, ONE best-effort CreateEvent receipt
            (extractionMode='SKIP' so AgentCore's built-in extraction
            ignores it; metadata={'type': {'stringValue': 'ratings'}};
            sessionId=the resolved session; payload = one blob item with
            the JSON of every entry that was actually applied, including
            evidence) — only when at least one entry applied. Event
            failure is swallowed and never affects the sweep's return
            value.

        Returns the sqlite shape::

            {"session_id": str,
             "applied":  {"cited": int, "shaped": int, "ignored": int,
                          "misled": int, "overlooked": int},
             "skipped":  {"not_exposed": int, "already_rated": int,
                          "memory_missing": int, "memory_retired": int}}

        memory_retired never fires here — agentcore reflections carry no
        equivalent "superseded before rating" lifecycle check on this
        path. There is no AWS-side atomicity for step (c) — entries
        already credited/stamped stay that way regardless of what happens
        to a later entry, and this method itself never raises once past
        the up-front batch validation in step (a)."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not ratings:
            raise ValueError("ratings must be non-empty")

        from better_memory.services.memory_rating import validate_ratings_batch

        ratings = validate_ratings_batch(ratings)

        applied: dict[str, int] = {cls: 0 for cls in _RATING_TO_COUNTER}
        skipped: dict[str, int] = {
            "not_exposed": 0,
            "already_rated": 0,
            "memory_missing": 0,
            "memory_retired": 0,
        }
        now_iso = datetime.now(UTC).isoformat()
        rated_entries: list[dict[str, Any]] = []

        for r in ratings:
            kind = r["kind"]
            rid = r["id"]
            cls = r["class"]
            evidence = r["_evidence"]

            if self._local_conn is not None:
                from better_memory.services import exposure_log

                rows = self._local_conn.execute(
                    "SELECT rated_at FROM session_memory_exposure "
                    "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?",
                    (session_id, kind, rid),
                ).fetchall()
                if not rows:
                    skipped["not_exposed"] += 1
                    continue
                if all(row["rated_at"] is not None for row in rows):
                    skipped["already_rated"] += 1
                    continue
                exposure_log.stamp(
                    self._local_conn,
                    session_id=session_id,
                    kind=kind,
                    memory_id=rid,
                    classification=cls,
                    evidence=evidence,
                    now=now_iso,
                )
                self._local_conn.commit()

            try:
                result = self._credit_counter(kind=kind, id=rid, classification=cls)
            except Exception as exc:  # noqa: BLE001 - AWS counter push must never abort the sweep
                if isinstance(exc, _ClientError):
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code == "ResourceNotFoundException":
                        # The record no longer exists — sqlite calls this
                        # memory_missing; keep rating the rest of the batch.
                        # The local stamp above (if any) is NOT rolled back.
                        skipped["memory_missing"] += 1
                        continue
                # Any OTHER AWS failure — throttling, access-denied, a
                # transient network error, anything besides a confirmed
                # "the record is gone" 404 (batch APIs share a 20 TPS pool,
                # so throttling is plausible). Per the design's
                # error-handling section ("All AWS calls best-effort ...
                # counters are statistics, not ledgers"): the local stamp
                # committed above is the session's evidence-of-record
                # (classification + evidence, feeding the UI and the
                # skip-bucket accounting) and must NOT be lost to a
                # throttled/denied AWS call. The rating IS applied — from
                # the ledger's and the rater's perspective — even though
                # this one counter increment was not; count it and keep
                # sweeping the rest of the batch. apply_session_ratings
                # must never raise from an AWS failure.
                applied[cls] += 1
                rated_entries.append(
                    {"kind": kind, "id": rid, "class": cls, "evidence": evidence}
                )
                continue
            if result["applied"] is None:
                # Per-record batch-update failure (failedRecords) — the
                # closest sqlite skip reason is memory_missing.
                skipped["memory_missing"] += 1
                continue

            applied[cls] += 1
            rated_entries.append(
                {"kind": kind, "id": rid, "class": cls, "evidence": evidence}
            )

        if rated_entries:
            try:
                self._emit_ratings_event(session_id=session_id, rated=rated_entries)
            except Exception:  # noqa: BLE001 - receipt event must never block the sweep
                pass

        return {"session_id": session_id, "applied": applied, "skipped": skipped}

    def _emit_ratings_event(
        self, *, session_id: str, rated: list[dict[str, Any]]
    ) -> None:
        """Best-effort CreateEvent receipt for a successful rating sweep
        (design §3.2/§4 "Ratings event (C-lite)"). extractionMode='SKIP'
        keeps this out of AgentCore's built-in episodicMemoryStrategy
        extraction — it is a durable, team-visible receipt only; the
        read/UI path is deferred to a future PR. Callers wrap this in a
        try/except — a failure here must never affect the sweep's return
        value."""
        actor_id = resolve_actor_id(self._project)
        self._data.create_event(
            memoryId=self._cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            extractionMode="SKIP",
            payload=[{"blob": {"ratings": rated}}],
            metadata={"type": {"stringValue": "ratings"}},
        )

    # ----- Synthesis: no-ops in agentcore mode -----

    def synthesize_next_get_context(self, *, project: str) -> Any:
        # No-op in agentcore mode: AgentCore's built-in episodicMemoryStrategy
        # performs extraction internally, so there is never a "pending
        # episode" to drain. Return None — sqlite mode also returns None when
        # nothing is pending, so callers that don't check supports_synthesis
        # see a quiet "nothing to do" signal instead of a crash.
        return None

    def synthesize_next_apply(
        self, *, episode_id: str, response: Any, project: str
    ) -> Any:
        # No-op in agentcore mode. Return the empty SynthesisStep-shaped dict
        # ("nothing applied, nothing skipped") matching the rest of the
        # agentcore-mode no-op contract (list_session_exposures empty
        # envelope, list_episodes empty list, episode close empty string).
        return {"applied": 0, "skipped": 0}
