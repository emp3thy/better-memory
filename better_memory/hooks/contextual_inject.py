"""UserPromptSubmit / PreToolUse hook: inject curated memories relevant to the
current prompt or tool-input. Gated by BETTER_MEMORY_CONTEXT_INJECT_MODE
(userprompt | pretool | both | off). Never raises; always exits 0.

NOTE: whether PreToolUse fires for the built-in Skill/Task tools is
environment-dependent (see the plan's Task 0 probe); UserPromptSubmit is the
reliable trigger.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.relevant import format_relevant, retrieve_relevant
from better_memory.storage import build_backend

_MAX_STDIN_BYTES = 1_000_000


def _enabled(event: str, mode: str) -> bool:
    if mode == "off":
        return False
    if event == "UserPromptSubmit":
        return mode in ("userprompt", "both")
    if event == "PreToolUse":
        return mode in ("pretool", "both")
    return False


def _query_from(payload: dict, event: str) -> str:
    if event == "UserPromptSubmit":
        return str(payload.get("prompt") or "")
    if event == "PreToolUse":
        tool = payload.get("tool_name") or ""
        return f"{tool} {json.dumps(payload.get('tool_input') or {})}"
    return ""


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except BaseException:  # noqa: BLE001 — hooks never fail
        pass
    payload: dict = {}
    if raw.strip() and len(raw) <= _MAX_STDIN_BYTES:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except BaseException:  # noqa: BLE001
            pass

    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    rendered = ""
    try:
        cfg = get_config()
        if _enabled(event, cfg.context_inject_mode):
            query = _query_from(payload, event)
            cwd = str(payload.get("cwd") or os.getcwd())
            project = project_name(Path(cwd))
            with closing(connect(cfg.memory_db)) as conn:
                backend = build_backend(
                    config=cfg,
                    memory_conn=conn,
                    embedder=None,
                    session_id=None,
                    project=project,
                )
                items = retrieve_relevant(backend, query=query, project=project)
            rendered = format_relevant(items)
    except BaseException as exc:  # noqa: BLE001
        try:
            record_hook_error(hook_name="contextual_inject", exc=exc)
        except BaseException:  # noqa: BLE001
            pass
        rendered = ""

    try:
        print(
            json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": event,
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
