"""Unit tests for AgentCoreBackend. All boto3 calls are mocked — these
tests verify wire shape (call args + return mapping), NOT live AWS
behavior. Integration tests against real AWS land in Plan 3."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from better_memory.storage import StorageBackend
from better_memory.storage.agentcore import AgentCoreBackend
from better_memory.storage.agentcore_persistence import (
    AgentCoreConfig,
    MemoryRecord,
)


@pytest.fixture
def ac_config() -> AgentCoreConfig:
    return AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        semantic=MemoryRecord(
            memory_id="mem-sem-abc1234567",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-sem-abc1234567",
            memory_name="better-memory-semantic",
            strategy_id="userPreference-zXy1234567",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id="mem-epi-def4567890",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-epi-def4567890",
            memory_name="better-memory-episodic",
            strategy_id="episodicReflections-qPr9876543",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
    )


@pytest.fixture
def mock_data_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-data")


@pytest.fixture
def mock_control_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-control")


@pytest.fixture
def backend(ac_config, mock_data_client, mock_control_client) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
    )


def test_agentcore_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_supports_synthesis_is_false(backend) -> None:
    """Synthesis runs inside AgentCore; the MCP synthesize_next_* tools
    are not registered in agentcore mode."""
    assert backend.supports_synthesis is False


def test_supports_episodes_is_false(backend) -> None:
    """AgentCore manages event grouping via sessionId; episodes are not a
    first-class concept the UI exposes."""
    assert backend.supports_episodes is False


def test_synthesize_next_get_context_is_noop(backend) -> None:
    """No-op returns None — matches sqlite mode's 'no pending episode' signal.
    MCP gates the tool out via supports_synthesis=False; this method exists
    only so direct callers don't crash."""
    assert backend.synthesize_next_get_context(project="testproj") is None


def test_synthesize_next_apply_is_noop(backend) -> None:
    """No-op returns empty dict — matches the 'no work done' signal."""
    result = backend.synthesize_next_apply(
        episode_id="ep-x", response={}, project="testproj"
    )
    assert result == {"applied": 0, "skipped": 0}


def test_open_background_episode_returns_synthetic_id(backend) -> None:
    """No-op in agentcore mode; returns a sentinel id so the existing MCP
    tool path doesn't break."""
    result = backend.open_background_episode(
        session_id="test-session", project="testproj"
    )
    assert isinstance(result, str) and result


def test_start_foreground_episode_returns_synthetic_id(backend) -> None:
    result = backend.start_foreground_episode(
        session_id="test-session",
        project="testproj",
        goal="ship plan 2",
    )
    assert isinstance(result, str) and result


def test_close_active_episode_returns_empty_string(backend) -> None:
    """No-op close; returns empty string. (The MCP handler converts this
    to a no-content tool result.)"""
    result = backend.close_active_episode(
        session_id="test-session",
        outcome="success",
        close_reason="goal_complete",
    )
    assert result == ""


def test_close_episode_by_id_returns_empty_string(backend) -> None:
    result = backend.close_episode_by_id(
        episode_id="ep-x",
        outcome="success",
        close_reason="goal_complete",
    )
    assert result == ""


def test_list_episodes_returns_empty_list(backend) -> None:
    """No episodes in agentcore mode; UI hides the tab via supports_episodes."""
    assert backend.list_episodes() == []
