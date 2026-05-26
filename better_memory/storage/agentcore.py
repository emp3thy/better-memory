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
import json
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

    def record_use(
        self, observation_id: str, *, outcome: UseOutcome | None = None
    ) -> None:
        raise NotImplementedError("Implemented in Task 8")

    # ----- Semantic memories: Task 11 -----

    def semantic_observe(self, **kwargs: Any) -> str:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_list(self, **kwargs: Any) -> list[Any]:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_update_text(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_set_scope(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

    def semantic_delete(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 11")

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

    def promote_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    def retire_reflection(self, **kwargs: Any) -> None:
        raise NotImplementedError("Implemented in Task 10")

    # ----- Session lifecycle: Tasks 9, 12 -----

    def session_bootstrap(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 12")

    def list_session_exposures(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Implemented in Task 9")

    def apply_session_ratings(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

    def credit_one(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Implemented in Task 9")

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
