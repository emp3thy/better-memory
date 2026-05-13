"""Diagnostic stderr+file logging gated by ``BETTER_MEMORY_EMBED_LOG``.

When the env var is truthy, :func:`log` writes ``msg`` to stderr AND
appends it to ``{config.home}/logs/diag.log``. The file is the reliable
channel: Claude Code's MCP framework captures the CLI's own debug
messages into ``mcp-logs-<server>/*.jsonl`` but does NOT capture the
spawned server's stderr, so without the file mirror these lines are
discarded when running under Claude Code.

The env var is named ``BETTER_MEMORY_EMBED_LOG`` for historical reasons
(originally added to localize Ollama embed hangs) but now gates all
diagnostic prints across the package.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_ENABLED = os.environ.get("BETTER_MEMORY_EMBED_LOG", "").strip() not in (
    "",
    "0",
    "false",
    "False",
)

_LOCK = threading.Lock()
_FILE_HANDLE: object | None = None  # actually a text file, typed loosely to avoid import
_FILE_INIT_ATTEMPTED = False


def enabled() -> bool:
    """Return True if diagnostic logging is on."""
    return _ENABLED


def _resolve_path() -> Path:
    """Resolve the diag log path. Lazy-imported config to avoid cycles."""
    from better_memory.config import resolve_home

    return resolve_home() / "logs" / "diag.log"


def log(msg: str) -> None:
    """Write a diagnostic line to stderr AND append to the diag file.

    Best-effort: any IO failure (stderr closed, file unwritable) is
    swallowed. Calls are cheap when the env var is off — a single
    boolean check.
    """
    if not _ENABLED:
        return
    line = msg + "\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — best-effort diagnostic
        pass

    global _FILE_HANDLE, _FILE_INIT_ATTEMPTED
    try:
        with _LOCK:
            if _FILE_HANDLE is None and not _FILE_INIT_ATTEMPTED:
                _FILE_INIT_ATTEMPTED = True
                path = _resolve_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                _FILE_HANDLE = path.open("a", encoding="utf-8")
            if _FILE_HANDLE is not None:
                _FILE_HANDLE.write(line)  # type: ignore[attr-defined]
                _FILE_HANDLE.flush()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort diagnostic
        pass
