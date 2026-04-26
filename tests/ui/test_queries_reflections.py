"""Tests for reflection-related UI query helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.ui.queries import (
    ReflectionDetail,
    ReflectionListRow,
    ReflectionSourceObservation,
    reflection_detail,
    reflection_list_for_ui,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(
    conn,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    created_at: str = "2026-04-25T10:00:00+00:00",
    updated_at: str = "2026-04-25T10:00:00+00:00",
    evidence_count: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        "confidence, status, evidence_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, title or f"title-{rid}", project, tech, phase, polarity,
            use_cases, hints, confidence, status, evidence_count,
            created_at, updated_at,
        ),
    )
    conn.commit()


class TestReflectionListForUi:
    def test_returns_empty_when_no_reflections(self, conn):
        rows = reflection_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_only_active_statuses_by_default(self, conn):
        _seed(conn, rid="r-pending", status="pending_review")
        _seed(conn, rid="r-confirmed", status="confirmed")
        _seed(conn, rid="r-retired", status="retired")
        _seed(conn, rid="r-superseded", status="superseded")

        rows = reflection_list_for_ui(conn, project="proj-a")
        ids = {r.id for r in rows}
        assert ids == {"r-pending", "r-confirmed"}

    def test_includes_specific_status_when_filtered(self, conn):
        _seed(conn, rid="r-pending", status="pending_review")
        _seed(conn, rid="r-retired", status="retired")

        rows = reflection_list_for_ui(
            conn, project="proj-a", status="retired"
        )
        assert [r.id for r in rows] == ["r-retired"]

    def test_filters_by_project(self, conn):
        _seed(conn, rid="r-a", project="proj-a")
        _seed(conn, rid="r-b", project="proj-b")
        rows = reflection_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["r-a"]

    def test_filters_by_tech(self, conn):
        _seed(conn, rid="r-py", tech="python")
        _seed(conn, rid="r-go", tech="go")
        _seed(conn, rid="r-none", tech=None)

        rows = reflection_list_for_ui(
            conn, project="proj-a", tech="python"
        )
        assert [r.id for r in rows] == ["r-py"]

    def test_filters_by_phase(self, conn):
        _seed(conn, rid="r-plan", phase="planning")
        _seed(conn, rid="r-impl", phase="implementation")
        _seed(conn, rid="r-gen", phase="general")

        rows = reflection_list_for_ui(
            conn, project="proj-a", phase="planning"
        )
        assert [r.id for r in rows] == ["r-plan"]

    def test_filters_by_polarity(self, conn):
        _seed(conn, rid="r-do", polarity="do")
        _seed(conn, rid="r-dont", polarity="dont")

        rows = reflection_list_for_ui(
            conn, project="proj-a", polarity="dont"
        )
        assert [r.id for r in rows] == ["r-dont"]

    def test_filters_by_min_confidence(self, conn):
        _seed(conn, rid="r-low", confidence=0.3)
        _seed(conn, rid="r-mid", confidence=0.6)
        _seed(conn, rid="r-high", confidence=0.9)

        rows = reflection_list_for_ui(
            conn, project="proj-a", min_confidence=0.6
        )
        assert {r.id for r in rows} == {"r-mid", "r-high"}

    def test_orders_by_confidence_desc_then_updated_at_desc(self, conn):
        _seed(
            conn, rid="r-mid-newer", confidence=0.6,
            updated_at="2026-04-25T12:00:00+00:00",
        )
        _seed(
            conn, rid="r-mid-older", confidence=0.6,
            updated_at="2026-04-25T08:00:00+00:00",
        )
        _seed(conn, rid="r-high", confidence=0.9)

        rows = reflection_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == [
            "r-high", "r-mid-newer", "r-mid-older",
        ]

    def test_row_includes_all_spec_fields(self, conn):
        _seed(
            conn, rid="r-1", project="proj-a", tech="python",
            phase="implementation", polarity="dont", confidence=0.85,
            use_cases="when X happens",
            hints="do Y",
            title="my title", evidence_count=3,
        )
        [row] = reflection_list_for_ui(conn, project="proj-a")
        assert row.id == "r-1"
        assert row.title == "my title"
        assert row.project == "proj-a"
        assert row.tech == "python"
        assert row.phase == "implementation"
        assert row.polarity == "dont"
        assert row.confidence == 0.85
        assert row.status == "confirmed"
        assert row.use_cases == "when X happens"
        assert row.evidence_count == 3

    def test_limit_truncates_results(self, conn):
        for i in range(3):
            _seed(conn, rid=f"r-{i}", confidence=0.5 + i * 0.1)
        rows = reflection_list_for_ui(
            conn, project="proj-a", limit=2
        )
        assert len(rows) == 2


class TestReflectionDetail:
    def test_returns_none_for_missing_reflection(self, conn):
        assert reflection_detail(conn, reflection_id="nope") is None

    def test_returns_reflection_with_no_sources(self, conn):
        _seed(conn, rid="r-1")
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.id == "r-1"
        assert detail.sources == []

    def test_returns_full_reflection_fields(self, conn):
        _seed(
            conn, rid="r-1", project="proj-a", tech="python",
            phase="implementation", polarity="dont", confidence=0.85,
            use_cases="when X", hints="do Y, then Z",
            title="my title", evidence_count=3,
        )
        detail = reflection_detail(conn, reflection_id="r-1")
        r = detail.reflection
        assert r.title == "my title"
        assert r.tech == "python"
        assert r.phase == "implementation"
        assert r.polarity == "dont"
        assert r.confidence == 0.85
        assert r.use_cases == "when X"
        assert r.hints == "do Y, then Z"
        assert r.evidence_count == 3

    def test_returns_sources_with_episode_outcome(self, conn):
        # Need an episode for observations to bind to.
        ep_id = EpisodeService(conn).open_background(
            session_id="s1", project="proj-a"
        )
        # Harden to give it a goal + close it as success.
        EpisodeService(conn).start_foreground(
            session_id="s1", project="proj-a", goal="ship feature", tech="python"
        )
        EpisodeService(conn).close_active(
            session_id="s1", outcome="success", close_reason="goal_complete"
        )

        # Insert two observations on this episode.
        for i in range(2):
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, episode_id, component, theme, outcome) "
                "VALUES (?, ?, 'proj-a', ?, 'comp', 'bug', 'failure')",
                (f"obs-{i}", f"content {i}", ep_id),
            )
        _seed(conn, rid="r-1")
        # Both observations source this reflection.
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'obs-0'), ('r-1', 'obs-1')"
        )
        conn.commit()

        detail = reflection_detail(conn, reflection_id="r-1")
        assert len(detail.sources) == 2
        for src in detail.sources:
            assert src.episode_goal == "ship feature"
            assert src.episode_outcome == "success"
            assert src.episode_close_reason == "goal_complete"
            assert src.component == "comp"
            assert src.theme == "bug"

    def test_sources_ordered_by_observation_created_at_desc(self, conn):
        ep_id = EpisodeService(conn).open_background(
            session_id="s1", project="proj-a"
        )
        # Two observations with explicit created_at to control ordering.
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-old', 'older', 'proj-a', ?, "
            "'2026-04-24T08:00:00+00:00')",
            (ep_id,),
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, created_at) "
            "VALUES ('obs-new', 'newer', 'proj-a', ?, "
            "'2026-04-24T10:00:00+00:00')",
            (ep_id,),
        )
        _seed(conn, rid="r-1")
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'obs-old'), ('r-1', 'obs-new')"
        )
        conn.commit()

        detail = reflection_detail(conn, reflection_id="r-1")
        assert [s.observation_id for s in detail.sources] == ["obs-new", "obs-old"]
