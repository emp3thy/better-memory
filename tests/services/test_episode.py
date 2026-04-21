"""Tests for EpisodeService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import Episode, EpisodeService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestOpenBackground:
    def test_creates_background_episode_with_null_goal(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        row = conn.execute(
            "SELECT id, project, goal, hardened_at, started_at, ended_at "
            "FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row["project"] == "proj-a"
        assert row["goal"] is None
        assert row["hardened_at"] is None
        assert row["ended_at"] is None
        assert row["started_at"] == "2026-04-21T10:00:00+00:00"

    def test_creates_episode_sessions_row(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        row = conn.execute(
            "SELECT episode_id, session_id, joined_at, left_at "
            "FROM episode_sessions WHERE episode_id = ? AND session_id = ?",
            (episode_id, "sess-1"),
        ).fetchone()
        assert row is not None
        assert row["joined_at"] == "2026-04-21T10:00:00+00:00"
        assert row["left_at"] is None


class TestActiveEpisode:
    def test_returns_none_when_no_active_episode(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        assert svc.active_episode("sess-never") is None

    def test_returns_background_episode_after_open(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        active = svc.active_episode("sess-1")
        assert isinstance(active, Episode)
        assert active.id == episode_id
        assert active.goal is None

    def test_does_not_return_closed_episode(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )
        conn.execute(
            "UPDATE episodes SET ended_at = ? WHERE id = ?",
            ("2026-04-21T11:00:00+00:00", episode_id),
        )
        conn.execute(
            "UPDATE episode_sessions SET left_at = ? "
            "WHERE episode_id = ? AND session_id = ?",
            ("2026-04-21T11:00:00+00:00", episode_id, "sess-1"),
        )
        conn.commit()
        assert svc.active_episode("sess-1") is None

    def test_other_session_does_not_see_episode(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        assert svc.active_episode("sess-other") is None
