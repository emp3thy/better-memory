"""End-to-end: observer hook -> spool file -> SpoolService.drain -> hook_events.

Invokes the observer as a subprocess (mirrors how Claude Code runs it),
confirms a file lands in the spool, then migrates a temp DB and drains the
spool, asserting the row appears in ``hook_events`` with the expected fields.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.spool import DrainReport, SpoolService


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    """Expected spool dir under a tmp BETTER_MEMORY_HOME. Created on first write."""
    return tmp_path / "spool"


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def test_observer_then_drain_produces_hook_events_row(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    payload = {
        "event_type": "tool_use",
        "tool": "Edit",
        "file": "services/auth.py",
        "content_snippet": "def login(): ...",
        "cwd": "/home/me/project",
        "session_id": "sess-int-1",
        "timestamp": "2026-04-18T12:34:56Z",
    }

    env = {**os.environ, "BETTER_MEMORY_HOME": str(tmp_spool.parent)}
    result = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.observer"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    # A file should now exist in the spool.
    files = list(tmp_spool.glob("*.json"))
    assert len(files) == 1

    # Drain the spool into the DB.
    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()
    assert report == DrainReport(drained=1, quarantined=0)
    assert list(tmp_spool.glob("*.json")) == []

    row = conn.execute(
        "SELECT event_type, tool, file, content_snippet, cwd, session_id, "
        "event_timestamp FROM hook_events"
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "tool_use"
    assert row["tool"] == "Edit"
    assert row["file"] == "services/auth.py"
    assert row["content_snippet"] == "def login(): ..."
    assert row["cwd"] == "/home/me/project"
    assert row["session_id"] == "sess-int-1"
    assert row["event_timestamp"] == "2026-04-18T12:34:56Z"


def test_session_close_then_drain_produces_session_end_row(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    env = {
        **os.environ,
        "BETTER_MEMORY_HOME": str(tmp_spool.parent),
        "CLAUDE_SESSION_ID": "sess-int-close",
    }
    result = subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_close"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()
    assert report.drained == 1
    assert report.quarantined == 0

    row = conn.execute(
        "SELECT event_type, session_id FROM hook_events"
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "session_end"
    assert row["session_id"] == "sess-int-close"
