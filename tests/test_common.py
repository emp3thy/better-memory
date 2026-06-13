"""Tests for the shared lightweight helpers in better_memory._common."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC

from better_memory import _common


class TestEnvSessionId:
    def test_claude_session_id_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "primary")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "secondary")
        assert _common.env_session_id() == "primary"

    def test_falls_back_to_claude_code_session_id(self, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "secondary")
        assert _common.env_session_id() == "secondary"

    def test_none_when_no_env(self, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert _common.env_session_id() is None


class TestGetSessionId:
    def test_uses_env_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session")
        assert _common.get_session_id() == "env-session"

    def test_generates_uuid_hex_when_no_env(self, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        sid = _common.get_session_id()
        assert len(sid) == 32
        int(sid, 16)  # raises if not hex


class TestDefaultClock:
    def test_returns_utc_aware_now(self) -> None:
        now = _common.default_clock()
        assert now.tzinfo is UTC


class TestResolveHome:
    def test_env_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
        assert _common.resolve_home() == tmp_path

    def test_default_is_dot_better_memory(self, monkeypatch) -> None:
        monkeypatch.delenv("BETTER_MEMORY_HOME", raising=False)
        home = _common.resolve_home()
        assert home.name == ".better-memory"


class TestDefaultSpoolDir:
    def test_under_home(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
        assert _common.default_spool_dir() == tmp_path / "spool"


class TestSafeTimestamp:
    def test_replaces_colons(self) -> None:
        assert (
            _common.safe_timestamp("2026-06-11T12:34:56+00:00")
            == "2026-06-11T12-34-56+00-00"
        )

    def test_falls_back_to_now_when_empty(self) -> None:
        out = _common.safe_timestamp(None)
        assert ":" not in out
        assert out.startswith("20")


def test_import_is_lightweight() -> None:
    """Hooks import _common at startup — it must not drag in sqlite3,
    boto3, or better_memory.config."""
    code = (
        "import sys; import better_memory._common; "
        "banned = {'sqlite3', 'boto3', 'better_memory.config'}; "
        "loaded = banned & set(sys.modules); "
        "sys.exit(1 if loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
