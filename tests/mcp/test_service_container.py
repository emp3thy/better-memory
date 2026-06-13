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
