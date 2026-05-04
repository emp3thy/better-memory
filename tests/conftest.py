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
