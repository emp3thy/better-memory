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

        # Session bindings: old episode's left_at stamped, new episode's left_at NULL.
        first_session = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (first, "sess-1"),
        ).fetchone()
        assert first_session["left_at"] == "2026-04-21T10:00:00+00:00"

        second_session = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (second, "sess-1"),
        ).fetchone()
        assert second_session["left_at"] is None

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

    def test_same_goal_resume_returns_same_episode(self, conn, fixed_clock):
        """Calling start_foreground twice with the same goal preserves the episode."""
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        first = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="ongoing work",
            tech="python",
        )
        # Second call with IDENTICAL goal.
        second = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="ongoing work",
            tech="python",
        )
        assert second == first

        # No superseded close happened.
        row = conn.execute(
            "SELECT ended_at, close_reason FROM episodes WHERE id = ?",
            (first,),
        ).fetchone()
        assert row["ended_at"] is None
        assert row["close_reason"] is None

        # Still only one episode row in the DB.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM episodes"
        ).fetchone()["c"]
        assert count == 1

    def test_empty_tech_stored_as_null(self, conn, fixed_clock):
        """tech='' is coerced to NULL, not stored as empty string."""
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.start_foreground(
            session_id="sess-1",
            project="proj-a",
            goal="no tech",
            tech="",
        )
        row = conn.execute(
            "SELECT tech FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        assert row["tech"] is None


class TestCloseActive:
    def test_closes_foreground_with_success(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        fg = svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="ship it"
        )

        closed_id = svc.close_active(
            session_id="sess-1",
            outcome="success",
            close_reason="goal_complete",
        )

        assert closed_id == fg
        row = conn.execute(
            "SELECT ended_at, close_reason, outcome, summary "
            "FROM episodes WHERE id = ?",
            (fg,),
        ).fetchone()
        assert row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert row["close_reason"] == "goal_complete"
        assert row["outcome"] == "success"
        assert row["summary"] is None

    def test_abandoned_close_records_summary(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-1", project="proj-a")
        svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="rejected work"
        )

        svc.close_active(
            session_id="sess-1",
            outcome="abandoned",
            close_reason="abandoned",
            summary="user asked me to stop and change direction",
        )

        row = conn.execute(
            "SELECT outcome, summary FROM episodes WHERE ended_at IS NOT NULL"
        ).fetchone()
        assert row["outcome"] == "abandoned"
        assert row["summary"] == "user asked me to stop and change direction"

    def test_marks_episode_session_left_at(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        fg = svc.start_foreground(
            session_id="sess-1", project="proj-a", goal="x"
        )

        svc.close_active(
            session_id="sess-1",
            outcome="success",
            close_reason="goal_complete",
        )

        row = conn.execute(
            "SELECT left_at FROM episode_sessions "
            "WHERE episode_id = ? AND session_id = ?",
            (fg, "sess-1"),
        ).fetchone()
        assert row["left_at"] == "2026-04-21T10:00:00+00:00"

    def test_raises_when_no_active_episode(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="No active episode"):
            svc.close_active(
                session_id="sess-nobody",
                outcome="success",
                close_reason="goal_complete",
            )

    def test_closes_background_episode_too(self, conn, fixed_clock):
        """Closing a background (unhardened) episode is valid (e.g. reconciliation with no_outcome)."""
        svc = EpisodeService(conn, clock=fixed_clock)
        bg = svc.open_background(session_id="sess-1", project="proj-a")

        closed_id = svc.close_active(
            session_id="sess-1",
            outcome="no_outcome",
            close_reason="session_end_reconciled",
        )

        assert closed_id == bg
        row = conn.execute(
            "SELECT goal, hardened_at, ended_at, outcome FROM episodes WHERE id = ?",
            (bg,),
        ).fetchone()
        assert row["goal"] is None
        assert row["hardened_at"] is None
        assert row["ended_at"] == "2026-04-21T10:00:00+00:00"
        assert row["outcome"] == "no_outcome"


class TestUnclosedEpisodes:
    def test_empty_when_no_episodes(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        assert svc.unclosed_episodes() == []

    def test_returns_open_episodes_across_sessions(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-a", project="p")
        svc.start_foreground(
            session_id="sess-b", project="p", goal="pending"
        )

        result = svc.unclosed_episodes()
        assert len(result) == 2

    def test_excludes_specified_sessions(self, conn, fixed_clock):
        """Current session's episode is filtered so the LLM doesn't prompt itself."""
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="sess-old", project="p")
        svc.open_background(session_id="sess-current", project="p")

        result = svc.unclosed_episodes(exclude_session_ids={"sess-current"})
        assert len(result) == 1
        # Only the old one should remain.
        assert result[0].project == "p"

    def test_excludes_closed_episodes(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.start_foreground(
            session_id="sess-a", project="p", goal="done"
        )
        svc.close_active(
            session_id="sess-a",
            outcome="success",
            close_reason="goal_complete",
        )

        assert svc.unclosed_episodes() == []


class TestListEpisodes:
    def test_empty_when_nothing(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        assert svc.list_episodes() == []

    def test_filter_by_project(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="s1", project="proj-a")
        svc.open_background(session_id="s2", project="proj-b")

        result = svc.list_episodes(project="proj-a")
        assert len(result) == 1
        assert result[0].project == "proj-a"

    def test_filter_by_outcome(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.start_foreground(
            session_id="s1", project="p", goal="won"
        )
        svc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )
        svc.start_foreground(
            session_id="s2", project="p", goal="lost"
        )
        svc.close_active(
            session_id="s2", outcome="abandoned", close_reason="abandoned"
        )

        result = svc.list_episodes(outcome="success")
        assert len(result) == 1
        assert result[0].goal == "won"

    def test_only_open(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        svc.open_background(session_id="s-open", project="p")
        svc.start_foreground(
            session_id="s-closed", project="p", goal="done"
        )
        svc.close_active(
            session_id="s-closed",
            outcome="success",
            close_reason="goal_complete",
        )

        result = svc.list_episodes(only_open=True)
        assert len(result) == 1
        # The still-open background has ended_at IS NULL.
        assert result[0].ended_at is None

    def test_orders_newest_first(self, conn):
        from datetime import UTC, datetime
        svc = EpisodeService(
            conn,
            clock=lambda: datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC),
        )
        first = svc.open_background(session_id="s1", project="p")

        svc_later = EpisodeService(
            conn,
            clock=lambda: datetime(2026, 4, 21, 11, 0, 0, tzinfo=UTC),
        )
        second = svc_later.open_background(session_id="s2", project="p")

        result = svc.list_episodes()
        assert [e.id for e in result] == [second, first]


class TestObservationServiceEpisodeIntegration:
    """The observation write path must produce a valid episode_id.

    These are integration-level tests against ObservationService + EpisodeService;
    the pure-ObservationService unit tests live in tests/services/test_observation.py.
    """

    async def test_observation_write_opens_background_episode_lazily(self, conn, fixed_clock):
        from better_memory.services.observation import ObservationService

        class _StubEmbedder:
            async def embed(self, text):
                return [0.0] * 768

        epsvc = EpisodeService(conn, clock=fixed_clock)
        obs_svc = ObservationService(
            conn,
            _StubEmbedder(),
            clock=fixed_clock,
            project_resolver=lambda: "proj-a",
            session_id="sess-1",
            episodes=epsvc,
        )

        obs_id = await obs_svc.create(content="first observation")

        row = conn.execute(
            "SELECT episode_id, tech FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["episode_id"] is not None
        assert row["tech"] is None

        # Subsequent writes reuse the same background episode.
        obs_id2 = await obs_svc.create(content="second")
        row2 = conn.execute(
            "SELECT episode_id FROM observations WHERE id = ?", (obs_id2,)
        ).fetchone()
        assert row2["episode_id"] == row["episode_id"]

    async def test_observation_accepts_tech_parameter(self, conn, fixed_clock):
        from better_memory.services.observation import ObservationService

        class _StubEmbedder:
            async def embed(self, text):
                return [0.0] * 768

        epsvc = EpisodeService(conn, clock=fixed_clock)
        obs_svc = ObservationService(
            conn,
            _StubEmbedder(),
            clock=fixed_clock,
            project_resolver=lambda: "proj-a",
            session_id="sess-1",
            episodes=epsvc,
        )

        obs_id = await obs_svc.create(content="x", tech="Python")
        row = conn.execute(
            "SELECT tech FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        # tech is lowercased by the service.
        assert row["tech"] == "python"
