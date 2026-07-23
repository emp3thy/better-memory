"""Detect parameter-enumeration drift in the user's CLAUDE.md.

Pure functions; the session_bootstrap hook wires them in best-effort.
Only lines that mention a better-memory tool name are scanned. Two token
shapes are checked against the live schema, so prose can't false-positive
on unrelated words:

- `word=` / `word:` — explicit parameter-usage syntax.
- `` `word` `` — a backtick-quoted snake_case identifier, the shape real
  CLAUDE.md prose uses when enumerating params conversationally (e.g.
  "optional `component` ... and `scope_path`").
"""

from __future__ import annotations

import re

_PARAM_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\s*[=:]")
_BACKTICK_RE = re.compile(r"`([a-z_]{4,})`")
_IGNORE = {"http", "https", "note", "example", "warning", "default"}


def build_schemas() -> dict[str, set[str]]:
    from better_memory.mcp.tools import tool_definitions
    out: dict[str, set[str]] = {}
    for tool in tool_definitions():
        rendered = tool.name.replace(".", "_")
        props = set((tool.inputSchema or {}).get("properties", {}).keys())
        out[rendered] = props
    return out


def check_claude_md(text: str, schemas: dict[str, set[str]]) -> list[str]:
    warnings: list[str] = []
    try:
        for line in (text or "").splitlines():
            hit_tools = [name for name in schemas if name in line]
            if not hit_tools:
                continue
            valid: set[str] = set()
            for t in hit_tools:
                valid |= schemas[t]
            candidates = _PARAM_RE.findall(line) + _BACKTICK_RE.findall(line)
            seen: set[str] = set()
            for token in candidates:
                if token in seen:
                    continue
                seen.add(token)
                if token in _IGNORE or token in valid:
                    continue
                if token in schemas:      # a tool name itself, not a param
                    continue
                warnings.append(
                    f"CLAUDE.md documents parameter '{token}' near "
                    f"{'/'.join(hit_tools)} but the live tool schema has no "
                    "such parameter - fix or drop the enumeration.")
    except Exception:
        return []
    return warnings[:1]     # at most one line of noise per session
