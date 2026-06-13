"""ServiceContainer bundles every long-lived service the MCP dispatcher needs."""
from __future__ import annotations

import sqlite3
from dataclasses import is_dataclass
from unittest.mock import MagicMock

from better_memory.mcp.container import ServiceContainer


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
    container = ServiceContainer(
        config=MagicMock(),
        memory_conn=mock_conn,
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
    assert container.memory_conn is mock_conn
