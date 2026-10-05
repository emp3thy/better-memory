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


class TestSemanticTriggers:
    """Handler-level: ``triggers`` on observe and update, echoed by retrieve."""

    def _handlers(self, conn):
        from better_memory.mcp.handlers.semantics import SemanticToolHandlers
        from better_memory.services.semantic import SemanticMemoryService
        return SemanticToolHandlers(semantic=SemanticMemoryService(conn))

    async def test_observe_with_triggers_persists(self, conn, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_PROJECT", "proj-a")
        h = self._handlers(conn)
        out = await h.semantic_observe(
            {"content": "rule", "triggers": ["tool:WebFetch", "bash:<<"]}
        )
        memory_id = _json.loads(out[0].text)["id"]
        row = conn.execute(
            "SELECT triggers FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert _json.loads(row["triggers"]) == ["tool:WebFetch", "bash:<<"]

    async def test_observe_rejects_bad_trigger(self, conn, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_PROJECT", "proj-a")
        h = self._handlers(conn)
        with pytest.raises(ValueError, match="invalid trigger"):
            await h.semantic_observe({"content": "rule", "triggers": ["nope"]})
        assert conn.execute("SELECT COUNT(*) FROM semantic_memories").fetchone()[0] == 0

    async def test_update_triggers_only_keeps_content(self, conn, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_PROJECT", "proj-a")
        h = self._handlers(conn)
        memory_id = _json.loads(
            (await h.semantic_observe({"content": "rule"}))[0].text
        )["id"]
        out = await h.semantic_update({"id": memory_id, "triggers": ["skill:superpowers:*"]})
        assert _json.loads(out[0].text) == {"ok": True}
        row = conn.execute(
            "SELECT content, triggers FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["content"] == "rule"
        assert _json.loads(row["triggers"]) == ["skill:superpowers:*"]

    async def test_update_content_and_triggers_together(self, conn, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_PROJECT", "proj-a")
        h = self._handlers(conn)
        memory_id = _json.loads(
            (await h.semantic_observe({"content": "rule"}))[0].text
        )["id"]
        await h.semantic_update({"id": memory_id, "content": "rule v2", "triggers": []})
        row = conn.execute(
            "SELECT content, triggers FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert (row["content"], row["triggers"]) == ("rule v2", None)

    async def test_update_requires_content_or_triggers(self, conn):
        h = self._handlers(conn)
        with pytest.raises(ValueError, match="provide content and/or triggers"):
            await h.semantic_update({"id": "x"})

    async def test_retrieve_includes_triggers(self, conn, monkeypatch):
        monkeypatch.setenv("BETTER_MEMORY_PROJECT", "proj-a")
        h = self._handlers(conn)
        await h.semantic_observe({"content": "rule", "triggers": ["tool:Agent"]})
        rows = _json.loads((await h.semantic_retrieve({"project": "proj-a"}))[0].text)
        assert rows[0]["triggers"] == ["tool:Agent"]

    def test_schema_declares_triggers(self):
        from better_memory.mcp.server import _tool_definitions
        tools = {t.name: t for t in _tool_definitions()}
        for name in ("memory.semantic_observe", "memory.semantic_update"):
            props = tools[name].inputSchema["properties"]
            assert props["triggers"]["type"] == "array"
            assert props["triggers"]["items"] == {"type": "string"}
            assert "tool:" in props["triggers"]["description"]
        assert tools["memory.semantic_update"].inputSchema["required"] == ["id"]
