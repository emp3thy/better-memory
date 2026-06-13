"""ServiceContainer bundles every long-lived service the MCP dispatcher needs."""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import is_dataclass
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer


def _make_container(memory_conn: sqlite3.Connection | None = None) -> ServiceContainer:
    return ServiceContainer(
        config=MagicMock(),
        memory_conn=memory_conn or MagicMock(spec=sqlite3.Connection),
        backend=MagicMock(),
        episodes=MagicMock(),
        observations=MagicMock(),
        reflections=MagicMock(),
        retention=MagicMock(),
        memory_rating=MagicMock(),
        knowledge=MagicMock(),
        spool=MagicMock(),
        semantic=MagicMock(),
        session_bootstrap=MagicMock(),
    )


def test_service_container_is_frozen_dataclass() -> None:
    fields = {
        "config", "memory_conn", "backend",
        "episodes", "observations", "reflections",
        "retention", "memory_rating", "knowledge",
        "spool", "semantic", "session_bootstrap",
    }
    assert is_dataclass(ServiceContainer)
    assert set(ServiceContainer.__dataclass_fields__) == fields


def test_service_container_holds_attributes() -> None:
    mock_conn = MagicMock(spec=sqlite3.Connection)
    container = _make_container(memory_conn=mock_conn)
    assert container.memory_conn is mock_conn


def test_service_container_rejects_attribute_reassignment() -> None:
    container = _make_container()
    with pytest.raises(dataclasses.FrozenInstanceError):
        container.memory_conn = MagicMock(spec=sqlite3.Connection)  # type: ignore[misc]


def test_build_services_constructs_each_service_exactly_once(
    monkeypatch, tmp_path,
) -> None:
    """Regression guard: SemanticMemoryService was built 4× per call,
    SessionBootstrapService 2× per call. Container must build each once."""
    from collections import Counter
    from pathlib import Path

    import better_memory.services.semantic as _sem_mod
    import better_memory.services.session_bootstrap as _sb_mod
    from better_memory.config import get_config
    from better_memory.db.connection import connect
    from better_memory.db.schema import apply_migrations
    from better_memory.mcp.server import _build_services

    counts: Counter[str] = Counter()

    def _wrap(cls: type) -> None:
        original_init = cls.__init__
        def _init(self, *a, **kw):
            counts[cls.__name__] += 1
            original_init(self, *a, **kw)
        cls.__init__ = _init  # type: ignore[method-assign]

    _wrap(_sem_mod.SemanticMemoryService)
    _wrap(_sb_mod.SessionBootstrapService)

    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    cfg = get_config()

    mem_conn = connect(cfg.memory_db)
    apply_migrations(
        mem_conn,
        migrations_dir=Path(__file__).parent.parent.parent
        / "better_memory" / "db" / "migrations",
    )
    kb_conn = connect(cfg.knowledge_db)
    apply_migrations(
        kb_conn,
        migrations_dir=Path(__file__).parent.parent.parent
        / "better_memory" / "db" / "knowledge_migrations",
    )

    container = _build_services(
        cfg, mem_conn, kb_conn, embedder=None,
        startup_project="test", startup_session_id=None,
    )
    # _build_services itself constructs each service exactly once at the
    # container level. SqliteBackend (built inside build_backend) keeps its
    # own internal SemanticMemoryService + SessionBootstrapService for
    # protocol compliance — that accounts for the second construction of
    # each. Total of 2 still kills the 4×/2× inline regressions; any
    # future drift back to per-call construction will push these well
    # above 2 and re-trip this test.
    assert counts["SemanticMemoryService"] == 2
    assert counts["SessionBootstrapService"] == 2
    assert container.semantic is not None
    assert container.session_bootstrap is not None
