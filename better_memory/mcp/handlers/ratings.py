"""Rating-domain MCP tool handlers.

Tools: memory.list_session_exposures, memory.apply_session_ratings,
memory.credit.

Bodies lifted verbatim from the legacy ``_call_tool`` if-chain. Session
resolution goes through :func:`resolve_session_id` so the handlers stay
testable without touching env vars or the marker file directly.

``apply_session_ratings`` raises a verbatim multi-line ``ValueError`` if
no session is active — the rate-session-memories skill depends on the
exact message text. ``credit`` has two payload shapes: the no-session
shape ``{"applied": None, "skipped": "no_session"}`` and the
``credit_one`` outcome shape. ``list_session_exposures`` coerces an
unresolved session to ``""`` so the service receives a string and the
caller gets an empty-exposures payload back instead of an exception.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.mcp._session import resolve_session_id
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler

_LIST_SESSION_EXPOSURES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

_APPLY_SESSION_RATINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ratings"],
    "properties": {
        "ratings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id", "class"],
                "properties": {
                    "kind": {"enum": ["reflection", "semantic"]},
                    "id": {"type": "string"},
                    "class": {
                        "enum": [
                            "cited", "shaped", "ignored",
                            "misled", "overlooked",
                        ]
                    },
                },
            },
        },
    },
}

_CREDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "id", "class"],
    "properties": {
        "kind": {"enum": ["reflection", "semantic"]},
        "id": {"type": "string"},
        "class": {"enum": ["cited", "shaped", "misled", "overlooked"]},
    },
}

_LIST_SESSION_EXPOSURES_DESCRIPTION = (
    "Return the unrated session_memory_exposure rows for the "
    "current Claude session (resolved server-side from "
    "CLAUDE_SESSION_ID env). Read-only; no side effects. "
    "Used by the rate-session-memories skill as the "
    "authoritative anti-hallucination list."
)

_APPLY_SESSION_RATINGS_DESCRIPTION = (
    "Atomic batch rating for the current Claude session "
    "(resolved server-side from CLAUDE_SESSION_ID). Called "
    "at session end by the rate-session-memories skill. "
    "Raises if CLAUDE_SESSION_ID is unset — call only inside "
    "an active Claude session."
)

_CREDIT_DESCRIPTION = (
    "Per-tool-use credit. When you actively use a memory "
    "retrieved during this session (quote it, follow its "
    "guidance, or it misled you), call this immediately. "
    "Resolved server-side from CLAUDE_SESSION_ID. "
    "class must be 'cited', 'shaped', 'misled', or "
    "'overlooked' — NOT 'ignored'. Use 'overlooked' when the "
    "user pointed you back to a memory you already had but "
    "had not applied."
)


class RatingHandlers:
    """The three rating tools: list_session_exposures + apply + credit."""

    async def list_session_exposures(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        sid = resolve_session_id(services.config.home) or ""
        payload = services.session_bootstrap.list_session_exposures(
            session_id=sid,
        )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def apply_session_ratings(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        sid = resolve_session_id(services.config.home)
        if not sid:
            raise ValueError(
                "No active session: CLAUDE_SESSION_ID / "
                "CLAUDE_CODE_SESSION_ID not set and no session marker "
                "found (SessionStart hook may not have run)"
            )
        payload = services.memory_rating.apply_session_ratings(
            session_id=sid,
            ratings=args["ratings"],
        )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def credit(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        sid = resolve_session_id(services.config.home)
        payload: Any
        if not sid:
            payload = {"applied": None, "skipped": "no_session"}
        else:
            payload = services.memory_rating.credit_one(
                session_id=sid,
                kind=args["kind"],
                id=args["id"],
                classification=args["class"],
            )
        return [TextContent(type="text", text=json.dumps(payload))]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.list_session_exposures",
                description=_LIST_SESSION_EXPOSURES_DESCRIPTION,
                schema=_LIST_SESSION_EXPOSURES_SCHEMA,
                call=self.list_session_exposures,
            ),
            Handler(
                name="memory.apply_session_ratings",
                description=_APPLY_SESSION_RATINGS_DESCRIPTION,
                schema=_APPLY_SESSION_RATINGS_SCHEMA,
                call=self.apply_session_ratings,
            ),
            Handler(
                name="memory.credit",
                description=_CREDIT_DESCRIPTION,
                schema=_CREDIT_SCHEMA,
                call=self.credit,
            ),
        ]
