"""Unit tests for better_memory.services.consolidation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.consolidation import (
    ObservationCluster,
    find_clusters,
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
    component: str | None = None,
    theme: str | None = None,
    status: str = "active",
    validated_true: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO observations
            (id, content, project, component, theme, status, validated_true)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (id, f"content-{id}", project, component, theme, status, validated_true),
    )
    conn.commit()


class TestFindClusters:
    def test_empty_returns_empty(self, conn: sqlite3.Connection) -> None:
        assert find_clusters(conn, project="p") == []

    def test_groups_by_component_and_theme(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn,
                id=f"a{i}",
                project="p",
                component="api",
                theme="retry",
                validated_true=1,
            )
        for i in range(3):
            _insert_observation(
                conn,
                id=f"b{i}",
                project="p",
                component="db",
                theme="migration",
                validated_true=1,
            )
        clusters = find_clusters(conn, project="p")
        assert len(clusters) == 2
        keys = {(c.component, c.theme) for c in clusters}
        assert keys == {("api", "retry"), ("db", "migration")}
        for c in clusters:
            assert len(c.observation_ids) == 3

    def test_skips_clusters_below_min_size(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_observation(
            conn, id="a1", project="p", component="api", theme="retry",
            validated_true=1,
        )
        _insert_observation(
            conn, id="a2", project="p", component="api", theme="retry",
            validated_true=1,
        )
        clusters = find_clusters(conn, project="p", min_size=3)
        assert clusters == []

    def test_skips_clusters_below_min_validated(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=0,
            )
        clusters = find_clusters(conn, project="p", min_validated=2)
        assert clusters == []

        _insert_observation(
            conn, id="a3", project="p",
            component="api", theme="retry", validated_true=2,
        )
        clusters = find_clusters(conn, project="p", min_validated=2)
        assert len(clusters) == 1
        assert set(clusters[0].observation_ids) == {"a0", "a1", "a2", "a3"}

    def test_excludes_non_active_status(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            _insert_observation(
                conn, id=f"a{i}", project="p",
                component="api", theme="retry", validated_true=1,
            )
        _insert_observation(
            conn, id="consolidated", project="p",
            component="api", theme="retry", validated_true=1,
            status="consolidated",
        )
        clusters = find_clusters(conn, project="p")
        assert len(clusters) == 1
        assert "consolidated" not in clusters[0].observation_ids
