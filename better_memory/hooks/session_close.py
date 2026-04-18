"""Stop / session-close hook.

Writes a ``session_end`` marker JSON file to the spool directory so the
consolidation service can use session boundaries when clustering observations.

Accepts an optional stdin payload; if stdin is empty or unparseable, a marker
is synthesised from environment variables and the current time. Never raises;
always exits 0.
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
    """Build a minimal ``session_end`` payload from env + clock."""
    return {
        "event_type": "session_end",
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": os.environ.get("PWD") or os.getcwd(),
        "session_id": os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex,
    }


def main() -> None:
    try:
        data: dict[str, object]

        raw_stdin = ""
        try:
            # Read one byte past the cap so we can detect oversize without
            # holding more than MAX+1 bytes in memory.
            raw_stdin = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        except Exception:
            raw_stdin = ""

        if len(raw_stdin) > _MAX_STDIN_BYTES:
            # Oversized — silently drop and exit 0; hooks never fail.
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

        # Always coerce event_type — this hook only ever emits session_end.
        data["event_type"] = "session_end"
        if "timestamp" not in data or not data["timestamp"]:
            data["timestamp"] = datetime.now(UTC).isoformat()
        if "session_id" not in data or not data["session_id"]:
            data["session_id"] = (
                os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
            )
        if "cwd" not in data or not data["cwd"]:
            data["cwd"] = os.environ.get("PWD") or os.getcwd()

        spool_dir = _default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = _safe_timestamp(str(data.get("timestamp")))
        # Salt the hash with monotonic-nanosecond clock + PID so two
        # byte-identical payloads in the same second can't collide on
        # filename. The salt does NOT appear in the written body.
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_session_end_{hash_hex}.json"
        (spool_dir / file_name).write_text(
            json.dumps(data), encoding="utf-8"
        )
    except Exception:
        # Hooks must never fail.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
