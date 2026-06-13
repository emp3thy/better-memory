"""Synthesis-domain MCP tool handlers.

Tools: memory.synthesize_next_get_context, memory.synthesize_next_apply.
Both are CAPABILITY-GATED (``requires_synthesis=True``) — the dispatcher
hides them when ``backend.supports_synthesis`` is False.

Bodies lifted verbatim from the legacy ``_call_tool`` if-chain to
preserve every documented invariant: the audit bracket on both tools
(start + complete JSONL rows in ``logs/synthesize.jsonl``), the
SynthesisResponseError -> validation_error fork on apply, the
ValueError -> state_error fork on apply (so the IDE-LLM refetches
context rather than retrying a stale episode_id), and the four
serializer payload shapes the IDE-LLM consumes.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.config import project_name
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler
from better_memory.mcp.handlers._audit import _audit_synth_call
from better_memory.services.reflection import (
    EpisodeContext,
    EpisodeQueueCounts,
    SynthesisResponseError,
    SynthesisStep,
)

_GET_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project": {
            "type": "string",
            "description": (
                "Optional project override; defaults to "
                "cwd-derived."
            ),
        },
    },
}

_APPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["episode_id", "decision"],
    "additionalProperties": False,
    "properties": {
        "episode_id": {"type": "string"},
        "decision": {
            "type": "object",
            "description": (
                "Decision JSON; validation errors are "
                "returned to the caller without stamping "
                "the episode failed (caller can retry)."
            ),
        },
        "project": {
            "type": "string",
            "description": (
                "Optional project override; defaults to "
                "cwd-derived."
            ),
        },
    },
}

_GET_CONTEXT_DESCRIPTION = (
    "Return the next pending episode's full context for "
    "consolidation: episode metadata, all observations on it, "
    "and tech-filtered existing reflections. The IDE-LLM "
    "consumes this, decides what new/augment/merge/ignore "
    "actions to take, and submits the decision via "
    "memory.synthesize_next_apply. Returns "
    '{"episode_id": null, "queue": {...}} when the queue is '
    "empty. See the better-memory-synthesize skill for the "
    "full workflow and decision schema."
)
_APPLY_DESCRIPTION = (
    "Apply a synthesis decision for one episode. Atomically "
    "creates new reflections, augments existing ones, merges "
    "duplicates, and marks observations as consumed (or "
    "ignored). Marks the episode synthesized. Returns a "
    "step summary {episode_id, counts, queue, failure}. "
    "decision shape: {new: [...], augment: [...], "
    "merge: [...], ignore: [...]} — see the "
    "better-memory-synthesize skill for the per-entry "
    "field schema."
)


def _serialize_queue(queue: EpisodeQueueCounts) -> dict[str, int]:
    return {
        "pending": queue.pending,
        "in_cooldown": queue.in_cooldown,
        "done": queue.done,
    }


def _serialize_synth_get_context(
    ctx: EpisodeContext | None,
    queue: EpisodeQueueCounts,
) -> dict[str, Any]:
    """Build the JSON payload for ``memory.synthesize_next_get_context``.

    Returns ``{"episode_id": null, "queue": {...}}`` when the queue is
    empty. Otherwise returns the full episode + observations + reflections
    bundle the IDE-LLM consumes to decide on synthesis actions.
    """
    queue_json = _serialize_queue(queue)
    if ctx is None:
        return {"episode_id": None, "queue": queue_json}
    return {
        "episode_id": ctx.episode.id,
        "queue": queue_json,
        "episode": {
            "id": ctx.episode.id,
            "project": ctx.episode.project,
            "goal": ctx.episode.goal,
            "tech": ctx.episode.tech,
            "outcome": ctx.episode.outcome,
        },
        "observations": [
            {
                "id": o.id,
                "content": o.content,
                "outcome": o.outcome,
                "component": o.component,
                "theme": o.theme,
                "tech": o.tech,
                "created_at": o.created_at,
                "status": o.status,
            }
            for o in ctx.observations
        ],
        "reflections": [
            {
                "id": r.id,
                "title": r.title,
                "tech": r.tech,
                "phase": r.phase,
                "polarity": r.polarity,
                "use_cases": r.use_cases,
                "hints": json.loads(r.hints) if r.hints else [],
                "confidence": r.confidence,
                "status": r.status,
            }
            for r in ctx.reflections
        ],
    }


def _serialize_synth_apply_ok(step: SynthesisStep) -> dict[str, Any]:
    """Build the JSON payload for a successful ``memory.synthesize_next_apply``."""
    return {
        "ok": True,
        "episode_id": step.episode_id,
        "counts": step.counts,
        "queue": _serialize_queue(step.queue),
    }


def _serialize_synth_apply_validation_error(message: str) -> dict[str, Any]:
    """Build the JSON payload for a decision-validation failure.

    Validation errors do NOT stamp ``synth_failed_at``; the caller can
    retry with a corrected payload.
    """
    return {
        "ok": False,
        "error": "validation",
        "message": message,
    }


def _serialize_synth_apply_state_error(message: str) -> dict[str, Any]:
    """Build the JSON payload for an episode-state failure.

    Surfaces apply-time precondition violations (episode not found,
    wrong project, already synthesized) without raising into the MCP
    framework's generic isError surface. Caller should NOT retry the
    same episode_id; pull fresh context via
    ``memory.synthesize_next_get_context`` first.
    """
    return {
        "ok": False,
        "error": "state",
        "message": message,
    }


class ReflectionHandlers:
    """The memory.synthesize_next_get_context / .synthesize_next_apply tools."""

    async def get_context(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        with _audit_synth_call(
            services.config.home,
            tool="get_context",
            project=project,
            episode_id=None,
        ) as audit:
            ctx = services.reflections.get_next_pending_context(project=project)
            queue = services.reflections.read_queue_counts(project=project)
            payload = _serialize_synth_get_context(ctx, queue)
            if ctx is None:
                audit["result_kind"] = "empty"
            else:
                audit["result_kind"] = "episode"
                audit["episode_id"] = ctx.episode.id
                audit["obs_count"] = len(ctx.observations)
                audit["refl_count"] = len(ctx.reflections)
        return [TextContent(type="text", text=json.dumps(payload))]

    async def apply(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        episode_id = args["episode_id"]
        decision = args["decision"]
        with _audit_synth_call(
            services.config.home,
            tool="apply",
            project=project,
            episode_id=episode_id,
        ) as audit:
            try:
                # ``decision`` is already a parsed dict (the MCP
                # framework decoded it before dispatch). Use the
                # dict-shape parser directly to skip a redundant
                # json.dumps → json.loads round-trip.
                response = services.reflections.parse_response_dict(decision)
            except SynthesisResponseError as exc:
                audit["result_kind"] = "validation_error"
                audit["error"] = str(exc)
                payload = _serialize_synth_apply_validation_error(str(exc))
                return [TextContent(type="text", text=json.dumps(payload))]
            try:
                step = services.reflections.apply_decision(
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
                payload = _serialize_synth_apply_state_error(str(exc))
                return [TextContent(type="text", text=json.dumps(payload))]
            audit["result_kind"] = "applied"
            audit["counts"] = step.counts
        return [
            TextContent(
                type="text",
                text=json.dumps(_serialize_synth_apply_ok(step)),
            )
        ]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.synthesize_next_get_context",
                description=_GET_CONTEXT_DESCRIPTION,
                schema=_GET_CONTEXT_SCHEMA,
                call=self.get_context,
                requires_synthesis=True,
            ),
            Handler(
                name="memory.synthesize_next_apply",
                description=_APPLY_DESCRIPTION,
                schema=_APPLY_SCHEMA,
                call=self.apply,
                requires_synthesis=True,
            ),
        ]
