"""PreToolUse(Bash) hook: memory-observe reminder before git commit."""
from __future__ import annotations

import json
import sys

_MESSAGE = (
    "MEMORY CHECKPOINT before commit: per CLAUDE.md mandatory triggers, if "
    "this commit fixes a non-obvious bug, addresses reviewer feedback, or "
    "wraps a phase, you MUST call mcp__better-memory__memory_observe BEFORE "
    "running git commit. Skipping is a CLAUDE.md violation."
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except BaseException:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = str(tool_input.get("command", ""))
    if "git commit" not in command:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _MESSAGE,
        }
    }), flush=True)


if __name__ == "__main__":
    main()
