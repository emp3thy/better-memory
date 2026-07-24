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
import concurrent.futures
import hashlib
import json
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
        rank via the sqlite ordering rule (useful_count +
        3*times_overlooked DESC, confidence DESC, updated_at DESC).
        Returns dict[polarity, list[reflection_dict]] in the same shape sqlite mode
        returns; the MCP memory.retrieve handler json-dumps this directly to Claude.

        ``query`` is accepted for parity but no-op in agentcore mode — BM25
        relevance fusion needs the local ``reflection_fts`` index, which has no
        AgentCore equivalent. Ordering stays the popularity rule below.

        track_exposure is accepted for parity but no-op in agentcore mode —
        AgentCore has no session_memory_exposure table; exposure tracking is
        not part of the agentcore-mode rating model.
        """
        actor_id = resolve_actor_id(project or self._project)
        effective_limit = limit_per_bucket if limit_per_bucket is not None else 20
        return self._fetch_reflection_buckets(
            actor_id=actor_id,
            limit_per_bucket=effective_limit,
            tech=tech,
            phase=phase,
            polarity=polarity if polarity in _POLARITIES else None,
        )

    def _fetch_reflection_buckets(
        self,
        *,
        actor_id: str,
        limit_per_bucket: int,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
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

        for bucket_name, items in buckets.items():
            # Sqlite ordering: (useful_count + 3*times_overlooked) DESC,
            # confidence DESC, updated_at DESC.
            items.sort(
                key=lambda r: (
                    -(r["useful_count"] + _OVERLOOKED_RANKING_WEIGHT * r["_overlooked_count"]),
                    -r["confidence"],
                    -r["_updated_at_ts"],
                )
            )
            # Strip the internal ranking/bucketing helpers — the payload
            # must be key-identical to the sqlite reflection dicts.
            buckets[bucket_name] = [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in items[:limit_per_bucket]
            ]
        return buckets

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
            # AgentCore has no exposure/rating sweep, so "ignored" (shown
            # but never rated) is never tracked here — 0 is the true
            # recorded signal, not a stand-in for a missing feature. The
            # Wilson prior therefore degrades to a monotone function of
            # (useful_count + times_overlooked) on this backend, which is
            # equivalent to the pre-existing popularity ordering below, not
            # a corruption of it.
            "times_ignored": 0,
            "times_misled": _count_body_first("times_misled", "times_misled"),
            "updated_at": updated_at_value,
            # Internal ranking/bucketing helpers — stripped by
            # _fetch_reflection_buckets before the payload leaves the backend.
            "_overlooked_count": overlooked_count,
            "_updated_at_ts": updated_at_ts,
            "_polarity": polarity_value,
            "_status": status_value,
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
            self._semantic_summary_to_model(
                rec, project=project or self._project
            )
            for rec in response.get("memoryRecordSummaries", [])
        ]

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
        (useful_count / times_misled / overlooked_count → times_overlooked);
        the collapsed rating timestamp comes from last_credited_at stringValue.
        Absent metadata (AWS-extracted or freshly-created records) → zeroed
        counters, never None.

        times_ignored is deliberately not populated here: agentcore has no
        exposure/rating sweep to derive it from, so it falls through to the
        SemanticMemory dataclass default of 0 — the true recorded signal,
        not a corruption of it (mirrors the reflection-dict handling in
        _parse_reflection_record above)."""
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
        """No-op: agentcore mode has no exposure log (see list_session_exposures)."""

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
        ratings: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Batch rating apply — sqlite-parity validation and return shape.

        Mirrors MemoryRatingService.apply_session_ratings: the whole batch
        is validated BEFORE any wire call (non-empty session_id/ratings,
        kind in {reflection, semantic}, class in the five rating classes,
        non-empty string id, no duplicate (kind, id) pairs — ValueError on
        any violation), then each entry runs through credit_one.

        Returns the sqlite shape::

            {"session_id": str,
             "applied":  {"cited": int, "shaped": int, "ignored": int,
                          "misled": int, "overlooked": int},
             "skipped":  {"not_exposed": int, "already_rated": int,
                          "memory_missing": int, "memory_retired": int}}

        Agentcore has no exposure log, so not_exposed / already_rated /
        memory_retired never fire here; a missing record (GetMemoryRecord
        404) or a per-record batch-update failure counts as
        memory_missing. There is no AWS-side atomicity — entries already
        credited stay credited if a later entry raises."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not ratings:
            raise ValueError("ratings must be non-empty")

        valid_kinds = {"reflection", "semantic"}
        seen: set[tuple[str, str]] = set()
        for i, r in enumerate(ratings):
            for field_name in ("kind", "class", "id"):
                if field_name not in r:
                    raise ValueError(
                        f"ratings[{i}].{field_name}: missing required field"
                    )
            kind = r["kind"]
            rid = r["id"]
            cls = r["class"]
            if kind not in valid_kinds:
                raise ValueError(
                    f"ratings[{i}].kind: invalid {kind!r}; "
                    f"expected one of {valid_kinds}"
                )
            if cls not in _RATING_TO_COUNTER:
                raise ValueError(
                    f"ratings[{i}].class: invalid {cls!r}; "
                    f"expected one of {set(_RATING_TO_COUNTER)}"
                )
            if not isinstance(rid, str) or not rid:
                raise ValueError(f"ratings[{i}].id: must be non-empty string")
            key = (kind, rid)
            if key in seen:
                raise ValueError(f"ratings[{i}]: duplicate (kind, id) = {key!r}")
            seen.add(key)

        applied: dict[str, int] = {cls: 0 for cls in _RATING_TO_COUNTER}
        skipped: dict[str, int] = {
            "not_exposed": 0,
            "already_rated": 0,
            "memory_missing": 0,
            "memory_retired": 0,
        }
        for r in ratings:
            try:
                result = self.credit_one(
                    session_id=session_id,
                    kind=r["kind"],
                    id=r["id"],
                    classification=r["class"],
                )
            except _ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ResourceNotFoundException":
                    raise
                # The record no longer exists — sqlite calls this
                # memory_missing; keep rating the rest of the batch.
                skipped["memory_missing"] += 1
                continue
            if result["applied"] is not None:
                applied[r["class"]] += 1
            else:
                # Per-record batch-update failure (failedRecords) — the
                # closest sqlite skip reason is memory_missing.
                skipped["memory_missing"] += 1
        return {"session_id": session_id, "applied": applied, "skipped": skipped}

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
