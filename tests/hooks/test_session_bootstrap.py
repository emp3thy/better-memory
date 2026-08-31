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
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.hooks import session_bootstrap as hook

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


@pytest.fixture(autouse=True)
def _isolate_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin HOME/USERPROFILE away from the developer's real profile.

    session_bootstrap now wires in autocheck.maybe_repair (replacing the old
    read-only CLAUDE.md sentinel), which resolves ~/.claude paths via
    engine.default_target_paths() (Path.home()-based) regardless of
    BETTER_MEMORY_HOME. Without this, every subprocess spawned by this file
    would diff — and potentially repair-write — the REAL ~/.claude.json,
    ~/.claude/settings.json, and ~/.claude/CLAUDE.md on the machine running
    the tests. monkeypatch.setenv touches process os.environ, so subprocess
    helpers here (which build env from ``{**os.environ, ...}``) inherit it
    too.
    """
    fake_home = tmp_path / "fake-user-home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))


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


def test_hook_writes_marker_even_when_bootstrap_fails(tmp_path, git_cwd):
    """Fix 6 pin: write_session_id is hoisted ABOVE the bootstrap call, so
    the degraded path (bootstrap raises) still leaves a valid session marker
    — the session id comes from the stdin payload, not from bootstrap, so
    the first-session server can resolve it and the fallback directive's
    manual memory_session_bootstrap remediation works on the first try."""
    from better_memory.runtime.session_marker import read_session_id

    bad_home = tmp_path / "bad-home"
    bad_home.mkdir()
    (bad_home / "memory.db").mkdir()  # directory in place of file → bootstrap fails
    payload = json.dumps({
        "source": "startup",
        "session_id": "degraded-sid",
        "cwd": str(git_cwd),
    })

    proc = _run_hook(
        bad_home,
        stdin=payload,
        cwd=git_cwd,
        # Pin the marker key deterministically (the outer test env may or
        # may not carry CLAUDE_PROJECT_DIR).
        extra_env={"CLAUDE_PROJECT_DIR": str(git_cwd)},
    )

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "session bootstrap failed" in text
    # Marker written despite the failure — hoisted, best-effort contract.
    assert (
        read_session_id(bad_home, project_dir=str(git_cwd)) == "degraded-sid"
    )


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


def test_agentcore_mode_opens_local_conn_for_exposure_ledger(
    tmp_path, monkeypatch, capsys
):
    """storage_backend=agentcore now ALSO opens a real local connection —
    for session-operational state (the exposure ledger), never for memory
    CONTENT. bootstrap goes through build_backend(memory_conn=<real conn>)
    and renders the backend dict's additional_context. The session marker
    is still written (the MCP server needs the session-id bridge in
    agentcore mode too), and the local connection is closed on exit."""
    from better_memory.runtime.session_marker import read_session_id

    home = tmp_path / "bm-home"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    # HOME/USERPROFILE are already isolated by the file-scoped
    # _isolate_real_home autouse fixture, so no real ~/.claude files are
    # touched here — but the isolated home is EMPTY, so autocheck would
    # still find "everything missing" drift and append a repair line,
    # breaking the exact-equality assertion below on the remote backend's
    # passthrough context. Kill-switch autocheck for this routing test —
    # its own behavior is covered by tests/setup/test_autocheck.py and the
    # dedicated autocheck-wiring tests further down this file.
    monkeypatch.setenv("BETTER_MEMORY_WIRING_AUTOCHECK", "off")

    connect_calls: list = []
    real_connect = hook.connect

    def _tracking_connect(*a, **kw):
        connect_calls.append(a)
        return real_connect(*a, **kw)

    monkeypatch.setattr(hook, "connect", _tracking_connect)

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
    # A real local connection IS opened now (for the exposure ledger) —
    # that's the Task 2 contract change from the prior "never opened" rule.
    assert len(connect_calls) == 1
    assert len(build_backend_calls) == 1
    assert build_backend_calls[0]["memory_conn"] is not None
    assert build_backend_calls[0]["session_id"] == "ac-sess-1"
    assert len(fake_backend.bootstrap_calls) == 1
    call = fake_backend.bootstrap_calls[0]
    assert call["session_id"] == "ac-sess-1"
    assert call["source"] == "startup"
    assert call["cwd"] == Path(str(proj))
    # Session-id bridge marker still written in agentcore mode.
    assert read_session_id(home, project_dir=str(proj)) == "ac-sess-1"


def test_agentcore_mode_bootstrap_exposure_lands_in_local_ledger(
    tmp_path, monkeypatch, capsys
):
    """Task 2: the SessionStart hook's agentcore branch opens the local db
    for session-operational state (the exposure ledger) even though memory
    CONTENT stays in AgentCore. Mirrors a deferred-mode bootstrap, which
    exposes only GENERAL-scope semantic ids (see
    SessionBootstrapService.bootstrap's deferred branch) — this proves the
    wiring end-to-end: hook opens conn -> build_backend threads it through
    -> backend.session_bootstrap's exposure write lands in the real db."""
    from better_memory.services import exposure_log

    home = tmp_path / "bm-home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    # The hook itself never calls apply_migrations (the sqlite branch's
    # connect() targets an already-migrated memory.db in real deployments,
    # created at install time) — pre-apply here so the fake backend's
    # exposure_log.record call below has a session_memory_exposure table
    # to write into.
    c = connect(home / "memory.db")
    try:
        apply_migrations(c, migrations_dir=_MIGRATIONS)
    finally:
        c.close()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    # Kill-switch autocheck: the isolated fake HOME (from the file-scoped
    # _isolate_real_home fixture) has no pre-existing ~/.claude wiring, so
    # autocheck would repair it fresh and append a report line, breaking
    # the exact-equality assertion below on the remote backend's passthrough
    # context. Autocheck's own behavior is covered elsewhere.
    monkeypatch.setenv("BETTER_MEMORY_WIRING_AUTOCHECK", "off")

    class _FakeDeferredBackend:
        def __init__(self, conn):
            self._conn = conn

        def session_bootstrap(self, **kwargs):
            exposure_log.record(
                self._conn,
                session_id=kwargs["session_id"],
                items=[("semantic", "sem-general-1", None)],
                source="bootstrap",
                now=datetime.now(UTC).isoformat(),
            )
            self._conn.commit()
            return {
                "additional_context": "remote-deferred-context",
                "project": "/testproj",
                "source": kwargs.get("source") or "",
                "episode_id": kwargs.get("session_id"),
                "episode_action": "opened",
                "semantic_count": 1,
                "reflections_counts": {"do": 0, "dont": 0, "neutral": 0},
            }

    build_backend_calls: list[dict] = []

    def _fake_build_backend(**kwargs):
        build_backend_calls.append(kwargs)
        return _FakeDeferredBackend(kwargs["memory_conn"])

    monkeypatch.setattr(hook, "build_backend", _fake_build_backend)

    res = _run_inprocess(
        {"source": "startup", "session_id": "ac-deferred-sess", "cwd": str(proj)},
        monkeypatch, capsys,
    )

    assert res["hookSpecificOutput"]["additionalContext"] == "remote-deferred-context"
    assert len(build_backend_calls) == 1
    assert build_backend_calls[0]["memory_conn"] is not None

    check_conn = connect(home / "memory.db")
    try:
        row = check_conn.execute(
            "SELECT source FROM session_memory_exposure "
            "WHERE session_id = ? AND memory_id = ?",
            ("ac-deferred-sess", "sem-general-1"),
        ).fetchone()
    finally:
        check_conn.close()
    assert row is not None
    assert row["source"] == "bootstrap"


def test_agentcore_mode_backend_failure_falls_back_to_directive(
    tmp_path, monkeypatch, capsys
):
    """Graceful degradation: build_backend failing (e.g. agentcore.json
    missing) must not crash the hook and must NOT fall back to sqlite —
    the envelope carries the manual-bootstrap directive instead. The
    session marker is STILL written (fix 6: hoisted write_session_id) so
    the manual remediation can resolve the session id."""
    from better_memory.runtime.session_marker import read_session_id

    home = tmp_path / "bm-home"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

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
    # Task 2: connect() IS now called once, to open the local exposure
    # ledger — but that is NOT a degrade into sqlite CONTENT retrieval:
    # build_backend still fails and the manual-bootstrap directive still
    # fires (asserted above), never a silent SessionBootstrapService path.
    assert len(connect_calls) == 1
    # Fix 6: marker written BEFORE the failing backend call.
    assert read_session_id(home, project_dir=str(proj)) == "ac-sess-2"


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


# ---------------------------------------------------------------------------
# Wiring autocheck (replaces the retired CLAUDE.md sentinel): the hook's
# try block does `from better_memory.setup.autocheck import maybe_repair`
# on every call, so monkeypatching the attribute on the autocheck module
# itself is picked up (no need to patch `hook`).
# ---------------------------------------------------------------------------


def test_autocheck_line_appended_to_bootstrap_context(
    home_with_schema, git_cwd, monkeypatch, capsys
):
    """When autocheck.maybe_repair returns a report line, session_bootstrap
    appends it to the rendered additionalContext."""
    from better_memory.setup import autocheck

    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home_with_schema))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(
        autocheck,
        "maybe_repair",
        lambda home, cwd: "better-memory doctor: repaired 1 item(s): "
        "settings.json (effective next session)",
    )

    res = _run_inprocess(
        {"source": "startup", "session_id": "autocheck-sess-1", "cwd": str(git_cwd)},
        monkeypatch, capsys,
    )
    text = res["hookSpecificOutput"]["additionalContext"]
    assert "## better-memory: session bootstrap" in text
    assert "better-memory doctor: repaired 1 item(s)" in text


def test_autocheck_raising_still_produces_normal_output(
    home_with_schema, git_cwd, monkeypatch, capsys
):
    """autocheck is best-effort: if maybe_repair raises, the bootstrap still
    emits its normal additionalContext (the surrounding try/except
    BaseException swallows the failure and drops the autocheck line)."""
    from better_memory.setup import autocheck

    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home_with_schema))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    def _boom(home, cwd):
        raise RuntimeError("autocheck exploded")

    monkeypatch.setattr(autocheck, "maybe_repair", _boom)

    res = _run_inprocess(
        {"source": "startup", "session_id": "autocheck-sess-2", "cwd": str(git_cwd)},
        monkeypatch, capsys,
    )
    text = res["hookSpecificOutput"]["additionalContext"]
    assert "## better-memory: session bootstrap" in text
    assert "doctor" not in text
