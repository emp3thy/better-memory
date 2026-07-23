"""Detect parameter-enumeration drift in the user's CLAUDE.md.

Pure functions; the session_bootstrap hook wires them in best-effort.
Only lines that mention a better-memory tool name are scanned. Two token
shapes are checked against the live schema, so prose can't false-positive
on unrelated words:

- `word=` / `word:` — explicit parameter-usage syntax.
- `` `word` `` — a backtick-quoted snake_case identifier, the shape real
  CLAUDE.md prose uses when enumerating params conversationally (e.g.
  "optional `component` ... and `scope_path`"). To avoid flagging plain
  identifier mentions (e.g. "called from `session_bootstrap` module") this
  branch only fires on lines that also carry a parameter-signal word (see
  `_SIGNAL_WORDS`).

The accepted-token set per tool is schema-derived, not just property
names: enum *values* (e.g. `memory_retrieve`'s `polarity` enum
`do`/`dont`/`neutral`) are valid tokens too — those are documented return
shapes, not phantom parameters, and real CLAUDE.md prose legitimately
backtick-quotes them right next to the tool name.
"""

from __future__ import annotations

import re

_PARAM_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\s*[=:]")
_BACKTICK_RE = re.compile(r"`([a-z_]{4,})`")
_IGNORE = {"http", "https", "note", "example", "warning", "default", "docs"}

# Words/phrases whose presence on a line signals "this backtick token is
# being documented as a parameter", as opposed to a plain identifier or
# module mention. Checked case-insensitively as substrings.
_SIGNAL_WORDS = (
    "optional", "parameter", "param", "pass", "passing", "tune",
    "filter", "argument", "arg", "field", "defaults", "set ",
)


def build_schemas() -> dict[str, set[str]]:
    from better_memory.mcp.tools import tool_definitions
    out: dict[str, set[str]] = {}
    for tool in tool_definitions():
        rendered = tool.name.replace(".", "_")
        properties = (tool.inputSchema or {}).get("properties", {}) or {}
        tokens: set[str] = set()
        for prop_name, spec in properties.items():
            tokens.add(prop_name)
            if isinstance(spec, dict):
                enum_values = spec.get("enum")
                if isinstance(enum_values, list):
                    tokens.update(v for v in enum_values if isinstance(v, str))
        out[rendered] = tokens
    return out


def _has_signal_word(line: str) -> bool:
    lowered = line.lower()
    return any(word in lowered for word in _SIGNAL_WORDS)


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
            candidates = list(_PARAM_RE.findall(line))
            if _has_signal_word(line):
                candidates += _BACKTICK_RE.findall(line)
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
