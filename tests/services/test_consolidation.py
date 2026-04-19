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
    ObservationForPrompt,
    build_draft_prompt,
    existing_insight_for_cluster,
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


class TestBuildDraftPrompt:
    def test_renders_spec_prompt(self) -> None:
        observations = [
            ObservationForPrompt(
                id="o1",
                created_at="2026-03-01T10:00:00+00:00",
                content="The API retries on 503s with exponential backoff.",
                outcome="success",
            ),
            ObservationForPrompt(
                id="o2",
                created_at="2026-03-05T14:22:00+00:00",
                content="Retrying on 4xx is always wrong — they won't resolve.",
                outcome="failure",
            ),
            ObservationForPrompt(
                id="o3",
                created_at="2026-03-10T09:15:00+00:00",
                content="Add jitter to avoid thundering-herd retries.",
                outcome="success",
            ),
        ]
        prompt = build_draft_prompt(observations)
        assert "Here are 3 observations about the same pattern:" in prompt
        assert "o1" in prompt
        assert "2026-03-01" in prompt
        assert "success" in prompt
        assert "Write a single insight that:" in prompt
        assert "Generalises the pattern in present tense" in prompt
        assert "Is concise" in prompt


def _insert_insight(
    conn: sqlite3.Connection,
    *,
    id: str,
    project: str,
    component: str | None,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO insights "
        "(id, title, content, project, component, status, polarity) "
        "VALUES (?, ?, ?, ?, ?, ?, 'neutral')",
        (id, f"t-{id}", f"c-{id}", project, component, status),
    )
    conn.commit()


class TestExistingInsightForCluster:
    def test_returns_none_when_no_match(self, conn: sqlite3.Connection) -> None:
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_finds_confirmed_match_same_project_component(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", component="api",
                        status="confirmed")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        result = existing_insight_for_cluster(conn, cluster)
        assert result is not None
        assert result.id == "i1"

    def test_ignores_pending_review(self, conn: sqlite3.Connection) -> None:
        _insert_insight(conn, id="c1", project="p", component="api",
                        status="pending_review")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_ignores_different_component(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="i1", project="p", component="db",
                        status="confirmed")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        assert existing_insight_for_cluster(conn, cluster) is None

    def test_accepts_promoted_as_match(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_insight(conn, id="pr1", project="p", component="api",
                        status="promoted")
        cluster = ObservationCluster(
            project="p", component="api", theme="retry",
            observation_ids=["o1"], total_validated_true=0,
        )
        result = existing_insight_for_cluster(conn, cluster)
        assert result is not None
        assert result.id == "pr1"
