"""Tests for episode-related UI query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.ui.queries import EpisodeRow, episode_list_for_ui


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
