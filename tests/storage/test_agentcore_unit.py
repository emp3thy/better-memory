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


def test_retrieve_returns_dict_with_polarity_buckets(backend, mock_data_client) -> None:
    """retrieve returns dict[str, list[dict]] matching ReflectionSynthesisService."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.retrieve(project="testproj")
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"do", "dont", "neutral"}
    for bucket in ("do", "dont", "neutral"):
        assert isinstance(result[bucket], list)


def test_retrieve_fires_one_list_call_per_polarity(backend, mock_data_client) -> None:
    """Three list_memory_records calls (no semantic search): one per polarity."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.retrieve(project="testproj")
    assert mock_data_client.list_memory_records.call_count == 3

    polarities_filtered = []
    for call in mock_data_client.list_memory_records.call_args_list:
        for f in call.kwargs["metadataFilters"]:
            if f["left"]["metadataKey"] == "polarity":
                polarities_filtered.append(f["right"]["metadataValue"]["stringValue"])
    assert set(polarities_filtered) == {"do", "dont", "neutral"}


def test_retrieve_with_polarity_kwarg_fetches_only_that_bucket(backend, mock_data_client) -> None:
    """polarity='do' -> only the do bucket gets fetched; others empty."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.retrieve(project="testproj", polarity="do")
    assert mock_data_client.list_memory_records.call_count == 1
    assert result["dont"] == []
    assert result["neutral"] == []


def test_retrieve_parses_reflection_json_content(backend, mock_data_client) -> None:
    """content.text is a JSON blob with title/use_cases/hints/confidence — map
    to the sqlite-mode reflection dict shape."""
    import json
    record_json = json.dumps({
        "title": "Test reflection title",
        "use_cases": "Applies when X",
        "hints": "First hint.\n- Second hint.\n- Third hint.",
        "confidence": "0.85",
    })
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-1",
                "content": {"text": record_json},
                "memoryStrategyId": "episodicReflections-qPr9876543",
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "metadata": {
                    "polarity": {"stringValue": "do"},
                    "useful_count": {"numberValue": 3},
                    "missed_count": {"numberValue": 0},
                    "ignored_count": {"numberValue": 1},
                    "times_misled": {"numberValue": 0},
                    "overlooked_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }
        ]
    }
    result = backend.retrieve(project="testproj")
    do_bucket = result["do"]
    assert len(do_bucket) == 1
    refl = do_bucket[0]
    # Match the sqlite-mode reflection dict shape
    assert refl["id"] == "rec-1"
    assert refl["title"] == "Test reflection title"
    assert refl["use_cases"] == "Applies when X"
    assert refl["hints"] == ["First hint.", "Second hint.", "Third hint."]
    assert refl["confidence"] == 0.85  # float
    assert refl["useful_count"] == 3


def test_retrieve_ranks_by_useful_plus_3x_overlooked(backend, mock_data_client) -> None:
    """Ranking matches sqlite: useful_count + 3*times_overlooked DESC,
    confidence DESC, updated_at DESC."""
    import json
    def make_record(rec_id: str, useful: int, overlooked: int) -> dict:
        return {
            "memoryRecordId": rec_id,
            "content": {"text": json.dumps({
                "title": rec_id, "use_cases": "u", "hints": "h", "confidence": "0.9",
            })},
            "memoryStrategyId": "x",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "metadata": {
                "polarity": {"stringValue": "do"},
                "useful_count": {"numberValue": useful},
                "missed_count": {"numberValue": 0},
                "ignored_count": {"numberValue": 0},
                "times_misled": {"numberValue": 0},
                "overlooked_count": {"numberValue": overlooked},
                "status": {"stringValue": "active"},
            },
        }

    def stub(**kwargs):
        for f in kwargs.get("metadataFilters", []):
            if f["left"]["metadataKey"] == "polarity":
                pol = f["right"]["metadataValue"]["stringValue"]
                if pol == "do":
                    # high-rank: useful=10 -> score 10
                    # mid-rank:  useful=2, overlooked=3 -> score 2 + 9 = 11
                    # low-rank:  useful=0, overlooked=0 -> score 0
                    return {"memoryRecordSummaries": [
                        make_record("low", useful=0, overlooked=0),
                        make_record("high-via-useful", useful=10, overlooked=0),
                        make_record("highest-via-overlooked", useful=2, overlooked=3),
                    ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    do_titles = [r["title"] for r in result["do"]]
    # Score = useful_count + 3*times_overlooked
    # highest-via-overlooked: 2 + 9 = 11
    # high-via-useful:        10 + 0 = 10
    # low:                    0 + 0 = 0
    assert do_titles == ["highest-via-overlooked", "high-via-useful", "low"]


def _make_record_response(rec_id: str, **counters) -> dict:
    """Helper: build a MemoryRecord response with the standard metadata."""
    base = {
        "useful_count": 0, "missed_count": 0, "ignored_count": 0,
        "times_misled": 0, "overlooked_count": 0,
    }
    base.update(counters)
    return {
        "memoryRecord": {
            "memoryRecordId": rec_id,
            "content": {"text": "{}"},
            "memoryStrategyId": "episodicReflections-qPr9876543",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "metadata": {
                **{k: {"numberValue": v} for k, v in base.items()},
                "status": {"stringValue": "active"},
                "polarity": {"stringValue": "do"},
            },
        }
    }


def test_record_use_success_bumps_useful_count(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "rec-x", useful_count=2,
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.record_use("rec-x", outcome="success")
    call = mock_data_client.batch_update_memory_records.call_args.kwargs
    rec = call["records"][0]
    assert rec["memoryRecordId"] == "rec-x"
    assert rec["metadata"]["useful_count"]["numberValue"] == 3
    assert rec["metadata"]["missed_count"]["numberValue"] == 0
    # last_credited_at refreshed
    assert "last_credited_at" in rec["metadata"]


def test_record_use_failure_bumps_missed_count(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "rec-y", missed_count=4,
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-y", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.record_use("rec-y", outcome="failure")
    call = mock_data_client.batch_update_memory_records.call_args.kwargs
    rec = call["records"][0]
    assert rec["metadata"]["missed_count"]["numberValue"] == 5
    assert rec["metadata"]["useful_count"]["numberValue"] == 0


def test_record_use_none_outcome_is_noop(backend, mock_data_client) -> None:
    """record_use(id) without outcome should not touch the record (no
    classification, no counter change)."""
    backend.record_use("rec-z", outcome=None)
    mock_data_client.get_memory_record.assert_not_called()
    mock_data_client.batch_update_memory_records.assert_not_called()


def test_record_use_propagates_failed_records(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-fail")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{
            "memoryRecordId": "rec-fail",
            "status": "FAILED",
            "errorCode": 500,
            "errorMessage": "internal error",
        }],
    }
    with pytest.raises(RuntimeError, match="rec-fail"):
        backend.record_use("rec-fail", outcome="success")


def test_record_use_retries_on_transient_404(
    backend, mock_data_client, monkeypatch
) -> None:
    """batch_update_memory_records issued immediately after
    batch_create_memory_records can raise ResourceNotFoundException
    transiently. The _retry_on_transient_404 wrapper must retry once
    and succeed on the second attempt."""
    from better_memory.storage import agentcore as ac_module

    class _FakeClientError(Exception):
        def __init__(self) -> None:
            super().__init__("rec-x not found")
            self.response = {"Error": {"Code": "ResourceNotFoundException"}}

    monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)
    monkeypatch.setattr(ac_module.time, "sleep", lambda _s: None)

    mock_data_client.get_memory_record.return_value = _make_record_response(
        "rec-x", useful_count=0,
    )
    success_resp = {
        "successfulRecords": [{"memoryRecordId": "rec-x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    mock_data_client.batch_update_memory_records.side_effect = [
        _FakeClientError(),
        success_resp,
    ]

    backend.record_use("rec-x", outcome="success")

    assert mock_data_client.batch_update_memory_records.call_count == 2


_RATING_TO_COUNTER = {
    "cited": "useful_count",
    "shaped": "useful_count",
    "ignored": "ignored_count",
    "misled": "times_misled",
    "overlooked": "overlooked_count",
}


def test_list_session_exposures_returns_empty_envelope(backend) -> None:
    result = backend.list_session_exposures(session_id="test-session-xyz")
    assert result == {"session_id": "test-session-xyz", "exposures": []}


@pytest.mark.parametrize(
    "classification,counter_key",
    list(_RATING_TO_COUNTER.items()),
)
def test_credit_one_bumps_correct_counter(
    backend, mock_data_client, classification, counter_key
) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-c")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-c", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.credit_one(
        session_id="test-session-xyz",
        kind="reflection",
        id="rec-c",
        classification=classification,
    )
    assert result["applied"] == "rec-c"
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["metadata"][counter_key]["numberValue"] == 1


def test_credit_one_rejects_unknown_classification(backend) -> None:
    with pytest.raises(ValueError, match="classification"):
        backend.credit_one(
            session_id="s",
            kind="reflection",
            id="rec-c",
            classification="bogus",
        )


def test_credit_one_routes_semantic_kind_to_semantic_memory(backend, mock_data_client) -> None:
    """kind='semantic' must target the semantic memory, not episodic."""
    semantic_record = {
        "memoryRecord": {
            "memoryRecordId": "sm-rec",
            "content": {"text": "x"},
            "memoryStrategyId": "userPreference-zXy1234567",
            "namespaces": ["projects/testproj/semantic/"],
            "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
            "metadata": {
                "status": {"stringValue": "active"},
                "useful_count": {"numberValue": 0},
                "missed_count": {"numberValue": 0},
                "ignored_count": {"numberValue": 0},
                "times_misled": {"numberValue": 0},
                "overlooked_count": {"numberValue": 0},
            },
        }
    }
    mock_data_client.get_memory_record.return_value = semantic_record
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-rec", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.credit_one(
        session_id="test-session",
        kind="semantic",
        id="sm-rec",
        classification="cited",
    )
    assert result["applied"] == "sm-rec"

    # Verify both calls targeted SEMANTIC memory, not episodic
    get_call = mock_data_client.get_memory_record.call_args.kwargs
    assert get_call["memoryId"] == "mem-sem-abc1234567"
    update_call = mock_data_client.batch_update_memory_records.call_args.kwargs
    assert update_call["memoryId"] == "mem-sem-abc1234567"


def test_apply_session_ratings_credits_each_rating(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.side_effect = [
        _make_record_response("rec-1"),
        _make_record_response("rec-2"),
    ]
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.apply_session_ratings(
        session_id="test-session-xyz",
        ratings=[
            {"kind": "reflection", "id": "rec-1", "class": "cited"},
            {"kind": "reflection", "id": "rec-2", "class": "overlooked"},
        ],
    )
    assert mock_data_client.batch_update_memory_records.call_count == 2
    assert result["applied"] == 2
    assert result["failed"] == 0


def test_apply_session_ratings_empty_returns_zero_summary(backend) -> None:
    result = backend.apply_session_ratings(session_id="x", ratings=[])
    assert result == {"applied": 0, "failed": 0}


def test_apply_session_ratings_skips_malformed_entries(backend, mock_data_client) -> None:
    """Malformed entries (missing key, unknown class) increment `failed` instead of crashing the loop."""
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-ok")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-ok", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.apply_session_ratings(
        session_id="test-session-xyz",
        ratings=[
            {"kind": "reflection", "id": "rec-ok", "class": "cited"},  # OK
            {"kind": "reflection", "id": "rec-missing-class"},  # KeyError
            {"kind": "reflection", "id": "rec-bad", "class": "bogus"},  # ValueError
        ],
    )
    assert result["applied"] == 1
    assert result["failed"] == 2


def test_promote_reflection_moves_to_general_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-p")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-p", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.promote_reflection(reflection_id="rec-p")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/reflections/"]
    assert rec["metadata"]["status"]["stringValue"] == "promoted"


def test_retire_reflection_moves_to_retired_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-r")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "rec-r", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.retire_reflection(reflection_id="rec-r")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["projects/testproj/retired/"]
    assert rec["metadata"]["status"]["stringValue"] == "retired"


def test_promote_reflection_raises_when_batch_fails(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response("rec-fail")
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{"memoryRecordId": "rec-fail", "status": "FAILED", "errorMessage": "boom"}],
    }
    with pytest.raises(RuntimeError, match="rec-fail"):
        backend.promote_reflection(reflection_id="rec-fail")


# ===== Task 11: semantic CRUD =====
import hashlib


def test_semantic_observe_calls_batch_create_against_semantic_memory(backend, mock_data_client) -> None:
    mock_data_client.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED", "requestIdentifier": "any"}],
        "failedRecords": [],
    }
    sm_id = backend.semantic_observe(content="prefer uv over pip")
    assert sm_id == "sm-1"
    call = mock_data_client.batch_create_memory_records.call_args.kwargs
    assert call["memoryId"] == "mem-sem-abc1234567"
    rec = call["records"][0]
    assert rec["memoryStrategyId"] == "userPreference-zXy1234567"
    assert rec["namespaces"] == ["projects/testproj/semantic/"]
    assert rec["content"]["text"] == "prefer uv over pip"
    assert len(rec["requestIdentifier"]) <= 80
    # Initial metadata
    assert rec["metadata"]["status"]["stringValue"] == "active"
    assert rec["metadata"]["useful_count"]["numberValue"] == 0
    assert rec["metadata"]["overlooked_count"]["numberValue"] == 0


def test_semantic_observe_general_scope_uses_general_namespace(backend, mock_data_client) -> None:
    mock_data_client.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-2", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_observe(content="x", scope="general")
    rec = mock_data_client.batch_create_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/semantic/"]


def test_semantic_list_with_search_uses_retrieve_memory_records(backend, mock_data_client) -> None:
    mock_data_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-1",
                "content": {"text": "prefer uv"},
                "memoryStrategyId": "userPreference-zXy1234567",
                "namespaces": ["projects/testproj/semantic/"],
                "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
                "metadata": {"status": {"stringValue": "active"}},
            }
        ]
    }
    result = backend.semantic_list(search="uv")
    assert len(result) == 1
    assert result[0]["id"] == "sm-1"
    assert result[0]["content"] == "prefer uv"


def test_semantic_list_without_search_uses_list_memory_records(backend, mock_data_client) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list()
    mock_data_client.list_memory_records.assert_called_once()
    mock_data_client.retrieve_memory_records.assert_not_called()


def test_semantic_update_text_calls_batch_update(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = {
        "memoryRecord": {
            "memoryRecordId": "sm-1",
            "content": {"text": "original"},
            "memoryStrategyId": "userPreference-zXy1234567",
            "namespaces": ["projects/testproj/semantic/"],
            "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
            "metadata": {"status": {"stringValue": "active"}},
        }
    }
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_update_text(id="sm-1", content="updated")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["content"]["text"] == "updated"


def test_semantic_set_scope_swaps_namespace(backend, mock_data_client) -> None:
    mock_data_client.get_memory_record.return_value = {
        "memoryRecord": {
            "memoryRecordId": "sm-1",
            "content": {"text": "x"},
            "memoryStrategyId": "userPreference-zXy1234567",
            "namespaces": ["projects/testproj/semantic/"],
            "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
            "metadata": {"status": {"stringValue": "active"}},
        }
    }
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_set_scope(id="sm-1", scope="general")
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["namespaces"] == ["general/semantic/"]


def test_semantic_delete_calls_batch_delete(backend, mock_data_client) -> None:
    mock_data_client.batch_delete_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "sm-x", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.semantic_delete(id="sm-x")
    call = mock_data_client.batch_delete_memory_records.call_args.kwargs
    assert call["memoryId"] == "mem-sem-abc1234567"
    assert call["records"] == [{"memoryRecordId": "sm-x"}]


def test_session_bootstrap_fires_4_parallel_list_calls(backend, mock_data_client) -> None:
    """One per polarity (do/dont/neutral) against episodic + one against
    semantic — all 4 dispatched via asyncio.gather + run_in_executor.

    Uses list_memory_records (not retrieve_memory_records) because
    bootstrap is recency / metadata-only — no semantic search query."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.session_bootstrap(session_id="test-session", project="testproj")
    # 4 calls total — 3 reflection (episodic) + 1 semantic
    assert mock_data_client.list_memory_records.call_count == 4

    targets = []
    for call in mock_data_client.list_memory_records.call_args_list:
        targets.append((call.kwargs["memoryId"], call.kwargs["namespace"]))

    assert ("mem-epi-def4567890", "projects/testproj/reflections/") in targets
    assert ("mem-sem-abc1234567", "projects/testproj/semantic/") in targets


def test_session_bootstrap_returns_envelope_matching_sqlite_shape(backend, mock_data_client) -> None:
    """Envelope must match the BootstrapResult shape the MCP handler at
    server.py:1398-1411 unwraps. Keys: additional_context, project, source,
    episode_id, episode_action, semantic_count, reflections_counts. In
    agentcore mode there is no real episode — episode_id = the session_id
    placeholder and episode_action = 'opened'."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.session_bootstrap(session_id="s", project="testproj", source="bootstrap")

    assert result["project"] == "testproj"
    assert result["source"] == "bootstrap"
    assert result["additional_context"]  # non-empty string
    assert result["episode_id"] == "s"
    assert result["episode_action"] == "opened"
    assert result["semantic_count"] == 0
    assert result["reflections_counts"] == {"do": 0, "dont": 0, "neutral": 0}
