"""Tests for build_backend dispatch on config.storage_backend."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import sqlite_vec

from better_memory.storage import SqliteBackend, StorageBackend
from better_memory.storage.factory import build_backend


def _config(**overrides):
    """Build a Config-like object with only the storage-backend fields the
    factory reads. Other fields aren't touched by build_backend."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeConfig:
        storage_backend: str = "sqlite"

    return FakeConfig(**overrides)


def _write_agentcore_json(home: Path, region: str = "eu-west-2") -> None:
    """Write a schema-valid agentcore.json into ``home``."""
    (home / "agentcore.json").write_text(
        json.dumps({
            "schema_version": 1,
            "region": region,
            "semantic": {
                "memory_id": "mem-sem-abc1234567",
                "memory_arn": f"arn:aws:bedrock-agentcore:{region}:123:memory/mem-sem-abc1234567",
                "memory_name": "better-memory-semantic",
                "strategy_id": "userPreference-zXy1234567",
                "strategy_name": "userPreference",
                "event_expiry_duration_days": 365,
            },
            "episodic": {
                "memory_id": "mem-epi-def4567890",
                "memory_arn": f"arn:aws:bedrock-agentcore:{region}:123:memory/mem-epi-def4567890",
                "memory_name": "better-memory-episodic",
                "strategy_id": "episodicReflections-qPr9876543",
                "strategy_name": "episodicReflections",
                "event_expiry_duration_days": 90,
            },
        }),
        encoding="utf-8",
    )


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
            session_id="s",
            project="p",
        )


def test_build_backend_returns_agentcore_when_config_loaded(tmp_path, monkeypatch) -> None:
    """With agentcore.json present, factory returns AgentCoreBackend."""
    _write_agentcore_json(tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))

    cfg = _config(storage_backend="agentcore")

    from better_memory.storage.agentcore import AgentCoreBackend
    backend = build_backend(
        config=cfg,
        memory_conn=None,  # not used in agentcore mode
        session_id="s",
        project="p",
    )
    assert isinstance(backend, AgentCoreBackend)


def test_build_backend_agentcore_raises_when_config_missing(tmp_path, monkeypatch) -> None:
    """Without agentcore.json, factory raises — operator must run `agentcore init`."""
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    cfg = _config(storage_backend="agentcore")
    with pytest.raises(FileNotFoundError, match="agentcore.json"):
        build_backend(
            config=cfg,
            memory_conn=None,
            session_id="s",
            project="p",
        )


def test_build_backend_agentcore_clients_signed_with_json_region(
    tmp_path, monkeypatch
) -> None:
    """Both boto3 clients are configured with agentcore.json's region.

    Region is single-sourced from agentcore.json (the split-brain fix):
    the factory must read ``ac_cfg.region``, never an env var or a Config
    field. Uses a non-default region so a revert to any default cannot pass.
    """
    _write_agentcore_json(tmp_path, region="us-east-1")
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))

    import boto3

    captured: list[tuple[str, Any]] = []

    def _fake_client(service_name: str, config: Any = None) -> Any:
        captured.append((service_name, config))
        return MagicMock()

    monkeypatch.setattr(boto3, "client", _fake_client)

    from better_memory.storage.agentcore import AgentCoreBackend
    backend = build_backend(
        config=_config(storage_backend="agentcore"),
        memory_conn=None,
        session_id="s",
        project="p",
    )
    assert isinstance(backend, AgentCoreBackend)
    assert [service for service, _ in captured] == [
        "bedrock-agentcore",
        "bedrock-agentcore-control",
    ]
    for service, boto_config in captured:
        assert boto_config is not None, service
        assert boto_config.region_name == "us-east-1", service


def test_build_backend_agentcore_missing_boto3_raises_install_hint(
    tmp_path, monkeypatch
) -> None:
    """Missing boto3 surfaces a ModuleNotFoundError with the extras install
    hint, chained from the original ImportError (class + chain preserved so
    existing except ImportError / traceback expectations still hold)."""
    _write_agentcore_json(tmp_path)
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path))
    # None in sys.modules makes `import boto3` raise ImportError.
    monkeypatch.setitem(sys.modules, "boto3", cast(ModuleType, None))

    with pytest.raises(ModuleNotFoundError) as excinfo:
        build_backend(
            config=_config(storage_backend="agentcore"),
            memory_conn=None,
            session_id="s",
            project="p",
        )
    msg = str(excinfo.value)
    assert "better-memory[agentcore]" in msg
    assert "pip install" in msg
    assert isinstance(excinfo.value.__cause__, ImportError)
