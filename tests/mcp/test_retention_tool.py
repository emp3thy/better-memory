"""Tests for the memory.run_retention MCP tool."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionService


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
    fixed = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_no_outcome_episode_with_obs(conn) -> None:
    """Seed an episode + observation matching retention Rule C."""
    conn.execute(
        "INSERT INTO episodes "
        "(id, project, started_at, ended_at, outcome, close_reason) "
        "VALUES ('e1', 'p', '2026-04-01T00:00:00+00:00', "
        "'2026-04-01T00:00:00+00:00', 'no_outcome', "
        "'session_end_reconciled')"
    )
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, episode_id, status, "
        "created_at, status_changed_at) "
        "VALUES ('obs-1', 'c', 'p', 'e1', 'active', "
        "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')"
    )
    conn.commit()


class TestRunRetentionViaService:
    """The MCP tool is a thin wrapper; verify the service call shape."""

    def test_dry_run_returns_counts_without_writing(
        self, conn, fixed_clock
    ):
        _seed_no_outcome_episode_with_obs(conn)
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, dry_run=True
        )
        assert report.archived_via_no_outcome_episode == 1
        # Status NOT changed.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "active"

    def test_archive_only_flips_status(self, conn, fixed_clock):
        _seed_no_outcome_episode_with_obs(conn)
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=False
        )
        assert report.archived_via_no_outcome_episode == 1
        assert report.pruned == 0
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "archived"

    def test_prune_deletes_old_archived_unsourced_obs(
        self, conn, fixed_clock
    ):
        # An archived obs with no reflection_sources, archived 400 days
        # ago. With prune=True and prune_age_days=365, it should go.
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES ('e1', 'p', '2025-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, episode_id, status, "
            "created_at, status_changed_at) "
            "VALUES ('obs-old', 'c', 'p', 'e1', 'archived', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')"
        )
        conn.commit()

        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=365,
        )
        assert report.pruned == 1
        row = conn.execute(
            "SELECT id FROM observations WHERE id = 'obs-old'"
        ).fetchone()
        assert row is None  # deleted


class TestToolRegistration:
    def test_tool_is_registered_in_factory(self):
        """The MCP server registers memory.run_retention by name."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.run_retention" in tool_names

    def test_tool_input_schema_has_expected_properties(self):
        from better_memory.mcp.server import _tool_definitions

        tools = {t.name: t for t in _tool_definitions()}
        retention = tools["memory.run_retention"]
        schema = retention.inputSchema
        props = schema["properties"]
        assert "retention_days" in props
        assert "prune" in props
        assert "prune_age_days" in props
        assert "dry_run" in props
        assert schema.get("additionalProperties") is False
