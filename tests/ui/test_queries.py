"""Unit tests for better_memory.ui.queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.insight import Insight
from better_memory.ui.queries import (
    KanbanCounts,
    ObservationListRow,
    kanban_counts,
    list_candidates,
    list_insights,
    list_observations,
    list_promoted,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    status: str = "active",
) -> None:
    conn.execute(
        """
        INSERT INTO observations (id, content, project, status)
        VALUES (?, ?, ?, ?)
        """,
        (id, f"obs-{id}", project, status),
    )
    conn.commit()


def _insert_insight(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO insights (id, title, content, project, status, polarity)
        VALUES (?, ?, ?, ?, ?, 'neutral')
        """,
        (id, f"title-{id}", f"content-{id}", project, status),
    )
    conn.commit()


class TestKanbanCounts:
    def test_empty_project_returns_zero_counts(
        self, conn: sqlite3.Connection
    ) -> None:
        counts = kanban_counts(conn, project="empty-proj")
        assert counts == KanbanCounts(
            observations=0, candidates=0, insights=0, promoted=0
        )

    def test_counts_by_status_and_project(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(conn, id="o1", project="p1")
        _insert_observation(conn, id="o2", project="p1")
        _insert_observation(conn, id="o3", project="p1", status="archived")
        _insert_observation(conn, id="o4", project="p2")  # other project

        _insert_insight(conn, id="c1", project="p1", status="pending_review")
        _insert_insight(conn, id="c2", project="p1", status="pending_review")
        _insert_insight(conn, id="i1", project="p1", status="confirmed")
        _insert_insight(conn, id="pr1", project="p1", status="promoted")
        _insert_insight(conn, id="r1", project="p1", status="retired")

        counts = kanban_counts(conn, project="p1")
        assert counts == KanbanCounts(
            observations=2,  # only active, only p1
            candidates=2,
            insights=1,
            promoted=1,
        )


class TestListObservations:
    def test_empty_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        assert list_observations(conn, project="empty") == []

    def test_returns_active_only_ordered_recent_first(
        self, conn: sqlite3.Connection
    ) -> None:
        # created_at defaults to CURRENT_TIMESTAMP, so insert in order.
        _insert_observation(conn, id="old", project="p")
        _insert_observation(conn, id="new", project="p")
        _insert_observation(conn, id="arc", project="p", status="archived")
        rows = list_observations(conn, project="p")
        ids = [r.id for r in rows]
        assert ids == ["new", "old"]  # DESC by created_at
        assert all(isinstance(r, ObservationListRow) for r in rows)

    def test_respects_limit(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            _insert_observation(conn, id=f"o{i}", project="p")
        rows = list_observations(conn, project="p", limit=2)
        assert len(rows) == 2


class TestListInsightsByStatus:
    def test_list_candidates(self, conn: sqlite3.Connection) -> None:
        _insert_insight(conn, id="c1", project="p", status="pending_review")
        _insert_insight(conn, id="c2", project="p", status="pending_review")
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        candidates = list_candidates(conn, project="p")
        assert [i.id for i in candidates] == ["c2", "c1"]  # newest first
        assert all(isinstance(i, Insight) for i in candidates)

    def test_list_insights_returns_confirmed_only(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="c1", project="p", status="pending_review")
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        _insert_insight(conn, id="pr1", project="p", status="promoted")
        result = list_insights(conn, project="p")
        assert [i.id for i in result] == ["i1"]

    def test_list_promoted_returns_promoted_only(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", status="confirmed")
        _insert_insight(conn, id="pr1", project="p", status="promoted")
        _insert_insight(conn, id="pr2", project="p", status="promoted")
        result = list_promoted(conn, project="p")
        assert sorted(i.id for i in result) == ["pr1", "pr2"]
