"""Tests for ``better_memory.cli.install_hooks``.

Pure-function tests use no fs / network. I/O tests use ``tmp_path``.
"""

from __future__ import annotations

import pytest

from better_memory.cli.install_hooks import (
    HookSpec,
    _OUR_HOOKS,
    merge_claude_json,
)


class TestMergeClaudeJson:
    def test_empty_config_adds_entry(self) -> None:
        out = merge_claude_json(
            {}, command="/venv/bin/python", home="/home/u/.better-memory"
        )
        bm = out["mcpServers"]["better-memory"]
        assert bm["type"] == "stdio"
        assert bm["command"] == "/venv/bin/python"
        assert bm["args"] == ["-m", "better_memory.mcp"]
        assert bm["env"] == {"BETTER_MEMORY_HOME": "/home/u/.better-memory"}

    def test_existing_user_env_preserved(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "type": "stdio",
                    "command": "/old/python",
                    "args": ["-m", "better_memory.mcp"],
                    "env": {"FOO": "bar"},
                },
            },
        }
        out = merge_claude_json(
            existing, command="/new/python", home="/home/u/.better-memory"
        )
        env = out["mcpServers"]["better-memory"]["env"]
        assert env["FOO"] == "bar"
        assert env["BETTER_MEMORY_HOME"] == "/home/u/.better-memory"

    def test_user_custom_BETTER_MEMORY_HOME_wins(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "env": {"BETTER_MEMORY_HOME": "/custom/path"},
                },
            },
        }
        out = merge_claude_json(
            existing, command="/x/python", home="/default/path"
        )
        assert out["mcpServers"]["better-memory"]["env"]["BETTER_MEMORY_HOME"] == "/custom/path"

    def test_command_path_refreshed_on_rerun(self) -> None:
        existing = {
            "mcpServers": {
                "better-memory": {
                    "type": "stdio",
                    "command": "/old/path/python",
                    "args": ["-m", "better_memory.mcp"],
                    "env": {"BETTER_MEMORY_HOME": "/h"},
                },
            },
        }
        out = merge_claude_json(existing, command="/new/path/python", home="/h")
        assert out["mcpServers"]["better-memory"]["command"] == "/new/path/python"

    def test_other_mcp_servers_untouched(self) -> None:
        existing = {
            "mcpServers": {
                "some-other-server": {"type": "stdio", "command": "/x"},
            },
        }
        out = merge_claude_json(existing, command="/p", home="/h")
        assert out["mcpServers"]["some-other-server"] == {"type": "stdio", "command": "/x"}
        assert "better-memory" in out["mcpServers"]


class TestHookSpec:
    """Sanity checks on the hook registry — pin the four expected entries."""

    def test_registry_has_four_entries(self) -> None:
        assert len(_OUR_HOOKS) == 4

    def test_session_start_pair_is_session_start_event_no_matcher(self) -> None:
        for spec in _OUR_HOOKS:
            if spec.module.endswith(("session_start", "session_retrieve")):
                assert spec.event == "SessionStart"
                assert spec.matcher is None
                assert spec.is_async is False

    def test_observer_is_post_tool_use_with_matcher(self) -> None:
        observer = next(s for s in _OUR_HOOKS if s.module.endswith("observer"))
        assert observer.event == "PostToolUse"
        assert observer.matcher == "Write|Edit|Bash"
        assert observer.is_async is True

    def test_session_close_is_stop_no_matcher_async(self) -> None:
        sc = next(s for s in _OUR_HOOKS if s.module.endswith("session_close"))
        assert sc.event == "Stop"
        assert sc.matcher is None
        assert sc.is_async is True
