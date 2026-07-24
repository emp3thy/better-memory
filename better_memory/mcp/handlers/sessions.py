"""Handlers for session-scoped tools: bootstrap, rating, credit, UI.

Tools: ``memory.session_bootstrap``, ``memory.list_session_exposures``,
``memory.apply_session_ratings``, ``memory.credit``, ``memory.start_ui``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from better_memory._common import get_session_id
from better_memory.mcp._util import resolve_session_id
from better_memory.services import ui_launcher
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.session_bootstrap import SessionBootstrapService
from better_memory.storage import StorageBackend


class SessionToolHandlers:
    """Session bootstrap + exposure rating + management-UI launch.

    ``remote`` (agentcore mode) routes bootstrap/rating to the
    StorageBackend; ``None`` (the sqlite default) keeps the original
    service path unchanged.
    """

    def __init__(
        self,
        *,
        session_bootstrap: SessionBootstrapService,
        memory_rating: MemoryRatingService,
        home: Path,
        remote: StorageBackend | None = None,
    ) -> None:
        self._session_bootstrap = session_bootstrap
        self._memory_rating = memory_rating
        self._home = home
        self._remote = remote

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
        session_id_arg = args.get("session_id") or get_session_id()
        if self._remote is not None:
            # AgentCoreBackend.session_bootstrap returns a DICT (not the
            # sqlite BootstrapResult dataclass) — unwrap by key. The
            # pending_synthesis-adjacent fields don't exist in agentcore
            # mode and are intentionally absent from the payload (same as
            # the sqlite payload below, which never carried them either).
            remote_result = self._remote.session_bootstrap(
                session_id=session_id_arg,
                source=args.get("source"),
                cwd=Path(cwd_arg),
            )
            payload = {
                "additionalContext": remote_result["additional_context"],
                "project": remote_result["project"],
                "source": remote_result["source"],
                "episode": {
                    "id": remote_result["episode_id"],
                    "action": remote_result["episode_action"],
                },
                "counts": {
                    "semantic": remote_result["semantic_count"],
                    "reflections": remote_result["reflections_counts"],
                },
            }
            return [TextContent(type="text", text=json.dumps(payload))]
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
        if self._remote is not None:
            payload = self._remote.list_session_exposures(session_id=sid)
            return [TextContent(type="text", text=json.dumps(payload))]
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
        if self._remote is not None:
            payload = self._remote.apply_session_ratings(
                session_id=sid,
                ratings=args["ratings"],
            )
            return [TextContent(type="text", text=json.dumps(payload))]
        payload = self._memory_rating.apply_session_ratings(
            session_id=sid,
            ratings=args["ratings"],
        )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def credit(self, args: dict[str, Any]) -> list[TextContent]:
        sid = resolve_session_id(self._home)
        if not sid:
            payload = {"applied": None, "skipped": "no_session"}
        elif self._remote is not None:
            payload = self._remote.credit_one(
                session_id=sid,
                kind=args["kind"],
                id=args["id"],
                classification=args["class"],
            )
        else:
            payload = self._memory_rating.credit_one(
                session_id=sid,
                kind=args["kind"],
                id=args["id"],
                classification=args["class"],
                evidence=args.get("evidence"),
            )
        return [TextContent(type="text", text=json.dumps(payload))]

    async def start_ui(self, args: dict[str, Any]) -> list[TextContent]:
        result = ui_launcher.start_ui()
        return [
            TextContent(type="text", text=json.dumps(result))
        ]
