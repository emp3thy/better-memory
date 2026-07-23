"""CLAUDE.md drift sentinel: prose that enumerates tool params rots.

The 2026-07 incident: the user-level CLAUDE.md documented component/
scope_path/window on memory_retrieve for weeks after they ceased to exist,
training every session to make silently-degraded calls. The rewrite removes
enumerations; the sentinel catches regressions.
"""
from __future__ import annotations

from better_memory.hooks._claude_md_sentinel import build_schemas, check_claude_md


def test_phantom_param_detected():
    schemas = {"memory_retrieve": {"query", "project", "tech"}}
    text = "call memory_retrieve with query and scope_path=src/"
    warnings = check_claude_md(text, schemas)
    assert warnings and "scope_path" in warnings[0]


def test_valid_params_silent():
    schemas = {"memory_retrieve": {"query", "project", "tech"}}
    text = "call memory_retrieve with query='task' and project=x"
    assert check_claude_md(text, schemas) == []


def test_lines_without_tool_names_ignored():
    schemas = {"memory_retrieve": {"query"}}
    assert check_claude_md("window=30d is a fine phrase alone", schemas) == []


def test_common_words_not_flagged():
    schemas = {"memory_retrieve": {"query"}}
    # 'e.g.' / 'i.e.' style tokens and words without = or : suffix don't count
    assert check_claude_md("memory_retrieve is documented here", schemas) == []


def test_build_schemas_covers_retrieve():
    schemas = build_schemas()
    assert "query" in schemas["memory_retrieve"]


def test_malformed_input_never_raises():
    assert check_claude_md("", {}) == []


# --- Real 2026-07 incident lines (verbatim), prose-style backtick enumeration ---


def test_prose_style_phantom_param_detected():
    schemas = {"memory_retrieve": {"query", "project", "tech"}}
    text = (
        "- `mcp__better-memory__memory_retrieve` with a broad `query` "
        "describing the project or task area. ... optional `component` "
        "(subsystem/module) and `scope_path` (file or directory)"
    )
    warnings = check_claude_md(text, schemas)
    assert warnings
    assert "component" in warnings[0] or "scope_path" in warnings[0]


def test_prose_style_no_tool_name_not_flagged():
    schemas = {"memory_retrieve": {"query"}}
    text = "Tune `window` (default `30d`)"
    assert check_claude_md(text, schemas) == []


def test_prose_style_valid_param_not_flagged():
    schemas = {"memory_retrieve": {"query"}}
    text = "call `memory_retrieve` with a `query` describing it"
    assert check_claude_md(text, schemas) == []
