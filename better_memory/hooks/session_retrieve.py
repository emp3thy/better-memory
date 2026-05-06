"""Session-start hook: inject persisted reflections as additionalContext.

Companion to ``session_start.py`` (which writes a spool marker for episode
lazy-open). This module surfaces prior reflections at the start of every
Claude Code session so Claude does not have to remember to call
``memory_retrieve`` on first turn.

Reads stdin payload (Claude Code SessionStart event JSON, currently unused —
reserved for future source-aware behaviour), opens memory.db, calls
ReflectionSynthesisService.retrieve_reflections, renders the three buckets
to Markdown, prints a hookSpecificOutput JSON envelope to stdout, and exits
0. Never raises; on any error, logs to hook_errors and injects a fallback
directive so Claude still gets a signal to retrieve manually.
"""

from __future__ import annotations

import json
import sys
from contextlib import closing

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.hooks._error_log import record_hook_error
from better_memory.services.reflection import ReflectionSynthesisService

# Per-bucket cap and per-hint truncation. See spec §Decisions log.
_LIMIT_PER_BUCKET = 10
_HINT_MAX_CHARS = 600

_FOOTER = (
    "Use mcp__better-memory__memory_record_use(id, success|failure) when a "
    "memory materially helps or misleads. Use mcp__better-memory__memory_observe "
    "to write new ones."
)

_EMPTY_PROJECT_MESSAGE = (
    "better-memory: no reflections recorded yet for this project. Use "
    "mcp__better-memory__memory_observe to record observations as you work; "
    "reflections will be distilled from them on episode close."
)


def _truncate_hint(hint: str) -> str:
    if len(hint) <= _HINT_MAX_CHARS:
        return hint
    return hint[: _HINT_MAX_CHARS - 1] + "…"


def _render_bucket(name: str, items: list[dict]) -> str:
    lines = [f"### {name}"]
    for item in items:
        lines.append(f"**{item['title']}**")
        lines.append(f"_{item['use_cases']}_")
        for hint in item.get("hints", []):
            lines.append(f"- {_truncate_hint(hint)}")
        lines.append(f"_id: {item['id']}_")
        lines.append("")
    return "\n".join(lines)


def _render(buckets: dict[str, list[dict]]) -> str:
    if not any(buckets[k] for k in ("do", "dont", "neutral")):
        return _EMPTY_PROJECT_MESSAGE

    sections = ["## Persisted reflections for this project (better-memory)"]
    if buckets["do"]:
        sections.append(_render_bucket("do (prior wins)", buckets["do"]))
    if buckets["dont"]:
        sections.append(_render_bucket("dont (approaches to avoid)", buckets["dont"]))
    if buckets["neutral"]:
        sections.append(_render_bucket("neutral (context)", buckets["neutral"]))
    sections.append(_FOOTER)
    return "\n\n".join(sections)


def _short_msg(exc: BaseException, *, limit: int = 200) -> str:
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return msg[:limit]


def _fallback_directive(exc: BaseException) -> str:
    return (
        f"better-memory: memory injection failed "
        f"({type(exc).__name__}: {_short_msg(exc)}). "
        f"Call mcp__better-memory__memory_retrieve manually before any task in this session."
    )


def _record_failure(exc: BaseException) -> None:
    try:
        record_hook_error(hook_name="session_retrieve", exc=exc)
    except BaseException:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(
            f"[better-memory] session_retrieve: {type(exc).__name__}: {_short_msg(exc)}\n"
        )
    except BaseException:  # noqa: BLE001
        pass


def _print_hook_output(text: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(payload), flush=True)


def main() -> None:
    # Drain stdin defensively — Claude Code may pipe a session payload, but we
    # currently don't act on it. Reading prevents PIPE-related write blocking.
    try:
        sys.stdin.read()
    except BaseException:  # noqa: BLE001
        pass

    rendered: str
    try:
        cfg = get_config()
        proj = project_name()
        with closing(connect(cfg.memory_db)) as conn:
            service = ReflectionSynthesisService(conn)
            buckets = service.retrieve_reflections(
                project=proj, limit_per_bucket=_LIMIT_PER_BUCKET,
            )
        rendered = _render(buckets)
    except BaseException as exc:  # noqa: BLE001
        _record_failure(exc)
        rendered = _fallback_directive(exc)

    try:
        _print_hook_output(rendered)
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
