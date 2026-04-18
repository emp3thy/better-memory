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
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def _default_spool_dir() -> Path:
    """Return the configured spool dir, honouring ``BETTER_MEMORY_SPOOL_DIR``.

    Mirrors the observer hook. Kept duplicated to avoid a cross-module import
    that would slow hook startup.
    """
    override = os.environ.get("BETTER_MEMORY_SPOOL_DIR")
    if override:
        return Path(override)
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
            raw_stdin = sys.stdin.read()
        except Exception:
            raw_stdin = ""

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
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        hash_hex = hashlib.sha256(serialised).hexdigest()[:12]

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
