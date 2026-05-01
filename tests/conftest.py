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
