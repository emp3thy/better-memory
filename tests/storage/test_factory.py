"""Tests for build_backend dispatch on config.storage_backend."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
import sqlite_vec

from better_memory.storage import StorageBackend, SqliteBackend
from better_memory.storage.factory import build_backend


def _config(**overrides):
    """Build a Config-like object with only the storage-backend fields the
    factory reads. Other fields aren't touched by build_backend."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeConfig:
        storage_backend: str = "sqlite"
        agentcore_region: str = "eu-west-2"
        agentcore_semantic_memory_id: str | None = None
        agentcore_episodic_memory_id: str | None = None

    return FakeConfig(**overrides)


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    """An in-memory sqlite connection with sqlite-vec loaded and migrations applied."""
    from better_memory.db.schema import apply_migrations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Migrations create vec0 virtual tables, so the extension must be loaded first.
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    return conn


def test_build_backend_returns_sqlite_for_sqlite_config(memory_conn) -> None:
    cfg = _config()
    backend = build_backend(
        config=cfg,
        memory_conn=memory_conn,
        embedder=MagicMock(),
        session_id="s",
        project="p",
    )
    assert isinstance(backend, SqliteBackend)
    assert isinstance(backend, StorageBackend)


def test_build_backend_raises_for_unknown(memory_conn) -> None:
    cfg = _config(storage_backend="bogus")
    with pytest.raises(ValueError, match="unknown storage_backend"):
        build_backend(
            config=cfg,
            memory_conn=memory_conn,
            embedder=MagicMock(),
            session_id="s",
            project="p",
        )


def test_build_backend_returns_agentcore_when_config_loaded(tmp_path, monkeypatch) -> None:
    """With agentcore.json present + valid memory IDs, factory returns AgentCoreBackend."""
    import json
    home = tmp_path
    (home / "agentcore.json").write_text(
        json.dumps({
            "schema_version": 1,
            "region": "eu-west-2",
            "semantic": {
                "memory_id": "mem-sem-abc1234567",
                "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-sem-abc1234567",
                "memory_name": "better-memory-semantic",
                "strategy_id": "userPreference-zXy1234567",
                "strategy_name": "userPreference",
                "event_expiry_duration_days": 365,
            },
            "episodic": {
                "memory_id": "mem-epi-def4567890",
                "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-epi-def4567890",
                "memory_name": "better-memory-episodic",
                "strategy_id": "episodicReflections-qPr9876543",
                "strategy_name": "episodicReflections",
                "event_expiry_duration_days": 90,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

    cfg = _config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-def4567890",
    )

    from better_memory.storage.agentcore import AgentCoreBackend
    backend = build_backend(
        config=cfg,
        memory_conn=None,  # not used in agentcore mode
        embedder=None,
        session_id="s",
        project="p",
    )
    assert isinstance(backend, AgentCoreBackend)


def test_build_backend_agentcore_raises_when_config_missing(tmp_path, monkeypatch) -> None:
    """Without agentcore.json, factory raises — operator must run `agentcore init`."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    cfg = _config(
        storage_backend="agentcore",
        agentcore_semantic_memory_id="mem-sem-abc1234567",
        agentcore_episodic_memory_id="mem-epi-def4567890",
    )
    with pytest.raises(FileNotFoundError, match="agentcore.json"):
        build_backend(
            config=cfg,
            memory_conn=None,
            embedder=None,
            session_id="s",
            project="p",
        )
