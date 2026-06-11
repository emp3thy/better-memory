"""Small shared helpers for the MCP server and its tool handlers.

Lives below both :mod:`better_memory.mcp.server` and the handler modules
under :mod:`better_memory.mcp.handlers` so either side can import it
without creating a cycle.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from better_memory import _diag
from better_memory.runtime.session_marker import read_session_id

logger = logging.getLogger(__name__)


def run_best_effort(
    operation: str,
    fn: Callable[[], Any],
    *,
    diag_cid: str | None = None,
) -> None:
    """Run ``fn`` swallowing any ``Exception`` but logging it via the module logger.

    Used by best-effort hooks inside ``memory.retrieve`` (spool drain,
    retention scheduler) where a failure must NEVER block the call but
    must still produce a discoverable diagnostic. The previous behaviour
    silently dropped the exception, so a broken background path could
    fail invisibly for weeks.

    When ``diag_cid`` is provided and ``BETTER_MEMORY_EMBED_LOG=1`` is on,
    emits a ``[bm-retrieve step=<operation> cid=... ms=N status=ok|error]``
    line through the shared diagnostic logger so callers can localize
    which step is slow.
    """
    t0 = time.monotonic()
    status = "ok"
    try:
        fn()
    except Exception:  # noqa: BLE001 — best-effort wrapper
        status = "error"
        logger.exception("best-effort %s failed", operation)
    finally:
        if diag_cid is not None and _diag.enabled():
            ms = int((time.monotonic() - t0) * 1000)
            _diag.log(
                f"[bm-retrieve step={operation} cid={diag_cid} "
                f"ms={ms} status={status}]"
            )


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
