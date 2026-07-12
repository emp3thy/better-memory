"""Round-trip tests for the agentcore.json persistence layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    AgentCoreConfigError,
    MemoryRecord,
    load_agentcore_config,
    save_agentcore_config,
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


def _assert_remediation(msg: str) -> None:
    """Every AgentCoreConfigError must carry actionable remediation text."""
    assert "agentcore init" in msg
    assert "--force" in msg
    assert "re-linked by hand-editing" in msg


def test_load_raises_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AgentCoreConfigError, match="parse") as excinfo:
        load_agentcore_config(tmp_path)
    _assert_remediation(str(excinfo.value))
    assert str(tmp_path / "agentcore.json") in str(excinfo.value)


def test_load_raises_on_non_object_json(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(AgentCoreConfigError, match="not a JSON object") as excinfo:
        load_agentcore_config(tmp_path)
    _assert_remediation(str(excinfo.value))


def test_load_raises_on_unsupported_schema_version(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 999, "region": "eu-west-2"}),
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="schema_version") as excinfo:
        load_agentcore_config(tmp_path)
    msg = str(excinfo.value)
    _assert_remediation(msg)
    assert "unsupported schema_version=999" in msg
    assert "expected 1" in msg
    # Forward-compat hint: an unknown schema likely came from a newer release.
    assert "newer better-memory" in msg


def test_load_raises_on_missing_required_field(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({"schema_version": 1, "region": "eu-west-2"}),  # semantic + episodic missing
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="semantic") as excinfo:
        load_agentcore_config(tmp_path)
    _assert_remediation(str(excinfo.value))


def test_load_raises_on_malformed_memory_block(tmp_path: Path) -> None:
    (tmp_path / "agentcore.json").write_text(
        json.dumps({
            "schema_version": 1,
            "region": "eu-west-2",
            "semantic": {"memory_id": "only-this"},
            "episodic": {"memory_id": "only-this"},
        }),
        encoding="utf-8",
    )
    with pytest.raises(AgentCoreConfigError, match="malformed") as excinfo:
        load_agentcore_config(tmp_path)
    _assert_remediation(str(excinfo.value))
