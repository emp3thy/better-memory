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


class TestStartForeground:
    def test_hardens_existing_background_episode(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        background_id = svc.open_background(
            session_id="sess-1", project="proj-a"
        )

        foreground_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="implement phase 2",
            tech="python",
        )

        assert foreground_id == background_id  # hardened, not replaced
        row = conn.execute(
            "SELECT goal, tech, hardened_at, ended_at FROM episodes WHERE id = ?",
            (background_id,),
        ).fetchone()
        assert row["goal"] == "implement phase 2"
        assert row["tech"] == "python"
        assert row["hardened_at"] == "2026-04-21T10:00:00+00:00"
        assert row["ended_at"] is None

    def test_supersedes_prior_foreground_when_goal_differs(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        first = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="first goal",
            tech="python",
        )

        # New goal comes in while first is still active.
        second = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="second goal",
            tech="sqlite",
        )

        assert second != first
        first_row = conn.execute(
            "SELECT ended_at, close_reason, outcome FROM episodes WHERE id = ?",
            (first,),
        ).fetchone()
        assert first_row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert first_row["close_reason"] == "superseded"
        assert first_row["outcome"] == "no_outcome"

        second_row = conn.execute(
            "SELECT goal, tech, hardened_at FROM episodes WHERE id = ?",
            (second,),
        ).fetchone()
        assert second_row["goal"] == "second goal"
        assert second_row["tech"] == "sqlite"
        assert second_row["hardened_at"] == "2026-04-21T10:00:00+00:00"

    def test_opens_new_foreground_when_no_background_exists(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        # No prior open_background call.
        foreground_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="brand new work",
        )

        row = conn.execute(
            "SELECT goal, hardened_at, started_at FROM episodes WHERE id = ?",
            (foreground_id,),
        ).fetchone()
        assert row["goal"] == "brand new work"
        assert row["hardened_at"] == "2026-04-21T10:00:00+00:00"
        # For a net-new foreground, started_at == hardened_at.
        assert row["started_at"] == "2026-04-21T10:00:00+00:00"

        # And the session is bound.
        session_row = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (foreground_id, "sess-1"),
        ).fetchone()
        assert session_row["left_at"] is None

    def test_tech_is_lowercased_on_write(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="lowercase me",
            tech="Python",
        )
        row = conn.execute(
            "SELECT tech FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        assert row["tech"] == "python"
