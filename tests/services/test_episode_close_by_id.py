"""Tests for EpisodeService.close_by_id (cross-session UI close)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService


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
    fixed = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestCloseById:
    def test_closes_open_episode_and_marks_all_session_bindings(
        self, conn, fixed_clock
    ):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        # Simulate a continuing-session bind from a second session.
        conn.execute(
            "INSERT INTO episode_sessions "
            "(episode_id, session_id, joined_at) VALUES (?, ?, ?)",
            (episode_id, "sess-2", "2026-04-25T11:00:00+00:00"),
        )
        conn.commit()

        closed = svc.close_by_id(
            episode_id=episode_id,
            outcome="abandoned",
            close_reason="abandoned",
            summary="user marked as abandoned in UI",
        )

        assert closed == episode_id
        ep = conn.execute(
            "SELECT ended_at, close_reason, outcome, summary "
            "FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert ep["ended_at"] == "2026-04-25T12:00:00+00:00"
        assert ep["close_reason"] == "abandoned"
        assert ep["outcome"] == "abandoned"
        assert ep["summary"] == "user marked as abandoned in UI"

        rows = conn.execute(
            "SELECT session_id, left_at FROM episode_sessions "
            "WHERE episode_id = ? ORDER BY session_id",
            (episode_id,),
        ).fetchall()
        assert len(rows) == 2
        # All open bindings get stamped; previously-closed bindings are
        # left alone.
        assert all(r["left_at"] == "2026-04-25T12:00:00+00:00" for r in rows)

    def test_raises_when_episode_does_not_exist(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Episode not found"):
            svc.close_by_id(
                episode_id="does-not-exist",
                outcome="abandoned",
                close_reason="abandoned",
            )

    def test_raises_when_episode_already_closed(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        svc.close_by_id(
            episode_id=episode_id, outcome="success", close_reason="goal_complete"
        )
        with pytest.raises(ValueError, match="already closed"):
            svc.close_by_id(
                episode_id=episode_id,
                outcome="abandoned",
                close_reason="abandoned",
            )

    def test_summary_is_optional(self, conn, fixed_clock):
        svc = EpisodeService(conn, clock=fixed_clock)
        episode_id = svc.open_background(session_id="sess-1", project="proj-a")
        svc.close_by_id(
            episode_id=episode_id,
            outcome="no_outcome",
            close_reason="session_end_reconciled",
        )
        row = conn.execute(
            "SELECT summary FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row["summary"] is None
