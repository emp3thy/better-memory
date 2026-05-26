"""Round-trip tests for the agentcore.json persistence layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
    load_agentcore_config,
    save_agentcore_config,
    AgentCoreConfigError,
)


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    cfg = AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        semantic=MemoryRecord(
            memory_id="better-memory-semantic-abc1234567",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/better-memory-semantic-abc1234567",
            memory_name="better-memory-semantic",
            strategy_id="userPreference-zXy1234567",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id="better-memory-episodic-def4567890",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/better-memory-episodic-def4567890",
            memory_name="better-memory-episodic",
            strategy_id="episodicReflections-qPr9876543",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
    )
    save_agentcore_config(cfg, tmp_path)
    loaded = load_agentcore_config(tmp_path)
    assert loaded == cfg


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_agentcore_config(tmp_path) is None


def test_load_raises_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AgentCoreConfigError, match="parse"):
        load_agentcore_config(tmp_path)


def test_load_raises_on_unsupported_schema_version(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 999, "region": "eu-west-2"}),
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="schema_version"):
        load_agentcore_config(tmp_path)


def test_load_raises_on_missing_required_field(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 1, "region": "eu-west-2"}),  # semantic + episodic missing
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="semantic"):
        load_agentcore_config(tmp_path)
