"""Tool-call triggers: the grammar, its validator, and the matcher.

A trigger is one string that names a tool-call condition under which a
memory should be served just in time (design spec
2026-10-05-just-in-time-serving-design.md §2). Five forms:

- ``tool:<Name>``   -- the tool name equals ``Name`` (exact, case-sensitive).
- ``skill:<name>``  -- the Skill tool is invoked with that skill; a trailing
  ``*`` matches any skill with that prefix.
- ``bash:<text>``   -- a Bash command contains ``text`` (case-insensitive).
- ``path:<text>``   -- a Write / Edit / Read file path contains ``text``
  (case-insensitive, separators normalised to ``/``).
- ``write:large``   -- a Write whose content exceeds ``large_write_chars``.

Pure functions, no I/O. The matcher never raises on odd input shapes: a
non-string operand is treated as absent, an unknown stored trigger string is
skipped.
"""
from __future__ import annotations

import json
from typing import Any

VALID_PREFIXES: tuple[str, ...] = ("tool:", "skill:", "bash:", "path:", "write:")

_PATH_TOOLS: tuple[str, ...] = ("Write", "Edit", "Read")

_GRAMMAR_HELP = "expected one of tool:, skill:, bash:, path:, write:large"


def validate_triggers(raw: list[str]) -> list[str]:
    """Strip, drop empties, de-duplicate (order kept) and check the grammar.

    Raises ``ValueError`` naming the first offending string.
    """
    out: list[str] = []
    for item in raw or []:
        t = (item or "").strip() if isinstance(item, str) else ""
        if not t:
            continue
        prefix, _, operand = t.partition(":")
        prefix = prefix + ":"
        bad = prefix not in VALID_PREFIXES or not operand
        if prefix == "write:" and operand != "large":
            bad = True
        if bad:
            raise ValueError(f"invalid trigger {t!r}: {_GRAMMAR_HELP}")
        if t not in out:
            out.append(t)
    return out


def parse_triggers(raw: str | None) -> list[str]:
    """Decode the JSON column. Anything that is not a JSON list of strings
    (``None``, empty, malformed, wrong shape) decodes to ``[]``."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        return []
    return list(data)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _fires(trigger: str, tool_name: str, tool_input: dict, large_write_chars: int) -> bool:
    prefix, _, operand = trigger.partition(":")
    if not operand:
        return False
    if prefix == "tool":
        return tool_name == operand
    if prefix == "skill":
        if tool_name != "Skill":
            return False
        skill = _str(tool_input.get("skill"))
        if skill is None:
            return False
        if operand.endswith("*"):
            return skill.startswith(operand[:-1])
        return skill == operand
    if prefix == "bash":
        if tool_name != "Bash":
            return False
        command = _str(tool_input.get("command"))
        return command is not None and operand.lower() in command.lower()
    if prefix == "path":
        if tool_name not in _PATH_TOOLS:
            return False
        path = _str(tool_input.get("file_path"))
        if path is None:
            return False
        return operand.lower().replace("\\", "/") in path.lower().replace("\\", "/")
    if prefix == "write":
        if tool_name != "Write" or operand != "large":
            return False
        content = _str(tool_input.get("content"))
        return content is not None and len(content) > large_write_chars
    return False


def match(
    triggers: list[str],
    tool_name: str,
    tool_input: dict | None,
    *,
    large_write_chars: int,
) -> str | None:
    """Return the first trigger string that fires for this tool call, else None."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    for trigger in triggers or []:
        if not isinstance(trigger, str):
            continue
        try:
            if _fires(trigger, tool_name or "", inp, large_write_chars):
                return trigger
        except Exception:  # noqa: BLE001 - a corrupt trigger must never break serving
            continue
    return None
