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


from better_memory.cli.install_hooks import merge_settings_json


class TestMergeSettingsJson:
    def test_empty_hooks_adds_all_four(self) -> None:
        out = merge_settings_json({}, venv_pyw="/venv/bin/pythonw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1  # one shared matcher-group
        ss_hooks = ss[0]["hooks"]
        cmds = [h["command"] for h in ss_hooks]
        assert any("better_memory.hooks.session_start" in c for c in cmds)
        assert any("better_memory.hooks.session_retrieve" in c for c in cmds)

        ptu = out["hooks"]["PostToolUse"]
        assert len(ptu) == 1
        assert ptu[0]["matcher"] == "Write|Edit|Bash"
        assert "better_memory.hooks.observer" in ptu[0]["hooks"][0]["command"]
        assert ptu[0]["hooks"][0].get("async") is True

        stop = out["hooks"]["Stop"]
        assert len(stop) == 1
        assert "matcher" not in stop[0]
        assert "better_memory.hooks.session_close" in stop[0]["hooks"][0]["command"]
        assert stop[0]["hooks"][0].get("async") is True

    def test_existing_user_postooluse_preserved(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo hello"},
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pythonw")
        ptu = out["hooks"]["PostToolUse"]
        # User's group preserved.
        assert any(
            g.get("matcher") == "Bash"
            and g["hooks"][0]["command"] == "echo hello"
            for g in ptu
        )
        # Our group also present.
        assert any(
            g.get("matcher") == "Write|Edit|Bash"
            and "better_memory.hooks.observer" in g["hooks"][0]["command"]
            for g in ptu
        )

    def test_stale_better_memory_paths_refreshed(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/path/pythonw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/new/path/pythonw")
        observer_cmds = [
            h["command"]
            for g in out["hooks"]["PostToolUse"]
            for h in g["hooks"]
            if "better_memory.hooks.observer" in h["command"]
        ]
        # Exactly one observer entry; refreshed to new path.
        assert len(observer_cmds) == 1
        assert "/new/path/pythonw" in observer_cmds[0]
        assert "/old/path/pythonw" not in observer_cmds[0]

    def test_mixed_matcher_group_user_preserved(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {"type": "command", "command": "echo user-hook"},
                            {
                                "type": "command",
                                "command": "/p/pyw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        # The user's hook stays in some matcher-group; ours moves to a fresh
        # canonical group at the end. The original mixed-group has only the
        # user's hook left.
        all_groups = out["hooks"]["PostToolUse"]
        user_groups = [
            g for g in all_groups
            if any(h["command"] == "echo user-hook" for h in g["hooks"])
        ]
        assert len(user_groups) == 1
        # In the user-preserved group, our observer hook should be gone.
        assert all(
            "better_memory.hooks.observer" not in h["command"]
            for h in user_groups[0]["hooks"]
        )
        # And there must be a separate group with our observer.
        observer_groups = [
            g for g in all_groups
            if any("better_memory.hooks.observer" in h["command"] for h in g["hooks"])
        ]
        assert len(observer_groups) == 1

    def test_empty_matcher_groups_pruned(self) -> None:
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/p/pyw -m better_memory.hooks.observer",
                                "async": True,
                            },
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        # Old group had ONLY our hook; after REMOVE pass it would be empty
        # and is dropped. ADD pass adds a fresh canonical group.
        assert len(out["hooks"]["PostToolUse"]) == 1

    def test_session_start_pair_shares_matcher_group(self) -> None:
        out = merge_settings_json({}, venv_pyw="/p/pyw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1
        assert len(ss[0]["hooks"]) == 2
        modules = [h["command"] for h in ss[0]["hooks"]]
        assert any("session_start" in m and "session_retrieve" not in m for m in modules)
        assert any("session_retrieve" in m for m in modules)

    def test_user_session_start_hook_preserved(self) -> None:
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "echo user-on-start"},
                        ],
                    },
                ],
            },
        }
        out = merge_settings_json(existing, venv_pyw="/p/pyw")
        ss = out["hooks"]["SessionStart"]
        # User's group preserved, our pair appended in its own group.
        assert any(
            g["hooks"][0]["command"] == "echo user-on-start"
            for g in ss
        )
        assert any(
            any("better_memory.hooks.session_start" in h["command"] for h in g["hooks"])
            for g in ss
        )

    def test_idempotent_second_run_is_noop(self) -> None:
        first = merge_settings_json({}, venv_pyw="/p/pyw")
        second = merge_settings_json(first, venv_pyw="/p/pyw")
        assert first == second


from pathlib import Path

from better_memory.cli.install_hooks import _load_or_empty


class TestLoadOrEmpty:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        out = _load_or_empty(tmp_path / "missing.json")
        assert out == {}

    def test_valid_json_returns_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        p.write_text('{"a": 1, "b": [2, 3]}', encoding="utf-8")
        assert _load_or_empty(p) == {"a": 1, "b": [2, 3]}

    def test_malformed_json_refuses_with_line_number(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"a": 1,\nnotjson}', encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            _load_or_empty(p)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        # Path + line# in stderr.
        assert str(p) in captured.err
        assert ":2:" in captured.err or ":2 " in captured.err
        assert "Fix the file then re-run" in captured.err
