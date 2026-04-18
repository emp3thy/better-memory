"""PostToolUse observer hook.

Reads the tool-use JSON payload from stdin, writes a single JSON file to the
spool directory, and exits 0. No DB access, no network access, no logging,
no retries. Hooks must never fail — any exception is swallowed.

File naming: ``{iso_ts_safe}_{tool}_{hash}.json`` so files sort chronologically
and identical payloads at the same instant don't collide.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def _default_spool_dir() -> Path:
    """Return the configured spool dir, honouring ``BETTER_MEMORY_SPOOL_DIR``.

    Kept separate from :func:`better_memory.config.get_config` so hooks do not
    import SQLite / sqlite-vec / anything heavyweight at invocation time.
    """
    override = os.environ.get("BETTER_MEMORY_SPOOL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".better-memory" / "spool"


def _safe_timestamp(raw: str | None) -> str:
    """Return a filesystem-safe timestamp component.

    Replaces ``:`` (illegal on NTFS) with ``-``. Falls back to current UTC
    time if ``raw`` is missing or empty.
    """
    if not raw:
        raw = datetime.now(UTC).isoformat()
    return raw.replace(":", "-")


def _safe_tool(raw: object) -> str:
    """Return a filesystem-safe tool component."""
    if not raw or not isinstance(raw, str):
        return "unknown"
    # Strip path separators so a hostile tool name can't escape the spool dir.
    scrubbed = raw.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
    return scrubbed or "unknown"


def main() -> None:
    try:
        payload = sys.stdin.read()
        # ``json.loads`` raises on empty input, which cascades into the outer
        # ``except Exception`` and exits 0 without writing a file.
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")

        # Minimal synthesis: default event_type to ``tool_use`` if missing.
        data.setdefault("event_type", "tool_use")
        if "timestamp" not in data or not data["timestamp"]:
            data["timestamp"] = datetime.now(UTC).isoformat()

        spool_dir = _default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = _safe_timestamp(data.get("timestamp"))
        tool_component = _safe_tool(data.get("tool"))
        # SHA-256 prefix of the serialised payload — cheap collision avoidance
        # for two events that happen in the same second on the same tool.
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        hash_hex = hashlib.sha256(serialised).hexdigest()[:12]

        file_name = f"{ts_component}_{tool_component}_{hash_hex}.json"
        (spool_dir / file_name).write_text(
            json.dumps(data), encoding="utf-8"
        )
    except Exception:
        # Hooks MUST NOT fail. Swallow everything; a silent miss is far
        # preferable to failing a tool invocation inside Claude Code.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
