"""Session-start hook.

Writes a ``session_start`` marker JSON file to the spool directory so the
MCP server's SpoolService.drain can lazy-open a background episode for
this session on first retrieve.

Accepts an optional stdin payload; if stdin is empty or unparseable, a marker
is synthesised from environment variables and the current time. Never raises;
always exits 0. Mirrors ``session_close.py``'s patterns for consistency.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# Mirror the observer cap: reject any stdin payload above 1 MiB without
# raising. Hooks must never fail.
_MAX_STDIN_BYTES = 1_048_576


def _default_spool_dir() -> Path:
    """Return ``$BETTER_MEMORY_HOME/spool``, defaulting to ``~/.better-memory``.

    Mirrors the observer hook. Kept duplicated to avoid a cross-module import
    that would slow hook startup.
    """
    home = os.environ.get("BETTER_MEMORY_HOME")
    if home:
        return Path(home).expanduser() / "spool"
    return Path.home() / ".better-memory" / "spool"


def _safe_timestamp(raw: str | None) -> str:
    if not raw:
        raw = datetime.now(UTC).isoformat()
    return raw.replace(":", "-")


def _synthesise_marker() -> dict[str, str]:
    """Build a minimal ``session_start`` payload from env + clock + cwd."""
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = os.environ.get("PWD") or "."
    return {
        "event_type": "session_start",
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": cwd,
        "project": Path(cwd).name,
        "session_id": os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex,
    }


def main() -> None:
    try:
        data: dict[str, object]

        raw_stdin = ""
        try:
            raw_stdin = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        except Exception:
            raw_stdin = ""

        if len(raw_stdin) > _MAX_STDIN_BYTES:
            sys.exit(0)

        parsed: object = None
        if raw_stdin.strip():
            try:
                parsed = json.loads(raw_stdin)
            except Exception:
                parsed = None

        if isinstance(parsed, dict):
            data = dict(parsed)
        else:
            data = dict(_synthesise_marker())

        # Always coerce event_type — this hook only ever emits session_start.
        data["event_type"] = "session_start"
        if "timestamp" not in data or not data["timestamp"]:
            data["timestamp"] = datetime.now(UTC).isoformat()
        if "session_id" not in data or not data["session_id"]:
            data["session_id"] = (
                os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
            )
        if "cwd" not in data or not data["cwd"]:
            try:
                data["cwd"] = os.getcwd()
            except Exception:
                data["cwd"] = os.environ.get("PWD") or "."
        # Derive project from cwd if not supplied.
        if "project" not in data or not data["project"]:
            data["project"] = Path(str(data["cwd"])).name

        spool_dir = _default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = _safe_timestamp(str(data.get("timestamp")))
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_session_start_{hash_hex}.json"
        (spool_dir / file_name).write_text(
            json.dumps(data), encoding="utf-8"
        )
    except Exception:
        # Hooks must never fail.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
