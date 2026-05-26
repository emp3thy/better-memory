"""Identity + payload helpers for the AgentCore backend.

Pure stdlib — no boto3 dependency. These helpers are used by
AgentCoreBackend (per-call namespace + session resolution) and by the
Stop hook (closure event payload, in Plan 3). Kept separate from
agentcore.py so that the Stop hook can import them without pulling in
boto3 (the Stop hook must remain fast — no boto3 import unless we're
actually firing an agentcore-mode closure event).
"""

from __future__ import annotations

import os
import re
from typing import Literal
from uuid import uuid4

_NamespaceKind = Literal["reflections", "episodes", "semantic", "retired"]
_VALID_NAMESPACE_KINDS: tuple[_NamespaceKind, ...] = (
    "reflections",
    "episodes",
    "semantic",
    "retired",
)


def resolve_actor_id(project: str | None) -> str:
    """Return the AgentCore actorId for this project, or `"general"` if no
    project is in scope (cross-project bucket)."""
    if not project:
        return "general"
    return project


def resolve_session_id() -> str:
    """Return the current Claude session id, generating one if no env var
    is set. Reads CLAUDE_SESSION_ID first, then CLAUDE_CODE_SESSION_ID,
    then generates a uuid4 hex (32 chars)."""
    return (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or uuid4().hex
    )


def resolve_namespace(actor_id: str, kind: _NamespaceKind) -> str:
    """Build the AgentCore namespace string for a (actor, kind) pair.

    Tree (per spec):
        projects/{actorId}/reflections/             <- reflection records
        projects/{actorId}/reflections/episodes/    <- episode records (nested)
        projects/{actorId}/semantic/                <- user-preference records
        projects/{actorId}/retired/                 <- retired records
        general/...                                  <- cross-project bucket
    """
    if kind not in _VALID_NAMESPACE_KINDS:
        raise ValueError(
            f"kind must be one of {_VALID_NAMESPACE_KINDS}, got {kind!r}"
        )
    root = "general" if actor_id == "general" else f"projects/{actor_id}"
    if kind == "episodes":
        # Nested under reflections (spec line 123).
        return f"{root}/reflections/episodes/"
    return f"{root}/{kind}/"


def closure_event_payload() -> list[dict]:
    """Canonical closure-marker payload for the Stop hook's final CreateEvent.

    Conversational.role enum per boto3 surface: ASSISTANT | USER | TOOL | OTHER.
    OTHER is the right semantic match for a system-emitted closure marker;
    spec example (line 177-184) previously used USER but was corrected in
    the Verified API surface section."""
    return [
        {
            "conversational": {
                "role": "OTHER",
                "content": {
                    "text": "Session complete. All work for this session has been recorded."
                },
            }
        }
    ]


_HINTS_BULLET_SPLIT = re.compile(r"\n-\s+")


def parse_hints_prose(prose: str) -> list[str]:
    """Split AgentCore's reflection hints (single prose string) into a
    list[str] for better-memory's Reflection.hints field.

    Working approach (per spec Open Question 1):
    - Split on the markdown bullet pattern `\\n- ` (newline then dash-space).
    - If no bullets, return [prose] as a single element.
    - Empty input returns []."""
    if not prose:
        return []
    parts = _HINTS_BULLET_SPLIT.split(prose)
    return [p.strip() for p in parts if p.strip()]
