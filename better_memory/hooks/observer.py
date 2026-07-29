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
import time
from datetime import UTC, datetime

from better_memory._common import default_spool_dir, safe_timestamp

# Cap stdin reads so a malicious or accidentally huge payload can't starve the
# hook process of memory. 1 MiB is far larger than anything Claude Code emits
# in practice but small enough to be trivially bounded.
_MAX_STDIN_BYTES = 1_048_576


def _safe_tool(raw: object) -> str:
    """Return a filesystem-safe tool component."""
    if not raw or not isinstance(raw, str):
        return "unknown"
    # Strip path separators so a hostile tool name can't escape the spool dir.
    scrubbed = raw.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
    return scrubbed or "unknown"


def main() -> None:
    try:
        # Read one byte past the cap so we can detect oversize without holding
        # more than MAX+1 bytes in memory.
        payload = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        if len(payload) > _MAX_STDIN_BYTES:
            # Oversized — silently drop and exit 0; hooks never fail.
            sys.exit(0)
        # ``json.loads`` raises on empty input, which cascades into the outer
        # ``except Exception`` and exits 0 without writing a file.
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")

        # Minimal synthesis: default event_type to ``tool_use`` if missing.
        data.setdefault("event_type", "tool_use")
        if "timestamp" not in data or not data["timestamp"]:
            data["timestamp"] = datetime.now(UTC).isoformat()

        spool_dir = default_spool_dir()
        spool_dir.mkdir(parents=True, exist_ok=True)

        ts_component = safe_timestamp(data.get("timestamp"))
        tool_component = _safe_tool(data.get("tool"))
        # SHA-256 prefix of the serialised payload — cheap collision avoidance
        # for two events that happen in the same second on the same tool. The
        # salt (monotonic-nanosecond clock + PID) guarantees uniqueness even
        # for two invocations with byte-identical payloads, which is otherwise
        # possible when Claude Code replays the same tool call. The salt does
        # NOT appear in the written body — it only perturbs the filename.
        serialised = json.dumps(data, sort_keys=True).encode("utf-8")
        salt = f"{time.time_ns()}:{os.getpid()}".encode()
        hash_hex = hashlib.sha256(serialised + salt).hexdigest()[:12]

        file_name = f"{ts_component}_{tool_component}_{hash_hex}.json"
        # Publish atomically: write to a sibling ``*.json.tmp`` then
        # ``os.replace`` onto the final name. ``SpoolService.drain`` globs
        # ``*.json`` (spool.py:111), so an in-flight tmp file is never
        # picked up mid-write. Without this, a concurrent drain can read
        # a partial file, JSONDecodeError-quarantine it (spool.py:120-125),
        # and lose the event forever.
        final_path = spool_dir / file_name
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_path, final_path)
    except Exception as _exc:
        # Hooks MUST NOT fail. Swallow everything; a silent miss is far
        # better than crashing Claude Code. Best-effort: record the
        # failure to hook_errors for /diagnostics visibility.
        try:
            from better_memory.hooks._error_log import record_hook_error
            record_hook_error(hook_name="observer", exc=_exc)
        except BaseException:  # noqa: BLE001
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
