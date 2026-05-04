"""Tests for SemanticMemoryService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


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
    fixed = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestCreate:
    def test_create_with_default_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="prefer terse replies", project="p1")
        assert memory_id  # non-empty id returned
        row = conn.execute(
            "SELECT content, project, scope, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "prefer terse replies"
        assert row["project"] == "p1"
        assert row["scope"] == "project"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"
        assert row["updated_at"] == "2026-05-04T12:00:00+00:00"

    def test_create_with_general_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(
            content="always assign per-step confidence",
            project="any",
            scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_create_rejects_invalid_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="scope"):
            svc.create(content="rule", project="p1", scope="invalid")

    def test_create_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="content"):
            svc.create(content="   ", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.create(content="", project="p1")


class TestUpdateText:
    def test_update_changes_content_and_bumps_updated_at(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="old text", project="p1")

        # Advance the clock to a later instant for the update.
        from datetime import timedelta
        later = (fixed_clock() + timedelta(hours=2)).isoformat()
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)

        svc.update_text(id=memory_id, content="new text")
        row = conn.execute(
            "SELECT content, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "new text"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"  # unchanged
        assert row["updated_at"] == later

    def test_update_raises_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.update_text(id="nope", content="x")

    def test_update_missing_id_rolls_back_implicit_transaction(
        self, conn, fixed_clock,
    ):
        """Regression for BugBot finding on PR #34: sqlite3 with default
        isolation_level opens an implicit BEGIN before the UPDATE; raising
        ValueError without rollback would strand the WAL write lock for
        any caller sharing this connection. Mirror of the
        ObservationService.set_outcome pattern.
        """
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.update_text(id="nope", content="x")
        # After the failed call, the connection must not be in a
        # transaction — otherwise the WAL write lock is still held.
        assert conn.in_transaction is False

    def test_update_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="original", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.update_text(id=memory_id, content="   ")
        # Original content unchanged.
        row = conn.execute(
            "SELECT content FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "original"


class TestDelete:
    def test_delete_removes_existing_row(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="x", project="p1")
        svc.delete(id=memory_id)
        row = conn.execute(
            "SELECT 1 FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is None

    def test_delete_is_idempotent_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # No exception when id doesn't exist.
        svc.delete(id="ghost")


class TestListForProject:
    def test_returns_empty_list_when_no_rows(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        assert svc.list_for_project(project="p1") == []

    def test_returns_only_project_rows_when_general_absent(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Two memories in p1, one in p2 (project-scope).
        svc.create(content="p1 rule", project="p1")
        svc.create(content="p1 other", project="p1")
        svc.create(content="p2 rule", project="p2")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        assert {m.project for m in memories} == {"p1"}
        assert {m.content for m in memories} == {"p1 rule", "p1 other"}

    def test_includes_general_from_other_projects(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1-only rule", project="p1")
        svc.create(content="cross-project rule", project="p2", scope="general")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        contents = {m.content for m in memories}
        assert "p1-only rule" in contents
        assert "cross-project rule" in contents
        # Confirm scope flag preserved on the read model.
        general_match = next(m for m in memories if m.scope == "general")
        assert general_match.project == "p2"

    def test_excludes_other_projects_project_scoped_memories(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="hidden p2 rule", project="p2")  # scope='project'
        memories = svc.list_for_project(project="p1")
        assert memories == []

    def test_orders_newest_first(self, conn, fixed_clock):
        from datetime import timedelta
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Three memories at t, t+1h, t+2h.
        svc._clock = lambda: fixed_clock()
        first_id = svc.create(content="oldest", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=1)
        second_id = svc.create(content="middle", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)
        third_id = svc.create(content="newest", project="p1")

        memories = svc.list_for_project(project="p1")
        assert [m.id for m in memories] == [third_id, second_id, first_id]
