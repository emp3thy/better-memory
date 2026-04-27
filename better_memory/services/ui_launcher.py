"""Spawn / reuse the better-memory management UI as a detached subprocess.

The MCP handler ``memory.start_ui`` is a thin passthrough to ``start_ui()``.
This module owns:

* Liveness detection via HTTP GET against ``$BETTER_MEMORY_HOME/ui.url``.
* Stale ``ui.url`` cleanup when the recorded URL no longer responds.
* Detached subprocess spawn (``python -m better_memory.ui``) with
  platform-specific detach flags so the UI survives MCP server termination.
* Stdout/stderr capture to ``$BETTER_MEMORY_HOME/ui.log``.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from better_memory.config import resolve_home

_HEALTHZ_TIMEOUT_SEC = 1.0


def _is_alive(url: str) -> bool:
    """Return True iff GET <url>/healthz returns 200 within the timeout."""
    probe = url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(  # noqa: S310 — local-only loopback URL
            probe, timeout=_HEALTHZ_TIMEOUT_SEC
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_ui() -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.
    """
    home = resolve_home()
    url_path = home / "ui.url"

    if url_path.exists():
        try:
            url = url_path.read_text().strip()
        except OSError:
            url = ""
        if url and _is_alive(url):
            return {"url": url, "reused": True}

    raise NotImplementedError("spawn path lands in Task 3")
