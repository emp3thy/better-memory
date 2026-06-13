"""Session-domain MCP tool handlers.

Tools: memory.session_bootstrap, memory.start_ui.

Bodies lifted verbatim from the legacy ``_call_tool`` if-chain.
``session_bootstrap`` resolves the SessionStart session_id through a
4-tier fallback (args → ``CLAUDE_SESSION_ID`` env → ``CLAUDE_CODE_SESSION_ID``
env → fresh UUID) and serialises the ``SessionBootstrapResult`` into the
5-key payload (``additionalContext``, ``project``, ``source``,
``episode``, ``counts``) the SessionStart hook expects. ``start_ui``
delegates to the ``ui_launcher`` module which spawns or reuses the
management UI server.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler
from better_memory.services import ui_launcher

_BOOTSTRAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source": {
            "type": "string",
            "enum": ["startup", "resume", "clear", "compact"],
            "description": (
                "SessionStart payload source. Unknown values "
                "coerce to 'startup' inside the service."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Optional SessionStart payload session_id. "
                "Defaults to $CLAUDE_SESSION_ID env var, or a "
                "fresh UUID."
            ),
        },
        "cwd": {
            "type": "string",
            "description": (
                "Optional working directory. Defaults to "
                "server's process cwd."
            ),
        },
    },
}

_START_UI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

_BOOTSTRAP_DESCRIPTION = (
    "Open or reuse a session episode and inject all project + "
    "general semantic memories and reflections as "
    "additionalContext markdown. Mirrors what the SessionStart "
    "hook does; callable manually for recovery, testing, or "
    "post-/clear re-injection."
)

_START_UI_DESCRIPTION = (
    "Spawn or reuse the better-memory management UI. Returns "
    '{"url": str, "reused": bool}. Reuses an existing live UI '
    "when one is already running on /healthz."
)


class SessionHandlers:
    """The memory.session_bootstrap / memory.start_ui tools."""

    async def session_bootstrap(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        cwd_arg = args.get("cwd") or os.getcwd()
        session_id_arg = (
            args.get("session_id")
            or os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or uuid.uuid4().hex
        )
        result = services.session_bootstrap.bootstrap(
            source=args.get("source"),
            session_id=session_id_arg,
            cwd=Path(cwd_arg),
        )
        payload = {
            "additionalContext": result.additional_context,
            "project": result.project,
            "source": result.source,
            "episode": {
                "id": result.episode_id,
                "action": result.episode_action,
            },
            "counts": {
                "semantic": result.semantic_count,
                "reflections": result.reflections_counts,
            },
        }
        return [TextContent(type="text", text=json.dumps(payload))]

    async def start_ui(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        result = ui_launcher.start_ui()
        return [TextContent(type="text", text=json.dumps(result))]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.session_bootstrap",
                description=_BOOTSTRAP_DESCRIPTION,
                schema=_BOOTSTRAP_SCHEMA,
                call=self.session_bootstrap,
            ),
            Handler(
                name="memory.start_ui",
                description=_START_UI_DESCRIPTION,
                schema=_START_UI_SCHEMA,
                call=self.start_ui,
            ),
        ]
