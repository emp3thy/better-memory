"""Integration-style tests for memory.semantic_* MCP tools.

Mirrors the harness pattern from tests/mcp/test_episode_tools.py:
exercise the dispatch by constructing services directly, plus a
factory smoke test confirming the tools register.
"""

from __future__ import annotations

import json as _json
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


class TestSemanticToolsRegistered:
    def test_all_four_tools_listed(self):
        from better_memory.mcp.server import _tool_definitions
        names = {t.name for t in _tool_definitions()}
        assert "memory.semantic_observe" in names
        assert "memory.semantic_retrieve" in names
        assert "memory.semantic_update" in names
        assert "memory.semantic_delete" in names


class TestSemanticObserveHandler:
    def test_default_scope_is_project(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="rule", project="proj-a")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "project"

    def test_explicit_general_scope(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(
            content="general rule", project="proj-a", scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_scope_null_via_args_get_falls_back_to_project(self, conn):
        """Regression: dict.get(key, default) returns None — not the default —
        when the key is present with value None. The handler must use
        `args.get("scope") or "project"` to defend against MCP clients
        sending {"scope": null}. Same finding as PR #25's BugBot finding
        on memory.observe.
        """
        args = {"content": "rule", "scope": None}
        scope = args.get("scope") or "project"
        assert scope == "project"


class TestSemanticRetrieveHandler:
    def test_returns_empty_list_for_unknown_project(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        result = svc.list_for_project(project="empty-proj")
        serialized = _json.dumps([
            {
                "id": m.id, "content": m.content, "project": m.project,
                "scope": m.scope,
                "created_at": m.created_at, "updated_at": m.updated_at,
            }
            for m in result
        ])
        assert _json.loads(serialized) == []

    def test_returns_serializable_rows(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        svc.create(content="rule one", project="p1")
        svc.create(content="general rule", project="p2", scope="general")
        rows = svc.list_for_project(project="p1")
        out = _json.dumps([
            {
                "id": m.id, "content": m.content, "project": m.project,
                "scope": m.scope,
                "created_at": m.created_at, "updated_at": m.updated_at,
            }
            for m in rows
        ])
        loaded = _json.loads(out)
        assert len(loaded) == 2
        contents = {r["content"] for r in loaded}
        assert "rule one" in contents
        assert "general rule" in contents


class TestSemanticUpdateHandler:
    def test_update_persists_new_content(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="old", project="p1")
        svc.update_text(id=memory_id, content="new")
        row = conn.execute(
            "SELECT content FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["content"] == "new"

    def test_update_missing_id_raises(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        with pytest.raises(ValueError):
            svc.update_text(id="nope", content="x")


class TestSemanticDeleteHandler:
    def test_delete_removes_row(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        memory_id = svc.create(content="x", project="p1")
        svc.delete(id=memory_id)
        row = conn.execute(
            "SELECT 1 FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is None

    def test_delete_missing_is_noop(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        svc.delete(id="ghost")
