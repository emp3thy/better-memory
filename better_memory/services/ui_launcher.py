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

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from better_memory.config import resolve_home

_HEALTHZ_TIMEOUT_SEC = 1.0
_DEFAULT_SPAWN_TIMEOUT_SEC = 10.0
_DEFAULT_CONFIRM_RETRY_SLEEP_SEC = 1.0
_POLL_INTERVAL_SEC = 0.05  # 50 ms — chosen to keep the spawn-test race window <25 ms.

# Windows-only constants. We resolve via getattr so tests on POSIX runners
# (where these attributes do not exist on the subprocess module) can still
# import this module and exercise the win32 branch via monkeypatched
# sys.platform without triggering AttributeError. The integer values are
# the documented Win32 process-creation flags and are stable.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def _is_alive(url: str) -> bool:
    """Return True iff GET <url>/healthz returns 200 within the timeout."""
    probe = url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(  # noqa: S310 — local-only loopback URL
            probe, timeout=_HEALTHZ_TIMEOUT_SEC
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _detach_kwargs() -> dict:
    """Platform-specific Popen kwargs that detach the child from the parent."""
    if sys.platform == "win32":
        return {
            "creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def _spawn(home: Path) -> None:
    """Spawn the UI subprocess. Stdout/stderr go to ui.log."""
    log_path = home / "ui.log"
    try:
        log_fh = log_path.open("ab")
    except OSError as exc:
        raise RuntimeError(
            f"cannot write to BETTER_MEMORY_HOME ({home}): {exc}"
        ) from exc

    with log_fh:
        try:
            subprocess.Popen(
                [sys.executable, "-m", "better_memory.ui"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
                **_detach_kwargs(),
            )
        except OSError as exc:
            raise RuntimeError(
                f"failed to spawn UI subprocess: {exc}"
            ) from exc


def _wait_for_url(url_path: Path, timeout: float) -> str:
    """Poll url_path until it appears or the deadline is hit. Return its content."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if url_path.exists():
            try:
                return url_path.read_text().strip()
            except OSError:
                pass
        time.sleep(_POLL_INTERVAL_SEC)
    raise RuntimeError(
        f"UI did not write ui.url within {timeout}s; check ui.log"
    )


def start_ui(
    *,
    spawn_timeout: float = _DEFAULT_SPAWN_TIMEOUT_SEC,
    confirm_retry_sleep: float = _DEFAULT_CONFIRM_RETRY_SLEEP_SEC,
) -> dict:
    """Return ``{"url": str, "reused": bool}``. Raises on failure.

    See ``docs/superpowers/specs/2026-04-26-start-ui-mcp-design.md`` for the
    full liveness / spawn flow.

    ``spawn_timeout`` and ``confirm_retry_sleep`` are exposed so tests can
    short-circuit the 10 s and 1 s defaults respectively.
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
        try:
            url_path.unlink()
        except FileNotFoundError:
            pass

    _spawn(home)
    url = _wait_for_url(url_path, timeout=spawn_timeout)
    if not _is_alive(url):
        # One short retry to absorb the gap between "url file written" and
        # "Werkzeug accepts connections".
        time.sleep(confirm_retry_sleep)
        if not _is_alive(url):
            raise RuntimeError(
                "UI wrote ui.url but /healthz did not respond"
            )
    return {"url": url, "reused": False}
