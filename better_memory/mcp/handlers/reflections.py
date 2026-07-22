"""Handlers for reflection retrieval and IDE-driven synthesis.

Tools: ``memory.retrieve``, ``memory.synthesize_next_get_context``,
``memory.synthesize_next_apply``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from better_memory import _diag
from better_memory.config import get_config, project_name
from better_memory.mcp._util import run_best_effort
from better_memory.mcp.serializers import (
    serialize_synth_apply_ok,
    serialize_synth_apply_state_error,
    serialize_synth_apply_validation_error,
    serialize_synth_get_context,
)
from better_memory.mcp.synth_audit import audit_synth_call
from better_memory.services.reflection import (
    ReflectionSynthesisService,
    SynthesisResponseError,
)
from better_memory.services.retention_scheduler import RetentionScheduler
from better_memory.services.spool import SpoolService
from better_memory.storage import StorageBackend


class ReflectionToolHandlers:
    """Reflection retrieval (with its best-effort pre-hooks) + synthesis."""

    def __init__(
        self,
        *,
        backend: StorageBackend,
        reflections: ReflectionSynthesisService,
        spool: SpoolService,
        memory_conn: sqlite3.Connection,
        home: Path,
        remote: StorageBackend | None = None,
    ) -> None:
        self._backend = backend
        self._reflections = reflections
        self._spool = spool
        self._memory_conn = memory_conn
        self._home = home
        # agentcore-mode marker: gates the sqlite-local best-effort
        # pre-hooks inside ``retrieve`` (spool drain + retention). The
        # retrieval itself always goes through ``backend``.
        self._remote = remote

    def tools(self) -> dict[str, Any]:
        return {
            "memory.retrieve": self.retrieve,
            "memory.synthesize_next_get_context": self.synthesize_next_get_context,
            "memory.synthesize_next_apply": self.synthesize_next_apply,
        }

    async def retrieve(self, args: dict[str, Any]) -> list[TextContent]:
        with _diag.trace(
            "mcp.memory.retrieve",
            project=args.get("project") or "<auto>",
            tech=args.get("tech"),
            phase=args.get("phase"),
            polarity=args.get("polarity"),
            query=args.get("query"),
        ):
            diag_cid: str | None = None
            t_retrieve = 0.0
            if _diag.enabled():
                diag_cid = uuid.uuid4().hex[:8]
                t_retrieve = time.monotonic()
                _diag.log(f"[bm-retrieve start cid={diag_cid}]")

            # Local best-effort pre-hooks run on the sqlite path only
            # (remote is None). In agentcore mode both would mutate local
            # episode/retention rows that no longer back retrieval; the
            # spool markers written by session hooks simply accumulate
            # un-drained there (files, not sqlite — no correctness impact).
            if self._remote is None:
                # 1. Drain spool — must happen before any retrieval so fresh
                #    hook events (session_start, commit_close) are processed.
                #    SpoolService.drain is idempotent.
                _diag.step("mcp.memory.retrieve", "before_spool_drain")
                run_best_effort(
                    "spool.drain", self._spool.drain, diag_cid=diag_cid
                )
                _diag.step("mcp.memory.retrieve", "after_spool_drain")

                # 2. Maybe run retention. Guard ensures at most once per 24h
                #    regardless of how often retrieve is called. Best-effort:
                #    a retention failure must NEVER block memory.retrieve.
                def _retention_step() -> None:
                    cfg = get_config()
                    RetentionScheduler(
                        self._memory_conn, auto_prune=cfg.auto_prune
                    ).maybe_run(triggered_by="retrieve")

                _diag.step("mcp.memory.retrieve", "before_retention_scheduler")
                run_best_effort(
                    "retention scheduler", _retention_step, diag_cid=diag_cid
                )
                _diag.step("mcp.memory.retrieve", "after_retention_scheduler")

            project = args.get("project") or project_name()
            # Default 5, not 20. An LLM working one task draws on ~5 memories
            # regardless of how many it is handed (measured: 5.3 +/- 2.4 useful
            # per session against 42.7 exposed). Everything past that dilutes
            # the set and burns context, so the default returns a shortlist and
            # callers that genuinely want the long tail ask for it.
            limit_per_bucket = args.get("limit_per_bucket", 5)
            t_reflections = time.monotonic() if diag_cid else 0.0
            _diag.step(
                "mcp.memory.retrieve",
                "before_retrieve_reflections",
                project=project,
            )
            buckets = self._backend.retrieve(
                project=project,
                tech=args.get("tech"),
                phase=args.get("phase"),
                polarity=args.get("polarity"),
                limit_per_bucket=limit_per_bucket,
                query=args.get("query"),
            )
            _diag.step(
                "mcp.memory.retrieve",
                "after_retrieve_reflections",
                do=len(buckets.get("do", [])),
                dont=len(buckets.get("dont", [])),
                neutral=len(buckets.get("neutral", [])),
            )
            if diag_cid is not None:
                refl_ms = int((time.monotonic() - t_reflections) * 1000)
                total_ms = int((time.monotonic() - t_retrieve) * 1000)
                _diag.log(
                    f"[bm-retrieve step=reflections cid={diag_cid} "
                    f"ms={refl_ms} status=ok]"
                )
                _diag.log(
                    f"[bm-retrieve done cid={diag_cid} total_ms={total_ms}]"
                )
            return [TextContent(type="text", text=json.dumps(buckets))]

    async def synthesize_next_get_context(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        with audit_synth_call(
            self._home,
            tool="get_context",
            project=project,
            episode_id=None,
        ) as audit:
            ctx = self._reflections.get_next_pending_context(project=project)
            queue = self._reflections._read_queue_counts(project=project)
            payload = serialize_synth_get_context(ctx, queue)
            if ctx is None:
                audit["result_kind"] = "empty"
            else:
                audit["result_kind"] = "episode"
                audit["episode_id"] = ctx.episode.id
                audit["obs_count"] = len(ctx.observations)
                audit["refl_count"] = len(ctx.reflections)
        return [TextContent(type="text", text=json.dumps(payload))]

    async def synthesize_next_apply(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        episode_id = args["episode_id"]
        decision = args["decision"]
        with audit_synth_call(
            self._home,
            tool="apply",
            project=project,
            episode_id=episode_id,
        ) as audit:
            try:
                # ``decision`` is already a parsed dict (the MCP
                # framework decoded it before dispatch). Use the
                # dict-shape parser directly to skip a redundant
                # json.dumps → json.loads round-trip.
                response = self._reflections.parse_response_dict(decision)
            except SynthesisResponseError as exc:
                audit["result_kind"] = "validation_error"
                audit["error"] = str(exc)
                payload = serialize_synth_apply_validation_error(str(exc))
                return [
                    TextContent(type="text", text=json.dumps(payload))
                ]
            try:
                step = self._reflections.apply_decision(
                    episode_id=episode_id,
                    response=response,
                    project=project,
                )
            except ValueError as exc:
                # Episode-state preconditions: not found / wrong
                # project / already synthesized. Surface as
                # structured error so the IDE-LLM can refetch
                # context instead of retrying the same stale id.
                audit["result_kind"] = "state_error"
                audit["error"] = str(exc)
                payload = serialize_synth_apply_state_error(str(exc))
                return [
                    TextContent(type="text", text=json.dumps(payload))
                ]
            audit["result_kind"] = "applied"
            audit["counts"] = step.counts
        return [
            TextContent(
                type="text",
                text=json.dumps(serialize_synth_apply_ok(step)),
            )
        ]
