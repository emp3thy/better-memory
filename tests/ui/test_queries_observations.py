"""Tests for observation-related UI query helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.ui.queries import (
    ObservationRow,
    observation_list_for_ui,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_episode(conn, *, eid: str = "ep-1", project: str = "proj-a") -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at) "
        "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
        (eid, project),
    )


def _seed_obs(
    conn,
    *,
    oid: str,
    project: str = "proj-a",
    component: str | None = "ui_launcher",
    theme: str | None = "bug",
    outcome: str = "neutral",
    status: str = "active",
    content: str = "test obs",
    episode_id: str = "ep-1",
    created_at: str = "2026-04-26T10:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, component, theme, outcome, status, "
        " episode_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            oid, content, project, component, theme, outcome, status,
            episode_id, created_at,
        ),
    )
    conn.commit()


class TestObservationListForUi:
    def test_returns_empty_when_no_observations(self, conn):
        rows = observation_list_for_ui(conn, project="proj-a")
        assert rows == []

    def test_returns_all_when_no_filters(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        _seed_obs(conn, oid="o-2")

        rows = observation_list_for_ui(conn, project="proj-a")
        ids = {r.id for r in rows}
        assert ids == {"o-1", "o-2"}

    def test_filters_by_project(self, conn):
        _seed_episode(conn, eid="ep-a", project="proj-a")
        _seed_episode(conn, eid="ep-b", project="proj-b")
        _seed_obs(conn, oid="o-a", project="proj-a", episode_id="ep-a")
        _seed_obs(conn, oid="o-b", project="proj-b", episode_id="ep-b")

        rows = observation_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["o-a"]

    def test_filters_by_status(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-active", status="active")
        _seed_obs(conn, oid="o-archived", status="archived")

        rows = observation_list_for_ui(
            conn, project="proj-a", status="active"
        )
        assert [r.id for r in rows] == ["o-active"]

    def test_filters_by_outcome(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-fail", outcome="failure")
        _seed_obs(conn, oid="o-ok", outcome="success")

        rows = observation_list_for_ui(
            conn, project="proj-a", outcome="failure"
        )
        assert [r.id for r in rows] == ["o-fail"]

    def test_filters_by_component(self, conn):
        _seed_episode(conn)
        _seed_obs(conn, oid="o-ui", component="ui_launcher")
        _seed_obs(conn, oid="o-mcp", component="mcp")

        rows = observation_list_for_ui(
            conn, project="proj-a", component="ui_launcher"
        )
        assert [r.id for r in rows] == ["o-ui"]

    def test_orders_newest_first(self, conn):
        _seed_episode(conn)
        _seed_obs(
            conn, oid="o-old", created_at="2026-04-25T10:00:00+00:00"
        )
        _seed_obs(
            conn, oid="o-new", created_at="2026-04-26T10:00:00+00:00"
        )

        rows = observation_list_for_ui(conn, project="proj-a")
        assert [r.id for r in rows] == ["o-new", "o-old"]

    def test_respects_limit(self, conn):
        _seed_episode(conn)
        for i in range(5):
            _seed_obs(
                conn,
                oid=f"o-{i}",
                created_at=f"2026-04-26T10:00:0{i}+00:00",
            )

        rows = observation_list_for_ui(conn, project="proj-a", limit=3)
        assert len(rows) == 3

    def test_row_shape_matches_dataclass(self, conn):
        _seed_episode(conn)
        _seed_obs(
            conn,
            oid="o-1",
            content="hello",
            component="ui_launcher",
            theme="bug",
            outcome="failure",
            status="active",
            created_at="2026-04-26T10:00:00+00:00",
        )

        [row] = observation_list_for_ui(conn, project="proj-a")
        assert isinstance(row, ObservationRow)
        assert row.id == "o-1"
        assert row.content == "hello"
        assert row.component == "ui_launcher"
        assert row.theme == "bug"
        assert row.outcome == "failure"
        assert row.status == "active"
        assert row.episode_id == "ep-1"


class TestObservationDetail:
    def test_returns_none_for_unknown_id(self, conn):
        from better_memory.ui.queries import observation_detail

        result = observation_detail(conn, observation_id="nope")
        assert result is None

    def test_returns_full_observation(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, tech, trigger_type, reinforcement_score, "
            " created_at) "
            "VALUES "
            "('o-1', 'hello', 'proj-a', 'ui_launcher', 'bug', 'failure', "
            " 'active', 'ep-1', 'python', 'review', 1.5, "
            " '2026-04-26T10:00:00+00:00')"
        )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert detail.observation.id == "o-1"
        assert detail.observation.content == "hello"
        assert detail.observation.project == "proj-a"
        assert detail.observation.component == "ui_launcher"
        assert detail.observation.theme == "bug"
        assert detail.observation.outcome == "failure"
        assert detail.observation.status == "active"
        assert detail.observation.tech == "python"
        assert detail.observation.trigger_type == "review"
        assert detail.observation.reinforcement_score == 1.5
        assert detail.observation.episode_id == "ep-1"
        assert detail.audit == []
        assert detail.reflections == []

    def test_returns_audit_timeline_newest_first(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        for at in (
            "2026-04-26T10:00:00+00:00",
            "2026-04-26T11:00:00+00:00",
            "2026-04-26T12:00:00+00:00",
        ):
            conn.execute(
                "INSERT INTO audit_log "
                "(id, entity_type, entity_id, action, actor, created_at) "
                "VALUES (?, 'observation', 'o-1', 'create', 'ai', ?)",
                (f"a-{at}", at),
            )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert len(detail.audit) == 3
        # Newest first.
        ats = [e.at for e in detail.audit]
        assert ats == sorted(ats, reverse=True)

    def test_returns_linked_reflections(self, conn):
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            " confidence, status, evidence_count, created_at, updated_at) "
            "VALUES "
            "('r-1', 'Linked', 'proj-a', NULL, 'general', 'do', "
            " 'uc', 'h', 0.8, 'confirmed', 1, "
            " '2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO reflection_sources (reflection_id, observation_id) "
            "VALUES ('r-1', 'o-1')"
        )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert len(detail.reflections) == 1
        assert detail.reflections[0].id == "r-1"
        assert detail.reflections[0].title == "Linked"
        assert detail.reflections[0].polarity == "do"
        assert detail.reflections[0].confidence == 0.8
        assert detail.reflections[0].status == "confirmed"
