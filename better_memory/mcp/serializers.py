"""JSON payload builders shared by the MCP tool handlers.

Pure functions: domain read-models in, plain JSON-ready dicts out. The
handler modules call these so the wire format is defined in exactly one
place and unit tests can assert on it without driving a live server.
"""

from __future__ import annotations

import json
from typing import Any

from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
)
from better_memory.services.reflection import (
    EpisodeContext,
    EpisodeQueueCounts,
    SynthesisStep,
)


def serialize_knowledge_search(result: KnowledgeSearchResult) -> dict[str, Any]:
    doc = result.document
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
        "rank": result.rank,
    }


def serialize_knowledge_doc(doc: KnowledgeDocument) -> dict[str, Any]:
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
    }


def serialize_queue(queue: EpisodeQueueCounts) -> dict[str, int]:
    return {
        "pending": queue.pending,
        "in_cooldown": queue.in_cooldown,
        "done": queue.done,
    }


def serialize_synth_get_context(
    ctx: EpisodeContext | None,
    queue: EpisodeQueueCounts,
) -> dict[str, Any]:
    """Build the JSON payload for ``memory.synthesize_next_get_context``.

    Returns ``{"episode_id": null, "queue": {...}}`` when the queue is
    empty. Otherwise returns the full episode + observations + reflections
    bundle the IDE-LLM consumes to decide on synthesis actions.
    """
    queue_json = serialize_queue(queue)
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


def serialize_synth_apply_ok(step: SynthesisStep) -> dict[str, Any]:
    """Build the JSON payload for a successful ``memory.synthesize_next_apply``."""
    return {
        "ok": True,
        "episode_id": step.episode_id,
        "counts": step.counts,
        "queue": serialize_queue(step.queue),
    }


def serialize_synth_apply_validation_error(message: str) -> dict[str, Any]:
    """Build the JSON payload for a decision-validation failure.

    Validation errors do NOT stamp ``synth_failed_at``; the caller can
    retry with a corrected payload.
    """
    return {
        "ok": False,
        "error": "validation",
        "message": message,
    }


def serialize_synth_apply_state_error(message: str) -> dict[str, Any]:
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
