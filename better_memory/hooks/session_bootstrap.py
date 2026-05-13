"""SessionStart hook: open/reuse episode + inject memories + reflections.

Reads Claude Code SessionStart payload from stdin (source, session_id, cwd),
calls SessionBootstrapService in-process (no MCP RPC on the hook critical
path), prints a hookSpecificOutput JSON envelope to stdout. Never raises;
on any error logs to hook_errors and injects a fallback directive instructing
Claude to call the MCP tool manually.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from better_memory.config import get_config
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.runtime.session_marker import write_session_id
from better_memory.services.session_bootstrap import SessionBootstrapService

_MAX_STDIN_BYTES = 1_048_576


def _short_msg(exc: BaseException, *, limit: int = 200) -> str:
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return msg[:limit]


def _fallback_directive(exc: BaseException) -> str:
    return (
        f"better-memory: session bootstrap failed "
        f"({type(exc).__name__}: {_short_msg(exc)}). "
        f"Call mcp__better-memory__memory_session_bootstrap manually before any task. "
        f"If the failure persists, check ~/.better-memory/hook_errors and "
        f"consider rolling back via the install-backups directory."
    )


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:  # noqa: BLE001 — hooks never fail
        pass
    if len(raw) > _MAX_STDIN_BYTES:
        raw = ""  # oversized; drop and proceed with defaults

    payload: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:  # noqa: BLE001
            pass

    source_val = payload.get("source")
    source = str(source_val) if source_val else None
    session_id = (
        str(payload.get("session_id"))
        if payload.get("session_id")
        else (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or uuid4().hex
        )
    )
    cwd_str = str(payload.get("cwd")) if payload.get("cwd") else os.getcwd()

    try:
        cfg = get_config()
        with closing(connect(cfg.memory_db)) as conn:
            service = SessionBootstrapService(conn)
            result = service.bootstrap(
                source=source, session_id=session_id, cwd=Path(cwd_str),
            )
        rendered = result.additional_context
        # Bridge session_id to the MCP server: it doesn't see CLAUDE_SESSION_ID
        # in its spawn env. See better_memory/runtime/session_marker.py.
        write_session_id(cfg.home, session_id, project_dir=cwd_str)
    except BaseException as exc:  # noqa: BLE001
        try:
            record_hook_error(hook_name="session_bootstrap", exc=exc)
        except BaseException:  # noqa: BLE001
            pass
        rendered = _fallback_directive(exc)

    try:
        print(
            json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": rendered,
                }
            }),
            flush=True,
        )
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
