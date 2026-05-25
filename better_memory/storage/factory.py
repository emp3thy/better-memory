"""Backend factory — picks SqliteBackend or AgentCoreBackend based on config."""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol

from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


class _ConfigLike(Protocol):
    """Structural type — the factory only reads storage_backend.

    Declared as a read-only property so frozen dataclasses (the real Config and
    test FakeConfigs) satisfy the Protocol — pyright treats a Protocol class
    attribute as read+write, which conflicts with frozen dataclasses' read-only
    fields.
    """

    @property
    def storage_backend(self) -> str: ...


def build_backend(
    *,
    config: _ConfigLike,
    memory_conn: sqlite3.Connection,
    embedder: Any = None,
    session_id: str | None,
    project: str,
) -> StorageBackend:
    """Construct the StorageBackend implementation appropriate for the config."""
    if config.storage_backend == "sqlite":
        return SqliteBackend(
            memory_conn=memory_conn,
            embedder=embedder,
            session_id=session_id,
            project=project,
        )
    if config.storage_backend == "agentcore":
        raise NotImplementedError(
            "AgentCoreBackend is delivered in Plan 2. "
            "Until then, set BETTER_MEMORY_STORAGE_BACKEND=sqlite."
        )
    raise ValueError(f"unknown storage_backend={config.storage_backend!r}")
