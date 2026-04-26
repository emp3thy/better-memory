"""Tests for episode-related UI query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.ui.queries import (
    EpisodeDetail,
    EpisodeObservationRow,
    EpisodeReflectionRow,
    EpisodeRow,
    episode_detail,
    episode_list_for_ui,
    unclosed_episode_count,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _ts(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, 0, 0, tzinfo=UTC).isoformat()


class TestEpisodeListForUi:
    def test_returns_empty_list_when_no_episodes(self, conn):
        rows = episode_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_rows_for_project_newest_first(self, conn):
        clock_a = lambda: datetime(2026, 4, 22, 9, 0, 0, tzinfo=UTC)
        clock_b = lambda: datetime(2026, 4, 24, 9, 0, 0, tzinfo=UTC)
        EpisodeService(conn, clock=clock_a).open_background(
            session_id="s1", project="proj-a"
        )
        EpisodeService(conn, clock=clock_b).open_background(
            session_id="s2", project="proj-a"
        )

        rows = episode_list_for_ui(conn, project="proj-a")
        assert len(rows) == 2
        assert rows[0].started_at == _ts(2026, 4, 24, 9)
        assert rows[1].started_at == _ts(2026, 4, 22, 9)

    def test_filters_by_project(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        EpisodeService(conn).open_background(session_id="s2", project="proj-b")

        rows = episode_list_for_ui(conn, project="proj-a")
        assert len(rows) == 1
        assert rows[0].project == "proj-a"

    def test_includes_goal_tech_outcome_close_reason(self, conn):
        svc = EpisodeService(conn)
        ep_id = svc.open_background(session_id="s1", project="proj-a")
        svc.start_foreground(
            session_id="s1", project="proj-a", goal="ship feature X", tech="python"
        )
        svc.close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )

        [row] = episode_list_for_ui(conn, project="proj-a")
        assert row.id == ep_id
        assert row.goal == "ship feature X"
        assert row.tech == "python"
        assert row.outcome == "success"
        assert row.close_reason == "goal_complete"
        assert row.ended_at is not None

    def test_observation_and_reflection_counts(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        # Two observations on this episode.
        for i in range(2):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, 'proj-a', ?)",
                (f"obs-{i}", f"content {i}", ep_id),
            )
        # One reflection sourced from one of those observations.
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, created_at, updated_at) "
            "VALUES ('refl-1', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-0')"
        )
        conn.commit()

        [row] = episode_list_for_ui(conn, project="proj-a")
        assert row.observation_count == 2
        assert row.reflection_count == 1

    def test_reflection_count_deduplicates_multi_source_reflection(self, conn):
        """A single reflection sourced from two observations in the same
        episode counts once, not twice — the COUNT(DISTINCT) guard."""
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        for i in range(2):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, 'proj-a', ?)",
                (f"obs-{i}", f"content {i}", ep_id),
            )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, created_at, updated_at) "
            "VALUES ('refl-1', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')"
        )
        # Same reflection sourced from BOTH observations.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-0'), ('refl-1', 'obs-1')"
        )
        conn.commit()

        [row] = episode_list_for_ui(conn, project="proj-a")
        assert row.observation_count == 2
        assert row.reflection_count == 1  # not 2 — DISTINCT collapses

    def test_limit_truncates_results(self, conn):
        """limit= caps the number of episodes returned."""
        for i in range(3):
            EpisodeService(conn).open_background(session_id=f"s{i}", project="proj-a")

        rows = episode_list_for_ui(conn, project="proj-a", limit=2)
        assert len(rows) == 2


class TestEpisodeDetail:
    def test_returns_none_for_missing_episode(self, conn):
        assert episode_detail(conn, episode_id="nope") is None

    def test_returns_episode_with_no_observations_or_reflections(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        detail = episode_detail(conn, episode_id=ep_id)
        assert detail is not None
        assert detail.episode.id == ep_id
        assert detail.observations == []
        assert detail.reflections == []

    def test_returns_observations_newest_first(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        # Insert with explicit created_at to control ordering
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, component, theme, outcome, "
            "created_at) "
            "VALUES (?, ?, 'proj-a', ?, ?, ?, ?, ?)",
            ("obs-old", "older content", ep_id, "comp", "bug", "failure",
             "2026-04-24T08:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, component, theme, outcome, "
            "created_at) "
            "VALUES (?, ?, 'proj-a', ?, ?, ?, ?, ?)",
            ("obs-new", "newer content", ep_id, "comp", "decision", "success",
             "2026-04-24T10:00:00+00:00"),
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert [o.id for o in detail.observations] == ["obs-new", "obs-old"]
        assert detail.observations[0].component == "comp"
        assert detail.observations[0].theme == "decision"
        assert detail.observations[0].outcome == "success"

    def test_returns_reflections_with_owning_episode_outcome(self, conn):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]

        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES ('obs-1', 'c', 'proj-a', ?)",
            (ep_id,),
        )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, status, created_at, updated_at) "
            "VALUES ('refl-1', 'lesson', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, 'pending_review', '2026-04-25T00:00:00+00:00', "
            "'2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-1')"
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert len(detail.reflections) == 1
        r = detail.reflections[0]
        assert r.id == "refl-1"
        assert r.title == "lesson"
        assert r.phase == "general"
        assert r.polarity == "do"
        assert r.status == "pending_review"
        assert r.confidence == 0.8

    def test_dedupes_reflections_when_multiple_observations_share_one(
        self, conn
    ):
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        for i in range(2):
            conn.execute(
                "INSERT INTO observations (id, content, project, episode_id) "
                "VALUES (?, ?, 'proj-a', ?)",
                (f"obs-{i}", "c", ep_id),
            )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, created_at, updated_at) "
            "VALUES ('refl-1', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.8, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-1', 'obs-0'), ('refl-1', 'obs-1')"
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert len(detail.reflections) == 1

    def test_includes_observations_regardless_of_status(self, conn):
        """Drawer is historical: archived observations still appear."""
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, status) "
            "VALUES ('obs-arch', 'archived obs', 'proj-a', ?, 'archived')",
            (ep_id,),
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        ids = [o.id for o in detail.observations]
        assert "obs-arch" in ids

    def test_includes_retired_reflections(self, conn):
        """Drawer is historical: retired reflections still appear."""
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id) "
            "VALUES ('obs-1', 'c', 'proj-a', ?)",
            (ep_id,),
        )
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, phase, polarity, use_cases, hints, "
            "confidence, status, created_at, updated_at) "
            "VALUES ('refl-retired', 't', 'proj-a', 'general', 'do', 'u', 'h', "
            "0.5, 'retired', '2026-04-25T00:00:00+00:00', "
            "'2026-04-25T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('refl-retired', 'obs-1')"
        )
        conn.commit()

        detail = episode_detail(conn, episode_id=ep_id)
        assert len(detail.reflections) == 1
        assert detail.reflections[0].status == "retired"


class TestUnclosedEpisodeCount:
    def test_zero_when_no_open_episodes(self, conn):
        assert unclosed_episode_count(conn, project="proj-a") == 0

    def test_counts_open_episodes_for_project(self, conn):
        # Open background for proj-a (counts).
        EpisodeService(conn).open_background(session_id="s1", project="proj-a")
        # Closed background for proj-a (does NOT count).
        svc2 = EpisodeService(conn)
        svc2.open_background(session_id="s2", project="proj-a")
        svc2.close_active(
            session_id="s2", outcome="abandoned", close_reason="abandoned"
        )
        # Open background for proj-b (does NOT count — wrong project).
        EpisodeService(conn).open_background(session_id="s3", project="proj-b")

        assert unclosed_episode_count(conn, project="proj-a") == 1
