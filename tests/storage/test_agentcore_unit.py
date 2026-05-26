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


from datetime import datetime, UTC


@pytest.mark.asyncio
async def test_observe_calls_create_event_with_correct_kwargs(backend, mock_data_client) -> None:
    """observe builds a CreateEvent against the EPISODIC memory with
    actorId=project, sessionId=backend session, and a conversational
    payload carrying the observation content."""
    mock_data_client.create_event.return_value = {
        "event": {"eventId": "evt-abc123", "memoryId": "mem-epi-def4567890"}
    }

    result = await backend.observe(
        content="Test observation.",
        outcome="success",
        component="parser",
        theme="bug",
    )

    assert result == "evt-abc123"
    mock_data_client.create_event.assert_called_once()
    call_kwargs = mock_data_client.create_event.call_args.kwargs

    assert call_kwargs["memoryId"] == "mem-epi-def4567890"
    assert call_kwargs["actorId"] == "testproj"
    assert call_kwargs["sessionId"] == "test-session-xyz"
    assert isinstance(call_kwargs["eventTimestamp"], datetime)
    assert call_kwargs["eventTimestamp"].tzinfo is UTC

    # Payload shape: list[{conversational: {role, content: {text}}}]
    payload = call_kwargs["payload"]
    assert isinstance(payload, list) and len(payload) == 1
    block = payload[0]["conversational"]
    assert block["role"] == "USER"  # observations are model-side inputs
    assert block["content"]["text"] == "Test observation."

    # Metadata: outcome / component / theme as stringValue only.
    metadata = call_kwargs["metadata"]
    assert metadata["outcome"]["stringValue"] == "success"
    assert metadata["component"]["stringValue"] == "parser"
    assert metadata["theme"]["stringValue"] == "bug"


@pytest.mark.asyncio
async def test_observe_resolves_project_when_kwarg_is_none(backend, mock_data_client) -> None:
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", project=None)
    assert mock_data_client.create_event.call_args.kwargs["actorId"] == "testproj"


@pytest.mark.asyncio
async def test_observe_general_project_uses_general_actor(backend, mock_data_client) -> None:
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", project="general")
    assert mock_data_client.create_event.call_args.kwargs["actorId"] == "general"


@pytest.mark.asyncio
async def test_observe_drops_none_metadata_keys(backend, mock_data_client) -> None:
    """Don't send `{"key": {"stringValue": None}}` — None-valued metadata
    keys are omitted entirely so the payload validates."""
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x", component=None, theme="bug")
    metadata = mock_data_client.create_event.call_args.kwargs["metadata"]
    assert "component" not in metadata
    assert metadata["theme"]["stringValue"] == "bug"


@pytest.mark.asyncio
async def test_observe_raises_value_error_when_session_id_is_none(ac_config, mock_data_client, mock_control_client) -> None:
    """CreateEvent on the episodic memory requires sessionId (per the
    output schema and our usage pattern). A backend with session_id=None
    cannot fire events — raise so the operator sees the misconfiguration."""
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
    )
    with pytest.raises(ValueError, match="session_id"):
        await backend.observe(content="x")


@pytest.mark.asyncio
async def test_list_observations_returns_current_session_events(backend, mock_data_client) -> None:
    mock_data_client.list_events.return_value = {
        "events": [
            {
                "eventId": "evt-1",
                "memoryId": "mem-epi-def4567890",
                "actorId": "testproj",
                "sessionId": "test-session-xyz",
                "eventTimestamp": datetime(2026, 5, 25, 12, tzinfo=UTC),
                "payload": [
                    {"conversational": {"role": "USER", "content": {"text": "obs one"}}}
                ],
                "metadata": {
                    "outcome": {"stringValue": "success"},
                    "theme": {"stringValue": "test"},
                },
            },
            {
                "eventId": "evt-2",
                "memoryId": "mem-epi-def4567890",
                "actorId": "testproj",
                "sessionId": "test-session-xyz",
                "eventTimestamp": datetime(2026, 5, 25, 12, 30, tzinfo=UTC),
                "payload": [
                    {"conversational": {"role": "USER", "content": {"text": "obs two"}}}
                ],
                "metadata": {"outcome": {"stringValue": "failure"}},
            },
        ],
    }

    result = await backend.list_observations(limit=10)
    assert isinstance(result, list) and len(result) == 2

    # Mapping: eventId -> id, content extracted from payload, metadata
    # flattened (stringValue unwrapped).
    assert result[0]["id"] == "evt-1"
    assert result[0]["content"] == "obs one"
    assert result[0]["outcome"] == "success"
    assert result[0]["theme"] == "test"

    # ListEvents call shape
    call_kwargs = mock_data_client.list_events.call_args.kwargs
    assert call_kwargs["memoryId"] == "mem-epi-def4567890"
    assert call_kwargs["actorId"] == "testproj"
    assert call_kwargs["sessionId"] == "test-session-xyz"
    assert call_kwargs["maxResults"] == 10
    assert call_kwargs["includePayloads"] is True


@pytest.mark.asyncio
async def test_list_observations_returns_empty_when_no_events(backend, mock_data_client) -> None:
    mock_data_client.list_events.return_value = {"events": []}
    assert await backend.list_observations(limit=5) == []


@pytest.mark.asyncio
async def test_list_observations_raises_when_session_id_is_none(ac_config, mock_data_client, mock_control_client) -> None:
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
    )
    with pytest.raises(ValueError, match="session_id"):
        await backend.list_observations(limit=5)
