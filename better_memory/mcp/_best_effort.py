"""Best-effort runner used by handlers that must never fail their caller.

Extracted from ``better_memory.mcp.server`` so handler modules can import
it without pulling the server module (which itself imports handlers via
``all_handlers``) and creating an import cycle.

The canonical re-export remains ``better_memory.mcp.server._run_best_effort``
for the test suite (``tests/mcp/test_best_effort_logging.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from better_memory import _diag

logger = logging.getLogger(__name__)


def _run_best_effort(
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
