"""Tests for reflection-related UI query helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.ui.queries import (
    RatingEvidenceRow,
    ReflectionDetail,
    ReflectionListRow,
    ReflectionSourceObservation,
    fetch_rating_evidence,
    reflection_detail,
    reflection_list_for_ui,
    reflection_provenance,
    reflection_row,
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
    scope: str = "project",
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        "confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, title or f"title-{rid}", project, tech, phase, polarity,
            use_cases, hints, confidence, status, evidence_count,
            created_at, updated_at, scope,
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
        assert detail is not None, "reflection_detail should return for the seeded reflection"
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
        assert detail is not None, "reflection_detail returns for two-source seed"
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
        assert detail is not None, "reflection_detail returns for ordered-sources seed"
        assert [s.observation_id for s in detail.sources] == ["obs-new", "obs-old"]

    def test_returns_default_project_scope_when_unspecified(self, conn):
        _seed(conn, rid="r-1")  # default scope='project'
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.scope == "project"

    def test_returns_general_scope_when_seeded_general(self, conn):
        _seed(conn, rid="r-1", scope="general")
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.scope == "general"

    def test_reflection_detail_composes_from_row_and_provenance(self, conn):
        """Composition pin: reflection_detail is recomposed from
        reflection_row + reflection_provenance and must produce the exact
        same objects those two helpers would produce independently."""
        ep_id = EpisodeService(conn).open_background(
            session_id="s1", project="proj-a"
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id) VALUES "
            "('obs-1', 'obs body', 'proj-a', ?)",
            (ep_id,),
        )
        _seed(conn, rid="r-1")
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'obs-1')"
        )
        conn.commit()

        detail = reflection_detail(conn, reflection_id="r-1")
        row = reflection_row(conn, reflection_id="r-1")
        prov = reflection_provenance(conn, reflection_id="r-1")
        assert detail is not None and row is not None
        assert detail.reflection == row
        assert detail.sources == prov

    def test_reflection_row_and_provenance_none_and_empty_for_missing(self, conn):
        assert reflection_row(conn, reflection_id="nope") is None
        assert reflection_provenance(conn, reflection_id="nope") == []


class TestUsefulCountInReadModel:
    def test_reflection_list_includes_useful_count(self, conn):
        from better_memory.ui.queries import reflection_list_for_ui
        # Seed a reflection with useful_count = 3.
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, useful_count, times_misled)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 3, 1)"""
        )
        conn.commit()
        rows = reflection_list_for_ui(conn, project="p")
        assert any(r.useful_count == 3 and r.times_misled == 1 for r in rows)

    def test_useful_only_filter(self, conn):
        from better_memory.ui.queries import reflection_list_for_ui
        # Seed: one with useful_count > 0, one with useful_count == 0.
        for rid, useful in [("r-useful", 2), ("r-unused", 0)]:
            conn.execute(
                """INSERT INTO reflections
                   (id, title, project, phase, polarity, use_cases, hints,
                    confidence, created_at, updated_at, useful_count, times_misled)
                   VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                           '2026-01-01', '2026-01-01', ?, 0)""",
                (rid, rid, useful),
            )
        conn.commit()
        rows = reflection_list_for_ui(conn, project="p", useful_only=True)
        ids = [r.id for r in rows]
        assert "r-useful" in ids
        assert "r-unused" not in ids

        # Default (no filter): both rows.
        rows_all = reflection_list_for_ui(conn, project="p")
        ids_all = [r.id for r in rows_all]
        assert "r-useful" in ids_all
        assert "r-unused" in ids_all


class TestUsefulMisledInDetail:
    def test_reflection_detail_includes_useful_and_misled_fields(self, conn):
        from better_memory.ui.queries import reflection_detail

        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at,
                useful_count, last_useful_at, times_misled, last_misled_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01',
                       5, '2026-05-11T10:00:00+00:00',
                       2, '2026-05-11T11:00:00+00:00')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.useful_count == 5
        assert detail.last_useful_at == "2026-05-11T10:00:00+00:00"
        assert detail.times_misled == 2
        assert detail.last_misled_at == "2026-05-11T11:00:00+00:00"

    def test_reflection_detail_defaults_to_zero_counts(self, conn):
        from better_memory.ui.queries import reflection_detail

        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.useful_count == 0
        assert detail.last_useful_at is None
        assert detail.times_misled == 0
        assert detail.last_misled_at is None


class TestOverlookedInReadModel:
    def test_reflection_list_includes_times_overlooked(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, times_overlooked)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 4)"""
        )
        conn.commit()
        rows = reflection_list_for_ui(conn, project="p")
        assert any(r.times_overlooked == 4 for r in rows)

    def test_reflection_list_times_overlooked_defaults_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        [row] = reflection_list_for_ui(conn, project="p")
        assert row.times_overlooked == 0

    def test_reflection_detail_includes_overlooked_fields(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at,
                times_overlooked, last_overlooked_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01',
                       3, '2026-05-17T11:00:00+00:00')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.times_overlooked == 3
        assert detail.last_overlooked_at == "2026-05-17T11:00:00+00:00"

    def test_reflection_detail_overlooked_defaults_zero(self, conn):
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        detail = reflection_detail(conn, reflection_id="r1")
        assert detail is not None
        assert detail.times_overlooked == 0
        assert detail.last_overlooked_at is None


def _seed_exposure(
    conn,
    *,
    kind: str,
    memory_id: str,
    session_id: str = "s-1",
    exposed_at: str = "2026-05-11T10:00:00+00:00",
    rated_at: str | None = "2026-05-11T11:00:00+00:00",
    classification: str | None = "cited",
    evidence: str | None = "some evidence",
) -> None:
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source, "
        "rated_at, classification, evidence) "
        "VALUES (?, ?, ?, ?, 'bootstrap', ?, ?, ?)",
        (session_id, kind, memory_id, exposed_at, rated_at, classification, evidence),
    )
    conn.commit()


class TestFetchRatingEvidence:
    def test_returns_empty_when_no_rows(self, conn):
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert rows == []

    def test_returns_row_with_expected_fields(self, conn):
        _seed_exposure(
            conn, kind="reflection", memory_id="r1",
            classification="shaped", evidence="guided the fix",
            rated_at="2026-05-11T11:00:00+00:00",
        )
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert rows == [
            RatingEvidenceRow(
                classification="shaped",
                evidence="guided the fix",
                rated_at="2026-05-11T11:00:00+00:00",
            )
        ]

    def test_excludes_rows_with_null_evidence(self, conn):
        _seed_exposure(
            conn, kind="reflection", memory_id="r1", exposed_at="a",
            classification="ignored", evidence=None,
        )
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert rows == []

    def test_isolates_by_kind_and_memory_id(self, conn):
        _seed_exposure(
            conn, kind="reflection", memory_id="r1", exposed_at="a",
            evidence="for r1",
        )
        _seed_exposure(
            conn, kind="semantic", memory_id="r1", exposed_at="b",
            evidence="for semantic r1",
        )
        _seed_exposure(
            conn, kind="reflection", memory_id="r2", exposed_at="c",
            evidence="for r2",
        )
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert [r.evidence for r in rows] == ["for r1"]

    def test_orders_newest_rated_at_first(self, conn):
        _seed_exposure(
            conn, kind="reflection", memory_id="r1", exposed_at="a",
            rated_at="2026-05-11T09:00:00+00:00", evidence="older",
        )
        _seed_exposure(
            conn, kind="reflection", memory_id="r1", exposed_at="b",
            rated_at="2026-05-11T12:00:00+00:00", evidence="newer",
        )
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert [r.evidence for r in rows] == ["newer", "older"]

    def test_limit_caps_row_count(self, conn):
        for i in range(15):
            _seed_exposure(
                conn, kind="reflection", memory_id="r1",
                exposed_at=f"exp-{i}",
                rated_at=f"2026-05-11T{10 + i:02d}:00:00+00:00",
                evidence=f"evidence-{i}",
            )
        rows = fetch_rating_evidence(conn, "reflection", "r1")
        assert len(rows) == 10  # default limit

        rows = fetch_rating_evidence(conn, "reflection", "r1", limit=3)
        assert len(rows) == 3
