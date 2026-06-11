"""Handlers for session-scoped tools: bootstrap, rating, credit, UI.

Tools: ``memory.session_bootstrap``, ``memory.list_session_exposures``,
``memory.apply_session_ratings``, ``memory.credit``, ``memory.start_ui``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from better_memory.mcp._util import resolve_session_id
from better_memory.services import ui_launcher
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.session_bootstrap import SessionBootstrapService


class SessionToolHandlers:
    """Session bootstrap + exposure rating + management-UI launch."""

    def __init__(
        self,
        *,
        session_bootstrap: SessionBootstrapService,
        memory_rating: MemoryRatingService,
        home: Path,
    ) -> None:
        self._session_bootstrap = session_bootstrap
        self._memory_rating = memory_rating
        self._home = home

    def tools(self) -> dict[str, Any]:
        return {
            "memory.session_bootstrap": self.session_bootstrap,
            "memory.list_session_exposures": self.list_session_exposures,
            "memory.apply_session_ratings": self.apply_session_ratings,
            "memory.credit": self.credit,
            "memory.start_ui": self.start_ui,
        }

    async def session_bootstrap(self, args: dict[str, Any]) -> list[TextContent]:
        cwd_arg = args.get("cwd") or os.getcwd()
        session_id_arg = (
            args.get("session_id")
            or os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or uuid.uuid4().hex
        )
        result = self._session_bootstrap.bootstrap(
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

    async def list_session_exposures(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        sid = resolve_session_id(self._home) or ""
        payload = self._session_bootstrap.list_session_exposures(
            session_id=sid,
        )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def apply_session_ratings(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        sid = resolve_session_id(self._home)
        if not sid:
            raise ValueError(
                "No active session: CLAUDE_SESSION_ID / "
                "CLAUDE_CODE_SESSION_ID not set and no session marker "
                "found (SessionStart hook may not have run)"
            )
        payload = self._memory_rating.apply_session_ratings(
            session_id=sid,
            ratings=args["ratings"],
        )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def credit(self, args: dict[str, Any]) -> list[TextContent]:
        sid = resolve_session_id(self._home)
        if not sid:
            payload = {"applied": None, "skipped": "no_session"}
        else:
            payload = self._memory_rating.credit_one(
                session_id=sid,
                kind=args["kind"],
                id=args["id"],
                classification=args["class"],
            )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def start_ui(self, args: dict[str, Any]) -> list[TextContent]:
        result = ui_launcher.start_ui()
        return [
            TextContent(type="text", text=json.dumps(result))
        ]
