"""resolve_session_id resolves Claude Code session id with the documented fallback."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from better_memory.mcp._session import resolve_session_id


def test_resolve_session_id_prefers_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    assert resolve_session_id(tmp_path) == "from-env"


def test_resolve_session_id_falls_back_to_alt_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "alt-env")
    assert resolve_session_id(tmp_path) == "alt-env"


def test_resolve_session_id_falls_back_to_marker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with patch("better_memory.mcp._session.read_session_id", return_value="marker"):
        assert resolve_session_id(tmp_path) == "marker"


def test_resolve_session_id_returns_none_when_all_absent(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with patch("better_memory.mcp._session.read_session_id", return_value=None):
        assert resolve_session_id(tmp_path) is None
