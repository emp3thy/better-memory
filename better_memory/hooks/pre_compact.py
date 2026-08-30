"""PreCompact hook: directive to persist conversation state before compaction."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except BaseException:  # noqa: BLE001 — hooks never fail
        hook_input = {}
    session_id = hook_input.get("session_id", "unknown")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": (
                f"IMPORTANT (session {session_id}): Context is about to be "
                "compacted. Before proceeding, persist current conversation "
                "state to better-memory via mcp__better-memory__memory_observe. "
                "Record one observation per durable fact/decision, setting "
                "outcome (success/failure/neutral) and filling component/theme/"
                "trigger_type where applicable. Include: (1) the task currently "
                "in flight, (2) key decisions made in this session, (3) any open "
                "questions or next steps, (4) file paths and line numbers "
                "relevant to current work. This ensures continuity after "
                "compaction."
            ),
        }
    }), flush=True)


if __name__ == "__main__":
    main()
