"""Subprocess tests for better_memory.hooks.session_bootstrap."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _run_hook(home_dir: Path, *, stdin: str = "", cwd: Path | None = None):
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_dir)}
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_bootstrap"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(cwd) if cwd else None,
    )


@pytest.fixture
def home_with_schema(tmp_path: Path) -> Path:
    home = tmp_path / "bm-home"
    home.mkdir()
    c = connect(home / "memory.db")
    try:
        apply_migrations(c, migrations_dir=_MIGRATIONS)
    finally:
        c.close()
    return home


@pytest.fixture
def git_cwd(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


def test_hook_emits_additional_context_envelope(home_with_schema, git_cwd):
    payload = json.dumps({"source": "startup", "session_id": "h-1"})

    proc = _run_hook(home_with_schema, stdin=payload, cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "## better-memory: session bootstrap" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_handles_empty_stdin_with_defaults(home_with_schema, git_cwd):
    proc = _run_hook(home_with_schema, stdin="", cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "## better-memory: session bootstrap" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_handles_malformed_json(home_with_schema, git_cwd):
    proc = _run_hook(home_with_schema, stdin="not json {{", cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    # falls back to source=startup defaults
    assert "Source: startup" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_falls_back_on_db_failure(tmp_path, git_cwd, monkeypatch):
    # Point at a directory instead of a DB file → connect / migrations should fail.
    bad_home = tmp_path / "bad-home"
    bad_home.mkdir()
    (bad_home / "memory.db").mkdir()  # directory in place of file
    payload = json.dumps({"source": "startup", "session_id": "x"})

    proc = _run_hook(bad_home, stdin=payload, cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" in text
    assert "memory_session_bootstrap" in text
