"""Resolve the Claude Code session id for MCP tool calls.

Moved out of ``better_memory.mcp.server`` so handlers in
``better_memory.mcp.handlers.*`` can import it without a circular
dependency on the server module.
"""
from __future__ import annotations

import os
from pathlib import Path

from better_memory.runtime.session_marker import read_session_id


def resolve_session_id(home: Path) -> str | None:
    """Resolve the current Claude Code session id.

    Order: ``CLAUDE_SESSION_ID`` env, ``CLAUDE_CODE_SESSION_ID`` env, then
    the marker file written by the SessionStart hook (see
    :mod:`better_memory.runtime.session_marker`). Claude Code does not
    propagate the session id into the spawned stdio MCP server's env, so
    the marker file is the fallback for every rating call.
    """
    return (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or read_session_id(home)
    )
