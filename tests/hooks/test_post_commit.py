"""Tests for ``better_memory.hooks.post_commit``.

The post-commit hook inspects the latest commit message on HEAD. If the
message contains a ``Closes-Episode: <truthy>`` trailer, the hook writes
a ``commit_close`` marker to the spool directory for SpoolService.drain
to process.

Pattern mirrors tests/hooks/test_session_close.py — subprocess invocation
of the real hook module against a freshly-initialised temp git repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _init_git_repo(repo: Path) -> None:
    """Create a throwaway git repo with a minimal identity and one initial commit."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True,
    )
    # An initial commit so HEAD always resolves.
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed commit"],
        cwd=repo, check=True,
    )


def _commit(repo: Path, message: str) -> None:
    """Make an arbitrary commit with the given message on HEAD."""
    # Touch a new file each time so --allow-empty isn't needed.
    marker = repo / f"file-{len(list(repo.glob('file-*.txt')))}.txt"
    marker.write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", marker.name], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, check=True,
    )


def _run_post_commit(
    repo: Path,
    spool_home: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_HOME": str(spool_home)}
    # Deliberately clear CLAUDE_SESSION_ID before re-applying extra_env
    # so tests don't pick up a leaked value from the parent process.
    env.pop("CLAUDE_SESSION_ID", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.post_commit"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(repo),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)
    return repo_dir


@pytest.fixture
def spool_home(tmp_path: Path) -> Path:
    return tmp_path / "bm"


def _spool_dir(spool_home: Path) -> Path:
    return spool_home / "spool"


def test_no_trailer_writes_no_marker(repo: Path, spool_home: Path) -> None:
    _commit(repo, "regular commit with no trailer")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    spool = _spool_dir(spool_home)
    if spool.exists():
        assert list(spool.glob("*.json")) == []


def test_trailer_true_writes_commit_close_marker(
    repo: Path, spool_home: Path
) -> None:
    _commit(repo, "fix bug\n\nCloses-Episode: true")
    result = _run_post_commit(
        repo,
        spool_home,
        extra_env={"CLAUDE_SESSION_ID": "sess-commit-abc"},
    )
    assert result.returncode == 0, result.stderr
    spool = _spool_dir(spool_home)
    files = list(spool.glob("*.json"))
    assert len(files) == 1
    assert "commit_close" in files[0].name
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["event_type"] == "commit_close"
    assert payload["session_id"] == "sess-commit-abc"
    assert "timestamp" in payload and payload["timestamp"]
    assert payload.get("commit_sha")  # hook captures the committed SHA


def test_trailer_yes_and_1_also_truthy(
    repo: Path, spool_home: Path
) -> None:
    _commit(repo, "ship it\n\nCloses-Episode: YES")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    files = list(_spool_dir(spool_home).glob("*.json"))
    assert len(files) == 1

    # Second commit, also truthy with "1".
    _commit(repo, "ship it harder\n\nCloses-Episode: 1")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    files = list(_spool_dir(spool_home).glob("*.json"))
    assert len(files) == 2


def test_trailer_false_writes_no_marker(
    repo: Path, spool_home: Path
) -> None:
    _commit(repo, "wip\n\nCloses-Episode: false")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    spool = _spool_dir(spool_home)
    if spool.exists():
        assert list(spool.glob("*.json")) == []


def test_trailer_arbitrary_value_writes_no_marker(
    repo: Path, spool_home: Path
) -> None:
    """Only `true` / `yes` / `1` are truthy. Everything else is a no-op."""
    _commit(repo, "wip\n\nCloses-Episode: maybe")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    spool = _spool_dir(spool_home)
    if spool.exists():
        assert list(spool.glob("*.json")) == []


def test_trailer_case_insensitive_key(
    repo: Path, spool_home: Path
) -> None:
    """The trailer key is recognised case-insensitively (`closes-episode`, `CLOSES-EPISODE`, etc.)."""
    _commit(repo, "done\n\ncloses-episode: true")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    files = list(_spool_dir(spool_home).glob("*.json"))
    assert len(files) == 1


def test_hook_swallows_git_failure(tmp_path: Path) -> None:
    """Outside a git repo, `git log` fails — the hook must still exit 0."""
    home = tmp_path / "bm"
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home)}
    env.pop("CLAUDE_SESSION_ID", None)
    result = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.post_commit"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(non_repo),
    )
    assert result.returncode == 0, result.stderr


def test_hook_falls_back_to_uuid_session_id(
    repo: Path, spool_home: Path
) -> None:
    """Without CLAUDE_SESSION_ID, session_id in the marker is a uuid4 hex."""
    _commit(repo, "done\n\nCloses-Episode: true")
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    files = list(_spool_dir(spool_home).glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_id"]
    # uuid4().hex length.
    assert len(payload["session_id"]) == 32


def test_multiple_trailer_lines_last_wins(
    repo: Path, spool_home: Path
) -> None:
    """If the same trailer appears twice, the last occurrence wins (git convention)."""
    _commit(
        repo,
        "done\n\nCloses-Episode: false\nCloses-Episode: true",
    )
    result = _run_post_commit(repo, spool_home)
    assert result.returncode == 0, result.stderr
    # Last value is truthy, so a marker should be written.
    files = list(_spool_dir(spool_home).glob("*.json"))
    assert len(files) == 1
