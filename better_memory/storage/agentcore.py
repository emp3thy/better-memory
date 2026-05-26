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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from better_memory.storage.agentcore_persistence import AgentCoreConfig
from better_memory.storage.protocol import Outcome, UseOutcome
from better_memory.storage.session import (
    parse_hints_prose,
    resolve_actor_id,
    resolve_namespace,
)

_POLARITIES: tuple[str, str, str] = ("do", "dont", "neutral")
_OVERLOOKED_RANKING_WEIGHT = 3  # mirrors better_memory/services/memory_rating.py:71
_AGENTCORE_SYSTEM_METADATA_PREFIX = "x-amz-agentcore-memory-"
_RATING_TO_COUNTER: dict[str, str] = {
    "cited": "useful_count",
    "shaped": "useful_count",
    "ignored": "ignored_count",
    "misled": "times_misled",
    "overlooked": "overlooked_count",
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
    ) -> None:
        self._cfg = config
        self._data = data_client
        self._control = control_client
        self._session_id = session_id
        self._project = project

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return False

    @property
    def supports_episodes(self) -> bool:
        return False

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

        sessionId is the backend's held session id (raised if None — events
        require a real session). actorId is resolved from project (or
        "general" when no project is in scope). Returns the AgentCore
        eventId."""
        if self._session_id is None:
            raise ValueError(
                "AgentCoreBackend.observe requires session_id at construction "
                "time. The MCP server populates it from CLAUDE_SESSION_ID at "
                "startup; if you see this in production, the env var is missing."
            )
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

        response = self._data.create_event(
            memoryId=self._cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=self._session_id,
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
    ) -> dict[str, list[dict[str, Any]]]:
        """Bucketed reflection retrieval matching ReflectionSynthesisService.retrieve_reflections.

        Per-polarity list_memory_records against the reflections namespace,
        parse JSON content, rank via the sqlite ordering rule
        (useful_count + 3*times_overlooked DESC, confidence DESC, updated_at DESC).
        Returns dict[polarity, list[reflection_dict]] in the same shape sqlite mode
        returns; the MCP memory.retrieve handler json-dumps this directly to Claude.

        track_exposure is accepted for parity but no-op in agentcore mode —
        AgentCore has no session_memory_exposure table; exposure tracking is
        not part of the agentcore-mode rating model.
        """
        actor_id = resolve_actor_id(project or self._project)
        namespace = resolve_namespace(actor_id, "reflections")
        effective_limit = limit_per_bucket if limit_per_bucket is not None else 20

        # Restrict fan-out to a single polarity if the caller specified one
        polarities_to_fetch: tuple[str, ...] = (
            (polarity,) if polarity in _POLARITIES else _POLARITIES
        )

        def _fetch(p: str) -> list[dict[str, Any]]:
            filters: list[dict[str, Any]] = [
                {
                    "left": {"metadataKey": "polarity"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": p}},
                },
                {
                    "left": {"metadataKey": "status"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "active"}},
                },
            ]
            response = self._data.list_memory_records(
                memoryId=self._cfg.episodic.memory_id,
                namespace=namespace,
                maxResults=effective_limit * 2,  # fetch some slack for client-side ranking
                metadataFilters=filters,
            )
            parsed_raw = [
                self._parse_reflection_record(rec, tech_filter=tech, phase_filter=phase)
                for rec in response.get("memoryRecordSummaries", [])
            ]
            # Drop None entries (filtered out by tech/phase post-filter)
            parsed: list[dict[str, Any]] = [r for r in parsed_raw if r is not None]
            # Sqlite ordering: (useful_count + 3*times_overlooked) DESC,
            # confidence DESC, updated_at DESC.
            parsed.sort(
                key=lambda r: (
                    -(r["useful_count"] + _OVERLOOKED_RANKING_WEIGHT * r["_overlooked_count"]),
                    -r["confidence"],
                    -r["_updated_at_ts"],
                )
            )
            return parsed[:effective_limit]

        async def _gather_all() -> dict[str, list[dict[str, Any]]]:
            loop = asyncio.get_running_loop()
            tasks = {p: loop.run_in_executor(None, _fetch, p) for p in polarities_to_fetch}
            return {p: await task for p, task in tasks.items()}

        fetched = asyncio.run(_gather_all())

        # Always return all 3 keys for stable shape; unfetched buckets are [].
        return {p: fetched.get(p, []) for p in _POLARITIES}

    def _parse_reflection_record(
        self,
        rec: dict[str, Any],
        *,
        tech_filter: str | None = None,
        phase_filter: str | None = None,
    ) -> dict[str, Any] | None:
        """Map MemoryRecordSummary -> sqlite-shaped reflection dict.

        Returns None if tech_filter / phase_filter excludes this record."""
        text = rec.get("content", {}).get("text", "")
        try:
            body = json.loads(text) if isinstance(text, str) else {}
        except json.JSONDecodeError:
            body = {}

        metadata_raw = rec.get("metadata", {})

        def _num(key: str) -> float:
            return float(metadata_raw.get(key, {}).get("numberValue", 0))

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

        updated_at = rec.get("updatedAt") or rec.get("createdAt")
        updated_at_ts = updated_at.timestamp() if isinstance(updated_at, datetime) else 0.0

        return {
            # Public shape — must match ReflectionSynthesisService.retrieve_reflections
            # return: {id, title, phase, use_cases, hints (list), confidence (float),
            #          tech, evidence_count, useful_count}
            "id": rec["memoryRecordId"],
            "title": body.get("title", "") if isinstance(body, dict) else "",
            "phase": phase_value,
            "use_cases": body.get("use_cases", "") if isinstance(body, dict) else "",
            "hints": hints_list,
            "confidence": confidence,
            "tech": tech_value,
            "evidence_count": int(_num("useful_count")) + int(_num("missed_count")),
            "useful_count": int(_num("useful_count")),
            # Internal ranking helpers — leading underscore so callers ignore
            "_overlooked_count": int(_num("overlooked_count")),
            "_updated_at_ts": updated_at_ts,
        }

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
        session enumeration is deferred (ListEvents requires sessionId)."""
        if self._session_id is None:
            raise ValueError(
                "AgentCoreBackend.list_observations requires session_id at "
                "construction time."
            )
        actor_id = resolve_actor_id(project or self._project)

        response = self._data.list_events(
            memoryId=self._cfg.episodic.memory_id,
            actorId=actor_id,
            sessionId=self._session_id,
            maxResults=limit,
            includePayloads=True,
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

            results.append(
                {
                    "id": event["eventId"],
                    "content": payload_text,
                    "session_id": event.get("sessionId"),
                    "actor_id": event.get("actorId"),
                    "event_timestamp": event.get("eventTimestamp"),
                    **flat_metadata,
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
        from botocore.exceptions import ClientError

        for attempt in range(1, max_attempts + 1):
            try:
                return call()
            except ClientError as exc:
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

    def record_use(
        self, observation_id: str, *, outcome: UseOutcome | None = None
    ) -> None:
        """Credit a record's reinforcement counter. outcome=None is a no-op
        (no classification, no counter change)."""
        if outcome is None:
            return

        record = self._get_record(observation_id)
        metadata = record.get("metadata", {})

        counter_key = "useful_count" if outcome == "success" else "missed_count"
        current_count = float(
            metadata.get(counter_key, {}).get("numberValue", 0)
        )

        updates: dict[str, dict[str, Any]] = {
            counter_key: {"numberValue": current_count + 1},
            "last_credited_at": {"dateTimeValue": datetime.now(UTC)},
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._data.batch_update_memory_records(
            memoryId=self._cfg.episodic.memory_id,
            records=[
                {
                    "memoryRecordId": observation_id,
                    "timestamp": datetime.now(UTC),
                    "metadata": snapshot,
                }
            ],
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
        return response["successfulRecords"][0]["memoryRecordId"]

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        """List semantic records. With search → retrieve_memory_records;
        without → list_memory_records."""
        actor_id = resolve_actor_id(project or self._project)
        if scope_filter == "general":
            namespace = resolve_namespace("general", "semantic")
        else:
            namespace = resolve_namespace(actor_id, "semantic")

        if search and search.strip():
            response = self._data.retrieve_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": search.strip(),
                    "topK": 50,
                },
            )
        else:
            response = self._data.list_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=namespace,
                maxResults=100,
            )

        return [
            {
                "id": rec["memoryRecordId"],
                "content": rec.get("content", {}).get("text", ""),
                "namespaces": rec.get("namespaces", []),
                "scope": "general" if rec.get("namespaces", [""])[0].startswith("general/")
                         else "project",
            }
            for rec in response.get("memoryRecordSummaries", [])
        ]

    def semantic_update_text(self, *, id: str, content: str) -> None:
        """Update the text of a semantic record. Metadata snapshot unchanged
        (but full — system keys stripped via _full_metadata_snapshot)."""
        record = self._get_semantic_record(id)
        metadata = record.get("metadata", {})
        snapshot = self._full_metadata_snapshot(metadata, {})

        response = self._data.batch_update_memory_records(
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

        response = self._data.batch_update_memory_records(
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
        failed = response.get("failedRecords", [])
        if failed:
            raise RuntimeError(
                f"AgentCore semantic_set_scope failed for {id}: "
                f"{failed[0].get('errorMessage', 'unknown')}"
            )

    def semantic_delete(self, *, id: str) -> None:
        """Permanently delete a semantic record."""
        self._data.batch_delete_memory_records(
            memoryId=self._cfg.semantic.memory_id,
            records=[{"memoryRecordId": id}],
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
        write back with full metadata snapshot."""
        record = self._get_record(reflection_id)
        metadata = record.get("metadata", {})

        updates: dict[str, dict[str, Any]] = {
            "status": {"stringValue": new_status},
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._data.batch_update_memory_records(
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

        Uses list_memory_records (not retrieve_memory_records) — bootstrap is
        metadata-only, no semantic search query. 4 boto3 calls are independent
        network round-trips, so we fan out via asyncio.gather + run_in_executor
        under asyncio.run(...) — the Protocol signature is sync, but the cost
        of spinning an event loop once per bootstrap is negligible (bootstrap
        runs once per session)."""
        actor_id = resolve_actor_id(project or self._project)
        reflections_namespace = resolve_namespace(actor_id, "reflections")
        semantic_namespace = resolve_namespace(actor_id, "semantic")

        def _fetch_reflections(polarity: str) -> dict[str, Any]:
            return self._data.list_memory_records(
                memoryId=self._cfg.episodic.memory_id,
                namespace=reflections_namespace,
                maxResults=5,
                metadataFilters=[
                    {
                        "left": {"metadataKey": "polarity"},
                        "operator": "EQUALS_TO",
                        "right": {"metadataValue": {"stringValue": polarity}},
                    },
                    {
                        "left": {"metadataKey": "status"},
                        "operator": "EQUALS_TO",
                        "right": {"metadataValue": {"stringValue": "active"}},
                    },
                ],
            )

        def _fetch_semantic() -> dict[str, Any]:
            return self._data.list_memory_records(
                memoryId=self._cfg.semantic.memory_id,
                namespace=semantic_namespace,
                maxResults=10,
            )

        async def _gather_all() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
            loop = asyncio.get_running_loop()
            reflection_tasks = {
                polarity: loop.run_in_executor(None, _fetch_reflections, polarity)
                for polarity in _POLARITIES
            }
            semantic_task = loop.run_in_executor(None, _fetch_semantic)

            reflection_responses = {
                polarity: await task for polarity, task in reflection_tasks.items()
            }
            semantic_response = await semantic_task
            return reflection_responses, semantic_response

        reflection_calls, semantic_response = asyncio.run(_gather_all())

        reflections_counts = {
            polarity: len(reflection_calls[polarity].get("memoryRecordSummaries", []))
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

    def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
        """Per spec Rating model section: no exposure log in agentcore mode.
        Returns the standard envelope shape with an empty exposures list."""
        return {"session_id": session_id, "exposures": []}

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> dict[str, Any]:
        """Apply one classification → counter increment on a record.

        Counter mapping (spec Rating model section):
            cited / shaped → useful_count
            ignored        → ignored_count
            misled         → times_misled
            overlooked     → overlooked_count"""
        counter_key = _RATING_TO_COUNTER.get(classification)
        if counter_key is None:
            raise ValueError(
                f"classification={classification!r} is not one of "
                f"{sorted(_RATING_TO_COUNTER)}"
            )

        record = self._get_record(id)
        metadata = record.get("metadata", {})
        current = float(metadata.get(counter_key, {}).get("numberValue", 0))

        updates: dict[str, dict[str, Any]] = {
            counter_key: {"numberValue": current + 1},
            "last_credited_at": {"dateTimeValue": datetime.now(UTC)},
        }
        snapshot = self._full_metadata_snapshot(metadata, updates)

        response = self._data.batch_update_memory_records(
            memoryId=self._cfg.episodic.memory_id,
            records=[
                {
                    "memoryRecordId": id,
                    "timestamp": datetime.now(UTC),
                    "metadata": snapshot,
                }
            ],
        )
        failed = response.get("failedRecords", [])
        if failed:
            return {
                "applied": None,
                "skipped": failed[0].get("errorMessage", "unknown"),
            }
        return {"applied": id, "skipped": None}

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Iterate ratings, call credit_one per entry. Return summary dict
        with `applied` (count of successful credits) and `failed` (count of
        skip / error results)."""
        applied = 0
        failed = 0
        for r in ratings:
            result = self.credit_one(
                session_id=session_id,
                kind=r["kind"],
                id=r["id"],
                classification=r["classification"],
            )
            if result["applied"] is not None:
                applied += 1
            else:
                failed += 1
        return {"applied": applied, "failed": failed}

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
