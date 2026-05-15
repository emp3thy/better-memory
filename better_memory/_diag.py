"""Diagnostic stderr+file logging gated by :class:`~better_memory.config.Config`.

When diagnostics are on, :func:`log` writes ``msg`` to stderr AND
appends it to ``{config.home}/logs/diag.log``. The file is the reliable
channel: Claude Code's MCP framework captures the CLI's own debug
messages into ``mcp-logs-<server>/*.jsonl`` but does NOT capture the
spawned server's stderr, so without the file mirror these lines are
discarded when running under Claude Code.

The gate resolves from the ``diag_logging`` config field, with the
``BETTER_MEMORY_EMBED_LOG`` environment variable kept as an emergency
override (env wins) so diagnostics can still be flipped without editing
config. The env var is named ``BETTER_MEMORY_EMBED_LOG`` for historical
reasons (originally added to localize Ollama embed hangs).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

# Truthiness gate, resolved lazily on first consult and memoized; ``None``
# means "not yet resolved". Resolution is deferred rather than computed at
# import time because the gate now consults the ``diag_logging`` config
# field — and ``config`` imports this module, so reading config at import
# time would be circular. By the time the first ``log``/``step``/``trace``
# call fires, ``config`` has finished importing and ``get_config()`` is safe.
_ENABLED: bool | None = None

# Values of ``BETTER_MEMORY_EMBED_LOG`` that mean "off"; any other non-empty
# value means "on", preserving the historic env-var semantics.
_ENV_OFF_VALUES = frozenset({"0", "false", "no", "off"})

_LOCK = threading.Lock()
_FILE_HANDLE: object | None = None  # actually a text file, typed loosely to avoid import
_FILE_INIT_ATTEMPTED = False


def _resolve_enabled() -> bool:
    """Resolve whether diagnostics are on.

    ``BETTER_MEMORY_EMBED_LOG`` is an emergency override: when set to a
    non-empty value it wins outright (env wins), keeping the run-time-flip
    workflow intact. Otherwise the gate falls back to the ``diag_logging``
    field on :class:`~better_memory.config.Config`.
    """
    raw = os.environ.get("BETTER_MEMORY_EMBED_LOG", "").strip()
    if raw:
        return raw.lower() not in _ENV_OFF_VALUES
    try:
        from better_memory.config import get_config

        return get_config().diag_logging
    except Exception:  # noqa: BLE001 — diagnostics must never break bootstrap
        return False


def enabled() -> bool:
    """Return True if diagnostic logging is on (memoized after first call)."""
    global _ENABLED
    cached = _ENABLED
    if cached is None:
        cached = _resolve_enabled()
        _ENABLED = cached
    return cached


def _reset_enabled_cache() -> None:
    """Clear the memoized :func:`enabled` result. Test hook only."""
    global _ENABLED
    _ENABLED = None


def _resolve_path() -> Path:
    """Resolve the diag log path. Lazy-imported config to avoid cycles."""
    from better_memory.config import resolve_home

    return resolve_home() / "logs" / "diag.log"


def log(msg: str) -> None:
    """Write a diagnostic line to stderr AND append to the diag file.

    Best-effort: any IO failure (stderr closed, file unwritable) is
    swallowed. Calls are cheap when diagnostics are off — a single
    boolean check after the gate is first resolved.
    """
    if not enabled():
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


def _fmt_fields(fields: dict[str, object]) -> str:
    """Render kv pairs as `` k=v k2=v2``; empty mapping -> empty string."""
    if not fields:
        return ""
    return " " + " ".join(f"{k}={v}" for k, v in fields.items())


def step(fn: str, msg: str, **fields: object) -> None:
    """Emit an intermediate ``[bm-trace step ...]`` line.

    Use inside a method already wrapped in :func:`trace` to mark a
    sub-step (e.g. "about to call self._embedder.embed", "SAVEPOINT
    open", "commit"). Cheap when diagnostics are off.
    """
    if not enabled():
        return
    log(
        f"[bm-trace step  fn={fn} msg={msg}{_fmt_fields(fields)} "
        f"ts={datetime.now(UTC).isoformat()}]"
    )


@contextlib.contextmanager
def trace(fn: str, **fields: object) -> Iterator[None]:
    """Context manager that emits paired enter/exit trace lines.

    Wraps a method/function call to localize where execution sits. The
    enter line is emitted BEFORE the body runs so a hang inside the
    body leaves an enter without an exit — pointing directly at the
    stuck call. Cheap when diagnostics are off (single boolean check).
    """
    if not enabled():
        yield
        return
    extra = _fmt_fields(fields)
    log(f"[bm-trace enter fn={fn}{extra} ts={datetime.now(UTC).isoformat()}]")
    t0 = time.monotonic()
    status = "ok"
    try:
        yield
    except BaseException:
        status = "error"
        # Wallclock + monotonic NOW (before finally runs the log call) so we
        # can pinpoint where time evaporates during exception unwind.
        log(
            f"[bm-trace except fn={fn}{extra} "
            f"ms={int((time.monotonic() - t0) * 1000)} "
            f"ts={datetime.now(UTC).isoformat()}]"
        )
        raise
    finally:
        ms = int((time.monotonic() - t0) * 1000)
        log(
            f"[bm-trace exit  fn={fn}{extra} ms={ms} status={status} "
            f"ts={datetime.now(UTC).isoformat()}]"
        )
