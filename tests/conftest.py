"""Shared pytest fixtures for the better-memory test suite."""

from __future__ import annotations

import asyncio
from asyncio import events as _asyncio_events
from collections.abc import Coroutine, Iterator
from pathlib import Path

import pytest


def run_async[T](coro: Coroutine[object, object, T]) -> T:
    """Drive ``coro`` to completion on a fresh event loop in this thread.

    Why this exists: pytest-playwright (used by the browser-based UI
    tests) leaves a running event loop registered on the main test
    thread even after its sync_api tests complete. Sync tests that run
    later in the session then can't use ``asyncio.run`` or
    ``loop.run_until_complete`` — both raise "another loop is running".
    Dispatching to a worker thread isn't an option either, because
    SQLite connections are bound to their creator thread and synthesize
    queries the test fixture's connection.

    The fix: snapshot the leaked running-loop reference, clear it for
    the duration of our run, and restore it afterward. The leaked loop
    isn't actually doing anything (sync_api runs Playwright work in a
    sibling greenlet, not on this loop) so unregistering it briefly is
    safe. ``run_until_complete`` then proceeds normally on a fresh loop.
    """
    leaked = _asyncio_events._get_running_loop()
    if leaked is not None:
        _asyncio_events._set_running_loop(None)
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    finally:
        if leaked is not None:
            _asyncio_events._set_running_loop(leaked)


@pytest.fixture(autouse=True)
def _strip_leaked_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip live-deployment env vars that the dev shell leaks into pytest.

    Live settings.json env (INJECT_MODE=deferred, embeddings backend) leaks
    into test runs via the shell's environment. Claude Code also sets
    ``CLAUDE_CODE_SESSION_ID`` (and ``CLAUDE_PROJECT_DIR``) in the env of
    shell tool subprocesses, but NOT in the env of spawned stdio MCP servers.
    Several tests exercise "what happens when no session env var is set" by
    calling ``monkeypatch.delenv("CLAUDE_SESSION_ID")``, not realising
    production code also reads ``CLAUDE_CODE_SESSION_ID`` — so those tests
    pass in CI but fail when run from a Claude Code shell.

    This autouse fixture clears the leaking variables at the start of every
    test. Tests that need the env var must ``monkeypatch.setenv`` it
    explicitly. (No effect when running outside Claude Code or with unset
    better-memory settings: the vars weren't set in the first place.)
    """
    for var in (
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
        "BETTER_MEMORY_INJECT_MODE",
        "BETTER_MEMORY_EMBEDDINGS_BACKEND",
        "BETTER_MEMORY_CONTEXT_VEC_FLOOR",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_better_memory_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin ``BETTER_MEMORY_HOME`` to a per-test tmp dir.

    The storage backend is resolved from ``$BETTER_MEMORY_HOME/settings.json``
    when the ``BETTER_MEMORY_STORAGE_BACKEND`` env var is unset, so any test
    calling ``get_config()`` / ``resolve_storage_backend()`` without pinning
    the home would read the developer's real ``~/.better-memory/settings.json``
    and could silently flip to the agentcore backend. Tests that deliberately
    exercise the real-home default explicitly ``delenv`` the variable; tests
    that need a specific home ``setenv`` over this pin.

    (E2E tests are unaffected: they build child-process environments from
    scratch via the ``isolated_env`` helpers and never inherit this value.)
    """
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / "bm-home"))


@pytest.fixture
def tmp_memory_db(tmp_path: Path) -> Iterator[Path]:
    """Yield a path to a fresh (non-existent) SQLite database file.

    The file itself is not created; callers are responsible for opening /
    initialising the database. ``tmp_path`` cleanup removes any file the test
    creates.
    """
    yield tmp_path / "memory.db"


@pytest.fixture
def tmp_knowledge_base(tmp_path: Path) -> Iterator[Path]:
    """Yield a path to an empty temporary directory for knowledge-base files."""
    kb = tmp_path / "knowledge-base"
    kb.mkdir()
    yield kb


def seed_pending_episodes(
    conn,
    project: str,
    n: int,
    obs_per_episode: int = 2,
    tech: str | None = None,
) -> list[str]:
    """Seed N closed-pending episodes (synthesized_at NULL), each with M active observations.

    Returns episode ids in creation order. Each episode is closed
    (outcome='success', close_reason='goal_complete'), starts at
    increasing wall-clock minutes so ORDER BY ended_at is deterministic.

    Used by service / UI / MCP tests that need a pre-populated pending
    queue without re-implementing fixture SQL each time.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    ids: list[str] = []
    for i in range(n):
        eid = f"seeded-ep-{i:03d}"
        started = base + timedelta(minutes=i * 10)
        ended = started + timedelta(minutes=5)
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
            "close_reason, goal, tech, synthesized_at) "
            "VALUES (?, ?, ?, ?, 'success', 'goal_complete', ?, ?, NULL)",
            (eid, project, started.isoformat(), ended.isoformat(),
             f"goal {i}", tech),
        )
        for j in range(obs_per_episode):
            obs_id = f"{eid}-obs-{j}"
            obs_time = (started + timedelta(minutes=j + 1)).isoformat()
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id, "
                "status, outcome, created_at, status_changed_at, tech) "
                "VALUES (?, ?, ?, ?, 'active', 'success', ?, ?, ?)",
                (obs_id, f"obs content {j}", project, eid,
                 obs_time, obs_time, tech),
            )
        ids.append(eid)
    conn.commit()
    return ids
