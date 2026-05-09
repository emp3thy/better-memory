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
    """Sanity checks on the hook registry — pin the three expected entries."""

    def test_registry_has_three_entries(self) -> None:
        assert len(_OUR_HOOKS) == 3

    def test_session_bootstrap_is_session_start_event_no_matcher(self) -> None:
        sb = next(s for s in _OUR_HOOKS if s.module.endswith("session_bootstrap"))
        assert sb.event == "SessionStart"
        assert sb.matcher is None
        assert sb.is_async is False

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
    def test_empty_hooks_adds_all_three(self) -> None:
        out = merge_settings_json({}, venv_pyw="/venv/bin/pythonw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1
        ss_hooks = ss[0]["hooks"]
        cmds = [h["command"] for h in ss_hooks]
        assert any("better_memory.hooks.session_bootstrap" in c for c in cmds)

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

    def test_session_start_group_contains_session_bootstrap(self) -> None:
        out = merge_settings_json({}, venv_pyw="/p/pyw")
        ss = out["hooks"]["SessionStart"]
        assert len(ss) == 1
        assert len(ss[0]["hooks"]) == 1
        modules = [h["command"] for h in ss[0]["hooks"]]
        assert any("session_bootstrap" in m for m in modules)

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
        # User's group preserved, our bootstrap appended in its own group.
        assert any(
            g["hooks"][0]["command"] == "echo user-on-start"
            for g in ss
        )
        assert any(
            any("better_memory.hooks.session_bootstrap" in h["command"] for h in g["hooks"])
            for g in ss
        )

    def test_idempotent_second_run_is_noop(self) -> None:
        first = merge_settings_json({}, venv_pyw="/p/pyw")
        second = merge_settings_json(first, venv_pyw="/p/pyw")
        assert first == second

    def test_session_bootstrap_uses_venv_py_async_hooks_use_venv_pyw(self) -> None:
        """Foreground bootstrap needs python.exe (stdout reaches Claude
        Code); the two async hooks keep pythonw.exe so they don't flash
        a console window on every tool call. setup.sh passes the same
        path on non-Windows; the split only matters on Windows."""
        out = merge_settings_json(
            {}, venv_py="/venv/python.exe", venv_pyw="/venv/pythonw.exe",
        )
        ss_cmd = out["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "python.exe" in ss_cmd and "pythonw.exe" not in ss_cmd
        assert "session_bootstrap" in ss_cmd

        ptu_cmd = out["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "pythonw.exe" in ptu_cmd

        stop_cmd = out["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "pythonw.exe" in stop_cmd

    def test_default_venv_py_falls_back_to_venv_pyw(self) -> None:
        """Back-compat: callers that only pass venv_pyw get the old
        behavior (every hook uses the same interpreter)."""
        out = merge_settings_json({}, venv_pyw="/legacy/path/pyw")
        ss_cmd = out["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "/legacy/path/pyw" in ss_cmd


def test_merge_settings_strips_legacy_session_start_and_session_retrieve():
    """Re-running install_hooks after upgrade scrubs the two old hook entries."""
    from better_memory.cli.install_hooks import merge_settings_json

    legacy_pyw = "C:/old/pythonw.exe"
    existing = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command",
                         "command": f'"{legacy_pyw}" -m better_memory.hooks.session_start'},
                        {"type": "command",
                         "command": f'"{legacy_pyw}" -m better_memory.hooks.session_retrieve'},
                    ],
                },
            ],
        },
    }
    new_pyw = "C:/new/pythonw.exe"

    result = merge_settings_json(existing, venv_pyw=new_pyw)

    session_start_groups = result["hooks"]["SessionStart"]
    flattened = [
        h["command"]
        for g in session_start_groups
        for h in g["hooks"]
    ]
    assert all("session_start" not in c or "session_bootstrap" in c for c in flattened)
    assert all("session_retrieve" not in c for c in flattened)
    assert any("session_bootstrap" in c for c in flattened)


def test_merge_settings_writes_single_session_bootstrap_entry_on_empty():
    from better_memory.cli.install_hooks import merge_settings_json

    result = merge_settings_json({}, venv_pyw="/tmp/pythonw")

    groups = result["hooks"]["SessionStart"]
    assert len(groups) == 1
    assert len(groups[0]["hooks"]) == 1
    assert "session_bootstrap" in groups[0]["hooks"][0]["command"]


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


from better_memory.cli.install_hooks import _atomic_write


class TestAtomicWrite:
    def test_creates_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new.json"
        _atomic_write(target, '{"x": 1}')
        assert target.read_text(encoding="utf-8") == '{"x": 1}'

    def test_replaces_existing_content(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.json"
        target.write_text("OLD", encoding="utf-8")
        _atomic_write(target, "NEW")
        assert target.read_text(encoding="utf-8") == "NEW"

    def test_tmp_remains_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "x.json"
        target.write_text("ORIGINAL", encoding="utf-8")

        def boom(src: object, dst: object) -> None:  # noqa: ARG001
            raise PermissionError("simulated")

        monkeypatch.setattr("better_memory.cli.install_hooks.os.replace", boom)
        with pytest.raises(PermissionError, match="simulated"):
            _atomic_write(target, "NEW")
        # Original is unchanged.
        assert target.read_text(encoding="utf-8") == "ORIGINAL"
        # The .tmp sibling persists for forensics.
        tmp = target.with_suffix(target.suffix + ".tmp")
        assert tmp.exists()
        assert tmp.read_text(encoding="utf-8") == "NEW"


from datetime import datetime as _datetime, timezone

from better_memory.cli.install_hooks import _backup


class TestBackup:
    def test_missing_source_returns_none(self, tmp_path: Path) -> None:
        result = _backup(
            tmp_path / "missing.json",
            tmp_path / "backups",
        )
        assert result is None
        assert not (tmp_path / "backups").exists()

    def test_existing_source_copied_with_timestamp(self, tmp_path: Path) -> None:
        src = tmp_path / "ok.json"
        src.write_text("CONTENT", encoding="utf-8")
        dst_dir = tmp_path / "backups"

        fixed = _datetime(2026, 5, 6, 19, 30, 7, tzinfo=timezone.utc)
        result = _backup(src, dst_dir, clock=lambda: fixed)
        assert result is not None
        assert result.name == "ok.json.20260506-193007.bak"
        assert result.read_text(encoding="utf-8") == "CONTENT"
        # Source unchanged.
        assert src.read_text(encoding="utf-8") == "CONTENT"


import json as _json
import os as _os
import subprocess
import sys


@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.claude.json + ~/.claude/settings.json under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / ".better-memory"))
    (tmp_path / ".better-memory" / "install-backups").mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _run_cli(home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **_os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "BETTER_MEMORY_HOME": str(home / ".better-memory"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m", "better_memory.cli.install_hooks",
            "--venv-py",  "/p/python",
            "--venv-pyw", "/p/pythonw",
            "--home",     str(home / ".better-memory"),
        ],
        text=True, capture_output=True, env=env, timeout=30,
    )


class TestCLIIntegration:
    def test_fresh_install_writes_both_files(self, mock_home: Path) -> None:
        result = _run_cli(mock_home)
        assert result.returncode == 0, result.stderr

        claude_json = mock_home / ".claude.json"
        settings_json = mock_home / ".claude" / "settings.json"
        assert claude_json.exists()
        assert settings_json.exists()

        cj = _json.loads(claude_json.read_text(encoding="utf-8"))
        assert cj["mcpServers"]["better-memory"]["command"] == "/p/python"

        sj = _json.loads(settings_json.read_text(encoding="utf-8"))
        assert "SessionStart" in sj["hooks"]
        assert "PostToolUse" in sj["hooks"]
        assert "Stop" in sj["hooks"]

    def test_idempotent_rerun_is_clean(self, mock_home: Path) -> None:
        r1 = _run_cli(mock_home)
        assert r1.returncode == 0
        first_settings = (mock_home / ".claude" / "settings.json").read_text(encoding="utf-8")

        r2 = _run_cli(mock_home)
        assert r2.returncode == 0
        second_settings = (mock_home / ".claude" / "settings.json").read_text(encoding="utf-8")

        # Bytewise stable — JSON formatting + content unchanged on rerun.
        assert first_settings == second_settings

    def test_malformed_settings_refuses_without_writing(self, mock_home: Path) -> None:
        settings = mock_home / ".claude" / "settings.json"
        original = '{"hooks": {invalid'
        settings.write_text(original, encoding="utf-8")

        result = _run_cli(mock_home)
        assert result.returncode == 1
        # File is untouched.
        assert settings.read_text(encoding="utf-8") == original
        # stderr surfaces the path + a fix-and-re-run hint.
        assert str(settings) in result.stderr
        assert "Fix the file then re-run" in result.stderr

    def test_malformed_settings_leaves_claude_json_untouched(
        self, mock_home: Path
    ) -> None:
        """Pre-validation: if settings.json is malformed, claude.json must
        not be modified either. Otherwise we end up in a half-applied state
        where the MCP server is registered but hooks aren't installed."""
        settings = mock_home / ".claude" / "settings.json"
        settings.write_text('{"hooks": {invalid', encoding="utf-8")
        claude_json = mock_home / ".claude.json"
        # Pre-condition: claude.json doesn't exist yet (will be created on a
        # successful install). After a refused install it must STILL not exist.
        assert not claude_json.exists()

        result = _run_cli(mock_home)
        assert result.returncode == 1

        # Settings still untouched.
        assert settings.read_text(encoding="utf-8") == '{"hooks": {invalid'
        # Critical: claude.json was NOT written.
        assert not claude_json.exists()

    def test_summary_lines_printed_on_success(self, mock_home: Path) -> None:
        result = _run_cli(mock_home)
        assert result.returncode == 0
        assert "Installing better-memory" in result.stdout
        assert "MCP server" in result.stdout
        assert "hooks" in result.stdout
        assert "Restart Claude Code" in result.stdout
