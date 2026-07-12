"""Subprocess tests for better_memory.hooks.session_bootstrap.

The agentcore-routing tests at the bottom run in-process (monkeypatched
``connect`` / ``build_backend``) so they need no boto3 stubs.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.hooks import session_bootstrap as hook

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _run_hook(
    home_dir: Path,
    *,
    stdin: str = "",
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
):
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_dir)}
    if extra_env:
        env.update(extra_env)
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


def test_hook_falls_back_on_db_failure(tmp_path, git_cwd):
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


def test_hook_handles_oversized_stdin(home_with_schema, git_cwd):
    # Pipe ~1.5 MiB of garbage. Hook must drop it and proceed with defaults.
    big = "x" * (1_572_864)  # 1.5 MiB
    proc = _run_hook(home_with_schema, stdin=big, cwd=git_cwd)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    # Defaults applied (source=startup); render must include the bootstrap header.
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "## better-memory: session bootstrap" in text
    assert "Source: startup" in text


def test_hook_writes_session_marker_for_mcp_fallback(home_with_schema, git_cwd):
    """SessionStart receives session_id in stdin payload and must write a
    marker file the MCP server can read (Claude Code doesn't propagate
    CLAUDE_SESSION_ID into spawned stdio MCP envs)."""
    from better_memory.runtime.session_marker import (
        encode_project_dir,
        read_session_id,
    )

    payload = json.dumps({
        "source": "startup",
        "session_id": "marker-test-sid",
        "cwd": str(git_cwd),
    })

    proc = _run_hook(home_with_schema, stdin=payload, cwd=git_cwd)
    assert proc.returncode == 0

    # Marker is keyed by the encoded cwd (which the hook passes as
    # project_dir), regardless of the MCP server's runtime CLAUDE_PROJECT_DIR.
    marker = (
        home_with_schema
        / "runtime"
        / "sessions"
        / encode_project_dir(str(git_cwd))
    )
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == "marker-test-sid"
    # Read helper round-trips with the same project_dir key.
    assert (
        read_session_id(home_with_schema, project_dir=str(git_cwd))
        == "marker-test-sid"
    )


def test_hook_writes_marker_keyed_by_claude_project_dir_when_set(
    home_with_schema, git_cwd, tmp_path,
):
    """Symmetry with MCP read path: when CLAUDE_PROJECT_DIR is set in the
    hook env, the marker MUST be keyed by that env value (not the payload's
    cwd) — otherwise the MCP server, which resolves the read path through
    CLAUDE_PROJECT_DIR, will look at a different key and miss the bridge.

    Regression for Claude BugBot finding on PR #53 (commit 8904fbf).
    """
    from better_memory.runtime.session_marker import (
        encode_project_dir,
        read_session_id,
    )

    # Hook payload's cwd is one path; CLAUDE_PROJECT_DIR is intentionally a
    # DIFFERENT path. The fix keys the marker by the env var.
    project_dir_env = str(tmp_path / "as-resolved-by-claude-code")
    payload = json.dumps({
        "source": "startup",
        "session_id": "env-keyed-sid",
        "cwd": str(git_cwd),
    })

    proc = _run_hook(
        home_with_schema,
        stdin=payload,
        cwd=git_cwd,
        extra_env={"CLAUDE_PROJECT_DIR": project_dir_env},
    )
    assert proc.returncode == 0, proc.stderr

    # Marker is keyed by CLAUDE_PROJECT_DIR, not payload cwd.
    env_marker = (
        home_with_schema
        / "runtime"
        / "sessions"
        / encode_project_dir(project_dir_env)
    )
    cwd_marker = (
        home_with_schema
        / "runtime"
        / "sessions"
        / encode_project_dir(str(git_cwd))
    )
    assert env_marker.is_file(), (
        f"marker not at env-keyed path: {env_marker}"
    )
    assert not cwd_marker.is_file(), (
        f"marker incorrectly keyed by payload cwd: {cwd_marker}"
    )
    # MCP-side read: pass project_dir explicitly with the env value (the
    # MCP server's _resolve_project_dir(None) would do this internally).
    assert (
        read_session_id(home_with_schema, project_dir=project_dir_env)
        == "env-keyed-sid"
    )


def test_hook_session_id_resolves_from_env_var(home_with_schema, git_cwd):
    # Ensure env var leg of the session_id resolution chain is exercised.
    env_overrides = {"CLAUDE_SESSION_ID": "env-fixed-id"}
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_with_schema), **env_overrides}

    proc1 = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_bootstrap"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(git_cwd),
    )
    assert proc1.returncode == 0
    out1 = json.loads(proc1.stdout)
    text1 = out1["hookSpecificOutput"]["additionalContext"]
    assert "Episode: opened" in text1

    # Second invocation with the same env var should reuse the episode.
    proc2 = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_bootstrap"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(git_cwd),
    )
    assert proc2.returncode == 0
    out2 = json.loads(proc2.stdout)
    text2 = out2["hookSpecificOutput"]["additionalContext"]
    assert "Episode: reused" in text2


# ---------------------------------------------------------------------------
# Agentcore-mode routing (in-process): the SessionStart hook must route
# through build_backend and never open the local sqlite database when the
# resolved backend is agentcore. Mirrors tests/hooks/test_contextual_inject.py
# ::test_agentcore_mode_does_not_open_sqlite_connection.
# ---------------------------------------------------------------------------


class _FakeConn:
    def close(self) -> None:
        pass


class _FakeRemoteBackend:
    def __init__(self) -> None:
        self.bootstrap_calls: list[dict] = []

    def session_bootstrap(self, **kwargs):
        self.bootstrap_calls.append(kwargs)
        return {
            "additional_context": "remote-bootstrap-context",
            "project": "/testproj",
            "source": kwargs.get("source") or "",
            "episode_id": kwargs.get("session_id"),
            "episode_action": "opened",
            "semantic_count": 0,
            "reflections_counts": {"do": 0, "dont": 0, "neutral": 0},
        }


def _run_inprocess(payload: dict, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        hook.main()
    assert e.value.code == 0
    return json.loads(capsys.readouterr().out)


def test_agentcore_mode_does_not_open_sqlite_connection(
    tmp_path, monkeypatch, capsys
):
    """storage_backend=agentcore must never call connect(); bootstrap goes
    through build_backend(memory_conn=None) and renders the backend dict's
    additional_context. The session marker is still written (the MCP server
    needs the session-id bridge in agentcore mode too)."""
    from better_memory.runtime.session_marker import read_session_id

    home = tmp_path / "bm-home"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    connect_calls: list = []
    monkeypatch.setattr(
        hook, "connect",
        lambda *a, **kw: connect_calls.append(a) or _FakeConn(),
    )

    build_backend_calls: list[dict] = []
    fake_backend = _FakeRemoteBackend()

    def _fake_build_backend(**kwargs):
        build_backend_calls.append(kwargs)
        return fake_backend

    monkeypatch.setattr(hook, "build_backend", _fake_build_backend)

    res = _run_inprocess(
        {"source": "startup", "session_id": "ac-sess-1", "cwd": str(proj)},
        monkeypatch, capsys,
    )

    assert res["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert (
        res["hookSpecificOutput"]["additionalContext"]
        == "remote-bootstrap-context"
    )
    assert connect_calls == []
    assert len(build_backend_calls) == 1
    assert build_backend_calls[0]["memory_conn"] is None
    assert build_backend_calls[0]["session_id"] == "ac-sess-1"
    assert len(fake_backend.bootstrap_calls) == 1
    call = fake_backend.bootstrap_calls[0]
    assert call["session_id"] == "ac-sess-1"
    assert call["source"] == "startup"
    assert call["cwd"] == Path(str(proj))
    # Session-id bridge marker still written in agentcore mode.
    assert read_session_id(home, project_dir=str(proj)) == "ac-sess-1"


def test_agentcore_mode_backend_failure_falls_back_to_directive(
    tmp_path, monkeypatch, capsys
):
    """Graceful degradation: build_backend failing (e.g. agentcore.json
    missing) must not crash the hook and must NOT fall back to sqlite —
    the envelope carries the manual-bootstrap directive instead."""
    home = tmp_path / "bm-home"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    connect_calls: list = []
    monkeypatch.setattr(
        hook, "connect",
        lambda *a, **kw: connect_calls.append(a) or _FakeConn(),
    )

    def _boom(**kwargs):
        raise FileNotFoundError(
            f"{home}/agentcore.json not found. Run `better-memory agentcore "
            f"init` to create the memory resources and persist their IDs."
        )

    monkeypatch.setattr(hook, "build_backend", _boom)

    res = _run_inprocess(
        {"source": "startup", "session_id": "ac-sess-2", "cwd": str(proj)},
        monkeypatch, capsys,
    )

    text = res["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" in text
    assert "FileNotFoundError" in text
    assert "memory_session_bootstrap" in text
    # Misconfigured agentcore must not silently degrade INTO sqlite.
    assert connect_calls == []


def test_sqlite_mode_never_consults_build_backend(
    home_with_schema, git_cwd, monkeypatch, capsys
):
    """Byte-identical sqlite oracle: with the backend resolved to sqlite the
    hook uses the direct SessionBootstrapService path and never touches the
    storage factory."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home_with_schema))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    def _forbidden(**kwargs):
        raise AssertionError("build_backend consulted on the sqlite path")

    # raising=False: passes against pre-fix code that has no such symbol.
    monkeypatch.setattr(hook, "build_backend", _forbidden, raising=False)

    res = _run_inprocess(
        {"source": "startup", "session_id": "sq-sess-1", "cwd": str(git_cwd)},
        monkeypatch, capsys,
    )
    text = res["hookSpecificOutput"]["additionalContext"]
    assert "## better-memory: session bootstrap" in text
