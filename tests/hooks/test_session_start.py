"""Tests for ``better_memory.hooks.session_start``.

The session-start hook writes a ``session_start`` marker JSON to the spool.
Pattern mirrors tests/hooks/test_session_close.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_session_start(
    tmp_spool: Path,
    *,
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_HOME": str(tmp_spool.parent)}
    if extra_env:
        env.update(extra_env)
    # Deliberately clear CLAUDE_SESSION_ID if not in extra_env so tests
    # don't pick up a leaked value from the parent process.
    if extra_env is None or "CLAUDE_SESSION_ID" not in extra_env:
        env.pop("CLAUDE_SESSION_ID", None)
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_start"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(cwd) if cwd is not None else None,
    )


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    return tmp_path / "spool"


def test_empty_stdin_writes_marker_with_event_type(tmp_spool: Path) -> None:
    result = _run_session_start(
        tmp_spool, stdin="", extra_env={"CLAUDE_SESSION_ID": "sess-claude-123"}
    )
    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    assert "session_start" in files[0].name
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["event_type"] == "session_start"
    assert payload["session_id"] == "sess-claude-123"
    assert "timestamp" in payload and payload["timestamp"]


def test_stdin_session_id_overrides_env(tmp_spool: Path) -> None:
    """stdin payload beats env var — matches session_close.py precedence."""
    stdin = json.dumps({"session_id": "stdin-sess", "cwd": "/some/path"})
    result = _run_session_start(
        tmp_spool,
        stdin=stdin,
        extra_env={"CLAUDE_SESSION_ID": "env-sess"},
    )
    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_id"] == "stdin-sess"
    assert payload["cwd"] == "/some/path"


def test_generates_session_id_when_missing(tmp_spool: Path) -> None:
    """No stdin, no CLAUDE_SESSION_ID → uuid4 fallback."""
    result = _run_session_start(tmp_spool, stdin="")
    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_id"]
    assert len(payload["session_id"]) == 32  # uuid4().hex length


def test_records_project_from_cwd(tmp_spool: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "my-cool-project"
    project_dir.mkdir()
    result = _run_session_start(tmp_spool, stdin="", cwd=project_dir)
    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["project"] == "my-cool-project"


def test_swallows_exceptions_and_exits_zero(tmp_path: Path) -> None:
    """Even when the spool path is unwritable, the hook exits 0."""
    # Point BETTER_MEMORY_HOME at a file (not a directory). Creating the
    # spool dir under it will raise NotADirectoryError, which the hook
    # must swallow.
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a dir")
    env = {**os.environ, "BETTER_MEMORY_HOME": str(blocker)}
    env.pop("CLAUDE_SESSION_ID", None)
    result = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_start"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
