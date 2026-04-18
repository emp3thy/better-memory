"""Tests for ``better_memory.hooks.session_close``.

The session-close hook writes a ``session_end`` marker JSON to the spool.
Like the observer, it must never raise and must exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_session_close(
    tmp_spool: Path,
    *,
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_SPOOL_DIR": str(tmp_spool)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_close"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    spool.mkdir()
    return spool


def test_session_close_empty_stdin_writes_marker(tmp_spool: Path) -> None:
    result = _run_session_close(tmp_spool, stdin="")
    assert result.returncode == 0, result.stderr
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    assert "session_end" in files[0].name
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["event_type"] == "session_end"
    assert "timestamp" in payload and payload["timestamp"]
    # session_id and cwd should be synthesized too.
    assert "session_id" in payload
    assert "cwd" in payload


def test_session_close_respects_claude_session_id_env(tmp_spool: Path) -> None:
    result = _run_session_close(
        tmp_spool,
        stdin="",
        extra_env={"CLAUDE_SESSION_ID": "the-session-123"},
    )
    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_id"] == "the-session-123"


def test_session_close_accepts_stdin_override(tmp_spool: Path) -> None:
    stdin_payload = json.dumps(
        {
            "event_type": "session_end",
            "cwd": "/custom/cwd",
            "session_id": "stdin-sess",
            "timestamp": "2026-04-18T15:30:00Z",
        }
    )
    result = _run_session_close(tmp_spool, stdin=stdin_payload)
    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["event_type"] == "session_end"
    assert payload["session_id"] == "stdin-sess"
    assert payload["cwd"] == "/custom/cwd"


def test_session_close_invalid_stdin_still_writes_marker(tmp_spool: Path) -> None:
    # Invalid JSON on stdin should NOT raise — the hook falls back to a
    # synthesized session_end marker.
    result = _run_session_close(tmp_spool, stdin="not-json")
    assert result.returncode == 0
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["event_type"] == "session_end"
