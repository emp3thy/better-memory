"""Unit tests for AgentCoreBackend. All boto3 calls are mocked — these
tests verify wire shape (call args + return mapping), NOT live AWS
behavior. Integration tests against real AWS land in Plan 3."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
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


@pytest.fixture
def backend_without_session(
    ac_config, mock_data_client, mock_control_client
) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
    )


@pytest.mark.asyncio
async def test_observe_raises_when_session_id_unresolvable(
    backend_without_session, mock_data_client
) -> None:
    """session_id=None triggers lazy re-resolution (env var → SessionStart
    marker under $BETTER_MEMORY_HOME). With neither available observe must
    still raise — and make ZERO wire calls. (The autouse conftest fixtures
    strip CLAUDE_*SESSION_ID and pin BETTER_MEMORY_HOME to an empty tmp
    dir, so nothing resolves here.)"""
    with pytest.raises(ValueError, match="session_id"):
        await backend_without_session.observe(content="x")
    mock_data_client.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_observe_lazily_resolves_session_id_from_env(
    backend_without_session, mock_data_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP server may spawn before CLAUDE_SESSION_ID / the marker
    exists; a backend frozen at session_id=None must re-resolve at first
    observe instead of raising forever."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-resolved-session")
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    result = await backend_without_session.observe(content="x")
    assert result == "evt-x"
    kwargs = mock_data_client.create_event.call_args.kwargs
    assert kwargs["sessionId"] == "env-resolved-session"


@pytest.mark.asyncio
async def test_observe_lazily_resolves_session_id_from_marker(
    backend_without_session, mock_data_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Marker-file fallback: the SessionStart hook writes the marker after
    the server spawns; observe picks it up on first use."""
    from better_memory.runtime.session_marker import write_session_id

    bm_home = tmp_path / "bm-marker-home"
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(bm_home))
    write_session_id(bm_home, "marker-resolved-session")
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend_without_session.observe(content="x")
    kwargs = mock_data_client.create_event.call_args.kwargs
    assert kwargs["sessionId"] == "marker-resolved-session"


@pytest.mark.asyncio
async def test_observe_reresolves_session_id_on_every_operation(
    backend_without_session, mock_data_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live-review major: the session id must NOT freeze at first
    resolution. A long-lived server process spans Claude sessions — each
    operation resolves the CURRENT env/marker value."""
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}

    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-one")
    await backend_without_session.observe(content="first")
    assert (
        mock_data_client.create_event.call_args.kwargs["sessionId"]
        == "session-one"
    )

    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-two")
    await backend_without_session.observe(content="second")
    assert (
        mock_data_client.create_event.call_args.kwargs["sessionId"]
        == "session-two"
    )


@pytest.mark.asyncio
async def test_observe_fresh_marker_beats_construction_time_session_id(
    backend, mock_data_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A marker written AFTER construction (a new Claude session started
    against the same long-lived server) supersedes the construction-time
    session id — the exact stale-adoption the freeze caused."""
    from better_memory.runtime.session_marker import write_session_id

    bm_home = tmp_path / "bm-fresh-marker-home"
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(bm_home))
    write_session_id(bm_home, "fresh-marker-session")
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x")
    kwargs = mock_data_client.create_event.call_args.kwargs
    assert kwargs["sessionId"] == "fresh-marker-session"  # not test-session-xyz


@pytest.mark.asyncio
async def test_observe_falls_back_to_construction_session_id(
    backend, mock_data_client
) -> None:
    """No live env var, no marker → the construction-time session id still
    works (the conftest strips CLAUDE_*SESSION_ID and pins an empty home)."""
    mock_data_client.create_event.return_value = {"event": {"eventId": "evt-x"}}
    await backend.observe(content="x")
    kwargs = mock_data_client.create_event.call_args.kwargs
    assert kwargs["sessionId"] == "test-session-xyz"


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
    # Key parity with the sqlite list_observations rows — no agentcore-only
    # keys (session_id/actor_id/event_timestamp) leak; created_at is the
    # iso-formatted event timestamp; reinforcement_score is a stable None
    # placeholder (no event-plane counter).
    assert set(result[0]) == {
        "id", "content", "component", "theme", "outcome",
        "reinforcement_score", "created_at",
    }
    assert result[0]["created_at"] == "2026-05-25T12:00:00+00:00"
    assert result[0]["reinforcement_score"] is None
    assert result[0]["component"] is None

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
async def test_list_observations_raises_when_session_id_unresolvable(
    backend_without_session, mock_data_client
) -> None:
    """Same lazy re-resolution contract as observe: env → marker → raise
    with zero wire calls when nothing resolves."""
    with pytest.raises(ValueError, match="session_id"):
        await backend_without_session.list_observations(limit=5)
    mock_data_client.list_events.assert_not_called()


@pytest.mark.asyncio
async def test_list_observations_lazily_resolves_session_id_from_env(
    backend_without_session, mock_data_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-resolved-session")
    mock_data_client.list_events.return_value = {"events": []}
    assert await backend_without_session.list_observations(limit=5) == []
    kwargs = mock_data_client.list_events.call_args.kwargs
    assert kwargs["sessionId"] == "env-resolved-session"


def test_retrieve_returns_dict_with_polarity_buckets(backend, mock_data_client) -> None:
    """retrieve returns dict[str, list[dict]] matching ReflectionSynthesisService."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.retrieve(project="testproj")
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"do", "dont", "neutral"}
    for bucket in ("do", "dont", "neutral"):
        assert isinstance(result[bucket], list)


def test_retrieve_fires_project_and_general_namespace_calls(backend, mock_data_client) -> None:
    """Two list_memory_records calls — the project reflections namespace plus
    the general/promoted namespace — with NO metadataFilters (live AWS
    rejects 'polarity' as a filter key; a server-side status filter would
    hide extraction-strategy records that carry no status metadata)."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.retrieve(project="testproj")
    assert mock_data_client.list_memory_records.call_count == 2

    namespaces = set()
    for call in mock_data_client.list_memory_records.call_args_list:
        assert call.kwargs["memoryId"] == "mem-epi-def4567890"
        assert "metadataFilters" not in call.kwargs
        namespaces.add(call.kwargs["namespace"])
    assert namespaces == {
        "projects/testproj/reflections/",
        "general/reflections/",
    }


def test_retrieve_general_actor_skips_duplicate_namespace_call(
    ac_config, mock_data_client, mock_control_client
) -> None:
    """actor == general → project and general namespaces coincide; exactly
    one wire call, never two identical ones."""
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="general",
    )
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.retrieve(project="general")
    assert mock_data_client.list_memory_records.call_count == 1
    call = mock_data_client.list_memory_records.call_args
    assert call.kwargs["namespace"] == "general/reflections/"


def test_retrieve_with_polarity_kwarg_buckets_only_that_polarity(backend, mock_data_client) -> None:
    """polarity='do' -> other buckets stay empty. The namespace fan-out is
    unchanged (polarity is client-side now — it cannot narrow the wire)."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.retrieve(project="testproj", polarity="do")
    assert mock_data_client.list_memory_records.call_count == 2
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
    # The same canned summary answers BOTH namespace calls — a single merged
    # entry proves the cross-namespace id-dedup.
    assert len(do_bucket) == 1
    refl = do_bucket[0]
    # Match the sqlite-mode reflection dict shape
    assert refl["id"] == "rec-1"
    assert refl["title"] == "Test reflection title"
    assert refl["use_cases"] == "Applies when X"
    assert refl["hints"] == ["First hint.", "Second hint.", "Third hint."]
    assert refl["confidence"] == 0.85  # float
    assert refl["useful_count"] == 3
    # Internal ranking/bucketing helpers must NOT leak to the payload.
    assert not any(key.startswith("_") for key in refl), refl


def _wilson_ranking_record(rec_id: str, *, useful: int, ignored: int, overlooked: int = 0) -> dict:
    """A record with only status/polarity metadata plus the three Wilson
    inputs set — the rest default to 0."""
    import json
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
            "ignored_count": {"numberValue": ignored},
            "times_misled": {"numberValue": 0},
            "overlooked_count": {"numberValue": overlooked},
            "status": {"stringValue": "active"},
        },
    }


def test_retrieve_ranks_by_shared_wilson_ordering(backend, mock_data_client) -> None:
    """Ranking matches the shared Wilson ordering (services/scoring.py):
    wilson_lower_bound(useful+overlooked, useful+overlooked+ignored) DESC,
    confidence DESC, updated_at DESC — replacing the legacy
    useful + 3*overlooked heuristic.

    Same counters as tests/services/test_wilson_ranking.py's
    test_hit_rate_beats_raw_count: a high-volume-but-lower-hit-rate
    'workhorse' (67 useful / 125 ignored, ~0.28 raw hit rate) loses to a
    'newcomer' with a higher hit rate on far less evidence (3 useful /
    1 ignored, ~0.30 raw hit rate) — literal parity with the sqlite test's
    newcomer-beats-workhorse assertion. Both are past EXPLORATION_RATED_FLOOR
    (rated >= 3), so the exploration slot never interferes with this
    ordering."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-workhorse", useful=67, ignored=125),
                _wilson_ranking_record("r-newcomer", useful=3, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["r-newcomer", "r-workhorse"]


def test_retrieve_exploration_slot_surfaces_untested_reflection(
    backend, mock_data_client
) -> None:
    """Exploration slot (reflection.py parity): with limit_per_bucket cap=3
    and 4 candidates — 3 proven (rated >= EXPLORATION_RATED_FLOOR) ranked
    below-cap plus 1 untested (rated < 3) — the untested reflection takes
    the reserved last slot instead of being dropped."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
                _wilson_ranking_record("proven-2", useful=15, ignored=5),
                _wilson_ranking_record("proven-3", useful=10, ignored=5),
                _wilson_ranking_record("untested", useful=1, ignored=0),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj", limit_per_bucket=3, track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert len(do_ids) == 3
    assert do_ids == ["proven-1", "proven-2", "untested"]


def test_retrieve_exploration_slot_all_proven_when_no_untested(
    backend, mock_data_client
) -> None:
    """No untested candidate in the bucket -> the reserved slot is simply
    filled from the remainder in ranked order; all returned rows are
    proven."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
                _wilson_ranking_record("proven-2", useful=15, ignored=5),
                _wilson_ranking_record("proven-3", useful=10, ignored=5),
                _wilson_ranking_record("proven-4", useful=5, ignored=5),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj", limit_per_bucket=3, track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["proven-1", "proven-2", "proven-3"]


def _reflection_summary(
    rec_id: str,
    *,
    namespace: str,
    status: str | None = "active",
    polarity: str | None = "do",
) -> dict:
    """MemoryRecordSummary for the client-side status/polarity tests.
    status/polarity=None omit the metadata key entirely (the shape
    AgentCore's own extraction strategy produces)."""
    import json
    metadata: dict = {"useful_count": {"numberValue": 0}}
    if status is not None:
        metadata["status"] = {"stringValue": status}
    if polarity is not None:
        metadata["polarity"] = {"stringValue": polarity}
    return {
        "memoryRecordId": rec_id,
        "content": {"text": json.dumps({
            "title": rec_id, "use_cases": "u", "hints": "h", "confidence": "0.5",
        })},
        "namespaces": [namespace],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": metadata,
    }


def test_retrieve_includes_promoted_records_from_general_namespace(
    backend, mock_data_client
) -> None:
    """Live-review major: promote_reflection moves records to
    general/reflections/ with status=promoted — retrieve must surface them
    (the old status==active project-only query made them invisible)."""
    def stub(**kwargs):
        if kwargs["namespace"] == "general/reflections/":
            return {"memoryRecordSummaries": [
                _reflection_summary(
                    "rec-promoted",
                    namespace="/general/reflections/",
                    status="promoted",
                    polarity="dont",
                )
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    assert [r["id"] for r in result["dont"]] == ["rec-promoted"]


def test_retrieve_excludes_non_active_status_in_project_namespace(
    backend, mock_data_client
) -> None:
    """A project-namespace record whose status is explicitly not active
    (e.g. retired, or promoted served stale by the lagging index) is
    excluded — promoted records are only admitted via general/."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _reflection_summary(
                    "rec-retired",
                    namespace="/projects/testproj/reflections/",
                    status="retired",
                ),
                _reflection_summary(
                    "rec-stale-promoted",
                    namespace="/projects/testproj/reflections/",
                    status="promoted",
                ),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    assert result == {"do": [], "dont": [], "neutral": []}


def test_retrieve_defaults_missing_status_and_polarity(
    backend, mock_data_client
) -> None:
    """Records written by AgentCore's own extraction strategy carry NO
    status/polarity metadata; they must still be retrievable — missing
    status parses as active, missing polarity buckets as neutral."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _reflection_summary(
                    "rec-extracted",
                    namespace="/projects/testproj/reflections/",
                    status=None,
                    polarity=None,
                )
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    assert [r["id"] for r in result["neutral"]] == ["rec-extracted"]
    assert result["do"] == []
    assert result["dont"] == []


def test_retrieve_dedupes_record_seen_in_both_namespaces(
    backend, mock_data_client
) -> None:
    """A just-promoted record can be served by BOTH namespace queries while
    the list index lags — it must appear exactly once."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _reflection_summary(
                    "rec-dup", namespace="/projects/testproj/reflections/",
                    status="active", polarity="do",
                )
            ]}
        return {"memoryRecordSummaries": [
            _reflection_summary(
                "rec-dup", namespace="/general/reflections/",
                status="promoted", polarity="do",
            )
        ]}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    assert [r["id"] for r in result["do"]] == ["rec-dup"]


def _wilson_pair_stub(mock_data_client) -> None:
    """Stubs list_memory_records so the project reflections namespace
    returns one high-Wilson non-matching record ranked first under pure
    Wilson order, and one low-Wilson record ranked second — the pair used
    by the query/RRF-fusion tests below."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("irrelevant-highwilson", useful=50, ignored=5),
                _wilson_ranking_record("relevant-lowwilson", useful=1, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub


def _retrieve_records_stub(
    mock_data_client, results_by_namespace: dict[str, list[str]]
) -> None:
    """Stub retrieve_memory_records per-namespace: results_by_namespace maps
    namespace -> ordered list of memoryRecordId (best match first).
    Namespaces not present in the map return an empty result — mirrors how
    `_wilson_pair_stub`-style helpers key list_memory_records by
    namespace."""
    def stub(**kwargs):
        ids = results_by_namespace.get(kwargs["namespace"], [])
        return {"memoryRecordSummaries": [{"memoryRecordId": rid} for rid in ids]}
    mock_data_client.retrieve_memory_records.side_effect = stub


def test_retrieve_query_fuses_relevance_with_wilson_rrf(backend, mock_data_client) -> None:
    """Under `query`, a semantically-relevant low-Wilson record outranks a
    high-Wilson non-matching record via RRF fusion (constant 60):
    score = 1/(60+wilson_rank) + 1/(60+relevance_rank), missing relevance
    leg contributes nothing. Also asserts retrieve_memory_records is called
    PER namespace (design spec 2026-07-24-agentcore-parity-design.md §3) —
    project + general/promoted, same fan-out as the Wilson (list_memory_
    records) fetch — with the semantic_list-mirroring call shape against
    the episodic memory."""
    _wilson_pair_stub(mock_data_client)
    _retrieve_records_stub(mock_data_client, {
        "projects/testproj/reflections/": ["relevant-lowwilson"],
    })
    result = backend.retrieve(project="testproj", query="some query", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["relevant-lowwilson", "irrelevant-highwilson"]

    assert mock_data_client.retrieve_memory_records.call_count == 2
    namespaces_called = {
        call.kwargs["namespace"]
        for call in mock_data_client.retrieve_memory_records.call_args_list
    }
    assert namespaces_called == {
        "projects/testproj/reflections/",
        "general/reflections/",
    }
    for call in mock_data_client.retrieve_memory_records.call_args_list:
        assert call.kwargs["memoryId"] == "mem-epi-def4567890"
        assert call.kwargs["searchCriteria"] == {
            "searchQuery": "some query", "topK": 50,
        }


def test_retrieve_query_fuses_relevance_from_general_namespace(
    backend, mock_data_client
) -> None:
    """Design spec requires RetrieveMemoryRecords called PER namespace —
    not just the project namespace. A low-Wilson record living ONLY in
    general/reflections/ (promoted or shared) still gets a relevance boost
    from its own namespace's search call and can outrank a high-Wilson
    project-namespace record that doesn't match the query."""
    def stub_list(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record(
                    "irrelevant-project-highwilson", useful=50, ignored=5,
                ),
            ]}
        if kwargs["namespace"] == "general/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record(
                    "relevant-general-lowwilson", useful=1, ignored=1,
                ),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub_list
    _retrieve_records_stub(mock_data_client, {
        "general/reflections/": ["relevant-general-lowwilson"],
    })
    result = backend.retrieve(project="testproj", query="some query", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["relevant-general-lowwilson", "irrelevant-project-highwilson"]

    assert mock_data_client.retrieve_memory_records.call_count == 2
    namespaces_called = {
        call.kwargs["namespace"]
        for call in mock_data_client.retrieve_memory_records.call_args_list
    }
    assert namespaces_called == {
        "projects/testproj/reflections/",
        "general/reflections/",
    }


def test_retrieve_query_one_namespace_lookup_failure_still_uses_the_other(
    backend, mock_data_client
) -> None:
    """A namespace-level RetrieveMemoryRecords failure is independently
    best-effort per namespace: the project namespace call raising doesn't
    blank out the general namespace's (successful) relevance boost."""
    def stub_list(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record(
                    "irrelevant-project-highwilson", useful=50, ignored=5,
                ),
            ]}
        if kwargs["namespace"] == "general/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record(
                    "relevant-general-lowwilson", useful=1, ignored=1,
                ),
            ]}
        return {"memoryRecordSummaries": []}

    def stub_retrieve(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            raise Exception("boom")
        return {"memoryRecordSummaries": [
            {"memoryRecordId": "relevant-general-lowwilson"},
        ]}

    mock_data_client.list_memory_records.side_effect = stub_list
    mock_data_client.retrieve_memory_records.side_effect = stub_retrieve
    result = backend.retrieve(project="testproj", query="some query", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["relevant-general-lowwilson", "irrelevant-project-highwilson"]


def test_retrieve_without_query_reverts_to_wilson_order(backend, mock_data_client) -> None:
    """No `query` -> ordering is pure Wilson (today's Task-4 behaviour) and
    no semantic search call is made at all."""
    _wilson_pair_stub(mock_data_client)
    result = backend.retrieve(project="testproj", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["irrelevant-highwilson", "relevant-lowwilson"]
    mock_data_client.retrieve_memory_records.assert_not_called()


def test_retrieve_query_relevance_lookup_error_degrades_to_wilson_order(
    backend, mock_data_client
) -> None:
    """retrieve_memory_records raising on EVERY namespace call (AWS error or
    otherwise) degrades to an empty merged rank map -> pure Wilson order,
    and never raises out of retrieve()."""
    _wilson_pair_stub(mock_data_client)
    mock_data_client.retrieve_memory_records.side_effect = Exception("boom")
    result = backend.retrieve(project="testproj", query="some query", track_exposure=False)
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["irrelevant-highwilson", "relevant-lowwilson"]
    # Both namespaces were attempted (each independently best-effort) even
    # though both raised.
    assert mock_data_client.retrieve_memory_records.call_count == 2


@pytest.mark.parametrize("blank_query", ["", "   "])
def test_retrieve_blank_query_skips_relevance_call(
    backend, mock_data_client, blank_query
) -> None:
    """Blank/whitespace-only query -> no retrieve_memory_records call at all
    (not even attempted)."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.retrieve(project="testproj", query=blank_query, track_exposure=False)
    mock_data_client.retrieve_memory_records.assert_not_called()


def test_retrieve_query_exploration_slot_still_reserved_in_fused_order(
    backend, mock_data_client
) -> None:
    """Fused (query) order feeds the two-pass exploration fill, not raw
    Wilson order: relevance hits reorder the tested candidates (proven-3
    jumps ahead of proven-2, displacing it out of the cap) while the
    untested reflection still claims its reserved slot."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
                _wilson_ranking_record("proven-2", useful=15, ignored=5),
                _wilson_ranking_record("proven-3", useful=10, ignored=5),
                _wilson_ranking_record("untested", useful=1, ignored=0),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    mock_data_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {"memoryRecordId": "proven-3"},
            {"memoryRecordId": "proven-1"},
        ]
    }
    result = backend.retrieve(
        project="testproj", query="some query", limit_per_bucket=3, track_exposure=False
    )
    do_ids = [r["id"] for r in result["do"]]
    assert do_ids == ["proven-1", "proven-3", "untested"]


# ----- Task 6: relevance_ranks -----


def test_relevance_ranks_reflection_kind_stubbed_retrieve(backend, mock_data_client) -> None:
    """kinds=("reflection",) calls RetrieveMemoryRecords against the
    episodic memory, per project + general namespace, and returns the
    merged (kind, id) -> rank map from result order."""
    _retrieve_records_stub(mock_data_client, {
        "projects/testproj/reflections/": ["r1", "r2"],
    })
    result = backend.relevance_ranks(query="some query", kinds=("reflection",))
    assert result == {("reflection", "r1"): 0, ("reflection", "r2"): 1}

    assert mock_data_client.retrieve_memory_records.call_count == 2
    namespaces_called = {
        call.kwargs["namespace"]
        for call in mock_data_client.retrieve_memory_records.call_args_list
    }
    assert namespaces_called == {
        "projects/testproj/reflections/", "general/reflections/",
    }
    for call in mock_data_client.retrieve_memory_records.call_args_list:
        assert call.kwargs["memoryId"] == "mem-epi-def4567890"
        assert call.kwargs["searchCriteria"] == {
            "searchQuery": "some query", "topK": 50,
        }


def test_relevance_ranks_semantic_kind_uses_semantic_memory_and_namespace(
    backend, mock_data_client
) -> None:
    """kinds=("semantic",) calls RetrieveMemoryRecords against the
    SEMANTIC memory id, not the episodic one, using the semantic namespace
    family (mirrors semantic_list's own namespace resolution)."""
    _retrieve_records_stub(mock_data_client, {
        "projects/testproj/semantic/": ["s1"],
    })
    result = backend.relevance_ranks(query="some query", kinds=("semantic",))
    assert result == {("semantic", "s1"): 0}

    namespaces_called = {
        call.kwargs["namespace"]
        for call in mock_data_client.retrieve_memory_records.call_args_list
    }
    assert namespaces_called == {
        "projects/testproj/semantic/", "general/semantic/",
    }
    for call in mock_data_client.retrieve_memory_records.call_args_list:
        assert call.kwargs["memoryId"] == "mem-sem-abc1234567"


def test_relevance_ranks_both_kinds_combined(backend, mock_data_client) -> None:
    """Default kinds=("reflection", "semantic") returns entries for both,
    keyed separately -- ranks are per-kind, not globally comparable."""
    def stub(**kwargs):
        if kwargs["namespace"] in (
            "projects/testproj/reflections/", "general/reflections/",
        ):
            ids = ["r1"] if kwargs["namespace"] == "projects/testproj/reflections/" else []
        else:
            ids = ["s1"] if kwargs["namespace"] == "projects/testproj/semantic/" else []
        return {"memoryRecordSummaries": [{"memoryRecordId": i} for i in ids]}

    mock_data_client.retrieve_memory_records.side_effect = stub
    result = backend.relevance_ranks(query="some query")
    assert result == {("reflection", "r1"): 0, ("semantic", "s1"): 0}


def test_relevance_ranks_all_namespaces_error_returns_none(
    backend, mock_data_client
) -> None:
    """RetrieveMemoryRecords raising on EVERY namespace call, for every
    requested kind, degrades relevance_ranks to ``None`` -- NOT ``{}`` --
    since ``None`` is the caller's (retrieve_relevant's) designated signal
    that the lookup itself failed (AWS error), distinct from a
    successful-but-empty result. Never raises out of relevance_ranks."""
    mock_data_client.retrieve_memory_records.side_effect = Exception("boom")
    result = backend.relevance_ranks(query="some query")
    assert result is None


def test_relevance_ranks_one_namespace_error_other_empty_returns_empty_dict(
    backend, mock_data_client
) -> None:
    """One namespace erroring while its sibling namespace call SUCCEEDS
    (even with zero matches) must NOT degrade to None -- that would
    incorrectly signal "lookup failed" for a kind that genuinely ran and
    found nothing in at least one namespace. Matches
    _merge_relevance_rank_maps' contract: only ALL-None legs produce
    None; any successful leg (empty or not) produces a real (possibly
    empty) dict."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            raise Exception("boom")
        return {"memoryRecordSummaries": []}

    mock_data_client.retrieve_memory_records.side_effect = stub
    result = backend.relevance_ranks(query="some query", kinds=("reflection",))
    assert result == {}


@pytest.mark.parametrize("blank_query", ["", "   "])
def test_relevance_ranks_blank_query_skips_wire_call(
    backend, mock_data_client, blank_query
) -> None:
    result = backend.relevance_ranks(query=blank_query)
    assert result == {}
    mock_data_client.retrieve_memory_records.assert_not_called()


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
    # last_credited_at refreshed — as stringValue iso8601, NEVER
    # dateTimeValue: the indexedKey is declared STRING and real AWS fails
    # the whole record update on a type mismatch (live root cause of
    # apply_session_ratings applied:0/failed:1).
    last_credited = rec["metadata"]["last_credited_at"]
    assert set(last_credited) == {"stringValue"}
    assert isinstance(last_credited["stringValue"], str)


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


def test_record_exposures_is_noop(
    backend, mock_data_client, mock_control_client
) -> None:
    """No exposure log in agentcore mode (see list_session_exposures). Must
    not raise and must not call either boto client."""
    result = backend.record_exposures(
        session_id="s", items=[("reflection", "r1")], source="contextual",
    )
    assert result is None
    assert mock_data_client.method_calls == []
    assert mock_control_client.method_calls == []


_MID_SESSION_CLASSES = {
    k: v for k, v in _RATING_TO_COUNTER.items() if k != "ignored"
}


@pytest.mark.parametrize(
    "classification,counter_key",
    list(_MID_SESSION_CLASSES.items()),
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
        evidence="the memory changed my approach",
    )
    # Sqlite parity: credit_one returns the applied CLASSIFICATION.
    assert result == {"applied": classification, "skipped": None}
    rec = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert rec["metadata"][counter_key]["numberValue"] == 1
    # STRING indexed key — dateTimeValue fails the record on real AWS.
    assert set(rec["metadata"]["last_credited_at"]) == {"stringValue"}


def test_credit_one_rejects_unknown_classification(backend) -> None:
    with pytest.raises(ValueError, match="classification"):
        backend.credit_one(
            session_id="s",
            kind="reflection",
            id="rec-c",
            classification="bogus",
        )


def test_credit_one_rejects_ignored_classification(backend, mock_data_client) -> None:
    """Sqlite parity: 'ignored' is the session-end sweep's exclusive write
    path (apply_session_ratings) — credit_one must reject it mid-session
    with the same error text sqlite's MemoryRatingService.credit_one uses,
    and must not touch AWS."""
    with pytest.raises(
        ValueError,
        match=r"credit_one does not accept classification='ignored'",
    ):
        backend.credit_one(
            session_id="s",
            kind="reflection",
            id="rec-c",
            classification="ignored",
        )
    mock_data_client.get_memory_record.assert_not_called()
    mock_data_client.batch_update_memory_records.assert_not_called()


def test_credit_one_requires_evidence_for_non_ignored_class(backend, mock_data_client) -> None:
    """Evidence is now validated (shared services.memory_rating.validate_evidence
    helper) on the credit_one path too — matching sqlite's contract."""
    with pytest.raises(ValueError, match="evidence"):
        backend.credit_one(
            session_id="s",
            kind="reflection",
            id="rec-c",
            classification="cited",
        )
    mock_data_client.get_memory_record.assert_not_called()
    mock_data_client.batch_update_memory_records.assert_not_called()


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
        evidence="matched the userPreference schema",
    )
    assert result == {"applied": "cited", "skipped": None}

    # Verify both calls targeted SEMANTIC memory, not episodic
    get_call = mock_data_client.get_memory_record.call_args.kwargs
    assert get_call["memoryId"] == "mem-sem-abc1234567"
    update_call = mock_data_client.batch_update_memory_records.call_args.kwargs
    assert update_call["memoryId"] == "mem-sem-abc1234567"


def test_apply_session_ratings_credits_each_rating(backend, mock_data_client) -> None:
    """Return shape is sqlite-parity: {session_id, applied: {per-class
    counts}, skipped: {per-reason counts}} — NOT the old flat
    {applied: int, failed: int}."""
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
            {"kind": "reflection", "id": "rec-1", "class": "cited",
             "evidence": "cited it directly"},
            {"kind": "reflection", "id": "rec-2", "class": "overlooked",
             "evidence": "was retrieved but never used"},
        ],
    )
    assert mock_data_client.batch_update_memory_records.call_count == 2
    assert result == {
        "session_id": "test-session-xyz",
        "applied": {
            "cited": 1, "shaped": 0, "ignored": 0, "misled": 0,
            "overlooked": 1,
        },
        "skipped": {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        },
    }


def test_apply_session_ratings_empty_raises_like_sqlite(backend) -> None:
    """Sqlite parity: MemoryRatingService.apply_session_ratings raises on
    an empty ratings list."""
    with pytest.raises(ValueError, match="non-empty"):
        backend.apply_session_ratings(session_id="x", ratings=[])


@pytest.mark.parametrize(
    "ratings,match",
    [
        ([{"kind": "reflection", "id": "rec-x"}], "missing required field"),
        ([{"kind": "reflection", "id": "rec-x", "class": "bogus"}], "invalid 'bogus'"),
        ([{"kind": "widget", "id": "rec-x", "class": "cited"}], "invalid 'widget'"),
        (
            [{"kind": "reflection", "id": "", "class": "cited",
              "evidence": "some evidence"}],
            "non-empty string",
        ),
        (
            [
                {"kind": "reflection", "id": "rec-x", "class": "cited",
                 "evidence": "e1"},
                {"kind": "reflection", "id": "rec-x", "class": "misled",
                 "evidence": "e2"},
            ],
            "duplicate",
        ),
        (
            [{"kind": "reflection", "id": "rec-x", "class": "cited"}],
            "evidence",
        ),
    ],
)
def test_apply_session_ratings_validates_batch_before_any_wire_call(
    backend, mock_data_client, ratings, match
) -> None:
    """Sqlite parity: the whole batch is validated up front — ValueError,
    zero wire calls (nothing partially credited)."""
    with pytest.raises(ValueError, match=match):
        backend.apply_session_ratings(session_id="s", ratings=ratings)
    mock_data_client.get_memory_record.assert_not_called()
    mock_data_client.batch_update_memory_records.assert_not_called()


def test_apply_session_ratings_counts_wire_failures_as_memory_missing(
    backend, mock_data_client
) -> None:
    """A per-record batch-update failure (real AWS reports these as HTTP
    200 failedRecords) counts under skipped.memory_missing and does not
    abort the rest of the batch."""
    mock_data_client.get_memory_record.side_effect = [
        _make_record_response("rec-fail"),
        _make_record_response("rec-ok"),
    ]
    mock_data_client.batch_update_memory_records.side_effect = [
        {
            "successfulRecords": [],
            "failedRecords": [{
                "memoryRecordId": "rec-fail",
                "status": "FAILED",
                "errorCode": 400,
                "errorMessage": "boom",
            }],
        },
        {
            "successfulRecords": [{"memoryRecordId": "rec-ok", "status": "SUCCEEDED"}],
            "failedRecords": [],
        },
    ]
    result = backend.apply_session_ratings(
        session_id="test-session-xyz",
        ratings=[
            {"kind": "reflection", "id": "rec-fail", "class": "cited",
             "evidence": "e1"},
            {"kind": "reflection", "id": "rec-ok", "class": "shaped",
             "evidence": "e2"},
        ],
    )
    assert result["applied"]["shaped"] == 1
    assert result["applied"]["cited"] == 0
    assert result["skipped"]["memory_missing"] == 1


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
        "failedRecords": [
            {"memoryRecordId": "rec-fail", "status": "FAILED", "errorMessage": "boom"}
        ],
    }
    with pytest.raises(RuntimeError, match="rec-fail"):
        backend.promote_reflection(reflection_id="rec-fail")


# ===== Task 11: semantic CRUD =====


def test_semantic_observe_calls_batch_create_against_semantic_memory(
    backend, mock_data_client
) -> None:
    mock_data_client.batch_create_memory_records.return_value = {
        "successfulRecords": [
            {
                "memoryRecordId": "sm-1",
                "status": "SUCCEEDED",
                "requestIdentifier": "any",
            }
        ],
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
    # Content-hash dedup contract: requestIdentifier is sha256(content)[:80],
    # computed here independently so this is a genuine oracle. A scheme change
    # (uuid4, content+timestamp hash) silently breaks natural dedup of
    # repeated preferences on AWS.
    expected_req_id = hashlib.sha256(b"prefer uv over pip").hexdigest()[:80]
    assert rec["requestIdentifier"] == expected_req_id
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
    # §6.3: semantic_list returns SemanticMemory objects, not dicts.
    assert result[0].id == "sm-1"
    assert result[0].content == "prefer uv"


def test_semantic_list_without_search_uses_list_memory_records(backend, mock_data_client) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list(scope_filter="project")
    mock_data_client.list_memory_records.assert_called_once()
    mock_data_client.retrieve_memory_records.assert_not_called()


def test_semantic_list_default_view_fans_out_over_project_and_general(
    backend, mock_data_client
) -> None:
    """[[guard-needs-triggering-test]] Bug regression: scope_filter=None (the
    UI default) must include general-scope records, mirroring sqlite's
    (project OR scope='general'). Project namespace has 0 records; general/
    semantic has 1 -> the default view must return that 1, not 0."""
    def stub(**kwargs):
        if kwargs["namespace"] == "general/semantic/":
            return {"memoryRecordSummaries": [
                {
                    "memoryRecordId": "sem-general-1",
                    "content": {"text": "prefer uv over pip"},
                    "namespaces": ["/general/semantic/"],
                    "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                    "metadata": {"useful_count": {"numberValue": 0}},
                }
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    result = backend.semantic_list(project="testproj", scope_filter=None)
    assert [m.id for m in result] == ["sem-general-1"]
    assert result[0].scope == "general"
    namespaces = {
        c.kwargs["namespace"]
        for c in mock_data_client.list_memory_records.call_args_list
    }
    assert namespaces == {"projects/testproj/semantic/", "general/semantic/"}


def test_semantic_list_default_view_dedups_project_wins(
    backend, mock_data_client
) -> None:
    """A record served by BOTH namespaces (lagging index) appears once."""
    rec = {
        "memoryRecordId": "sem-dup",
        "content": {"text": "dup"},
        "namespaces": ["/projects/testproj/semantic/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 0}},
    }
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": [rec]}
    result = backend.semantic_list(project="testproj", scope_filter=None)
    assert [m.id for m in result] == ["sem-dup"]


def test_semantic_list_project_filter_queries_only_project_namespace(
    backend, mock_data_client
) -> None:
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.semantic_list(project="testproj", scope_filter="project")
    assert mock_data_client.list_memory_records.call_count == 1
    assert (
        mock_data_client.list_memory_records.call_args.kwargs["namespace"]
        == "projects/testproj/semantic/"
    )


def test_semantic_list_scope_classification_normalizes_leading_slash(
    backend, mock_data_client
) -> None:
    """Live dialect: stored namespaces come back WITH a leading slash
    ("/general/semantic/") — the scope classifier must normalize or every
    read-back general record misreports as project scope."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-slash-general",
                "content": {"text": "g"},
                "namespaces": ["/general/semantic/"],
            },
            {
                "memoryRecordId": "sm-slash-project",
                "content": {"text": "p"},
                "namespaces": ["/projects/testproj/semantic/"],
            },
        ]
    }
    result = backend.semantic_list()
    scopes = {r.id: r.scope for r in result}
    assert scopes == {
        "sm-slash-general": "general",
        "sm-slash-project": "project",
    }


def test_semantic_summary_metadata_ignored_count(backend, mock_data_client) -> None:
    """_semantic_summary_to_model reads ignored_count from metadata."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-ignored",
                "content": {"text": "semantic content"},
                "namespaces": ["projects/testproj/semantic/"],
                "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
                "metadata": {
                    "ignored_count": {"numberValue": 4},
                },
            }
        ]
    }
    result = backend.semantic_list()
    assert len(result) == 1
    assert result[0].times_ignored == 4


def test_semantic_summary_no_ignored_count(backend, mock_data_client) -> None:
    """_semantic_summary_to_model defaults times_ignored to 0 when absent."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-no-ignored",
                "content": {"text": "semantic content"},
                "namespaces": ["projects/testproj/semantic/"],
                "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
                "metadata": {},
            }
        ]
    }
    result = backend.semantic_list()
    assert len(result) == 1
    assert result[0].times_ignored == 0


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


def test_semantic_delete_propagates_failed_records(backend, mock_data_client) -> None:
    """semantic_delete must surface batch_delete failures rather than silently
    swallow them — consistent with every other mutation method on the class."""
    mock_data_client.batch_delete_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{
            "memoryRecordId": "sm-bad",
            "status": "FAILED",
            "errorCode": 404,
            "errorMessage": "record not found",
        }],
    }
    with pytest.raises(RuntimeError, match="sm-bad"):
        backend.semantic_delete(id="sm-bad")


# ===== Error paths: observe / retrieve / _parse_reflection_record =====


class _FakeClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError (botocore is absent in
    the unit-test env). Carries the .response dict boto3 errors expose."""

    def __init__(self, code: str = "ValidationException", message: str = "bad request") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


async def test_observe_propagates_client_error_from_create_event(backend, mock_data_client) -> None:
    """observe has no try/except around create_event — a ClientError from
    AWS (e.g. invalid session id, validation failure) must surface to the
    caller, not be swallowed."""
    mock_data_client.create_event.side_effect = _FakeClientError(
        code="ValidationException", message="Invalid sessionId"
    )
    with pytest.raises(_FakeClientError, match="Invalid sessionId"):
        await backend.observe(content="x")


async def test_observe_propagates_transport_timeout(backend, mock_data_client) -> None:
    """A transport-level timeout raised inside the executor thread must
    propagate through run_in_executor to the awaiting caller."""
    mock_data_client.create_event.side_effect = TimeoutError("read timed out")
    with pytest.raises(TimeoutError, match="read timed out"):
        await backend.observe(content="x")


def test_retrieve_propagates_when_one_namespace_fetch_fails(backend, mock_data_client) -> None:
    """One namespace's list_memory_records raising must propagate out of
    retrieve via Future.result() — no silent partial result."""
    good = {"memoryRecordSummaries": []}

    def stub(**kwargs):
        if kwargs["namespace"] == "general/reflections/":
            raise _FakeClientError(code="ThrottlingException", message="rate exceeded")
        return good

    mock_data_client.list_memory_records.side_effect = stub
    with pytest.raises(_FakeClientError, match="rate exceeded"):
        backend.retrieve(project="testproj")


def test_retrieve_propagates_when_all_polarity_fetches_fail(backend, mock_data_client) -> None:
    mock_data_client.list_memory_records.side_effect = _FakeClientError(
        code="AccessDeniedException", message="not authorized"
    )
    with pytest.raises(_FakeClientError, match="not authorized"):
        backend.retrieve(project="testproj")


def test_retrieve_propagates_worker_timeout(backend, mock_data_client) -> None:
    """A timeout inside a ThreadPoolExecutor worker surfaces from
    Future.result() as the original exception."""
    mock_data_client.list_memory_records.side_effect = TimeoutError("read timed out")
    with pytest.raises(TimeoutError, match="read timed out"):
        backend.retrieve(project="testproj")


def test_retrieve_malformed_json_content_falls_back_to_valid_shape(
    backend, mock_data_client
) -> None:
    """_parse_reflection_record swallows json.JSONDecodeError and degrades to
    an empty body — the record still comes back in the public reflection
    shape with defaulted body fields and metadata-derived counters intact."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-malformed",
                "content": {"text": "{not valid json"},
                "memoryStrategyId": "episodicReflections-qPr9876543",
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "metadata": {
                    "polarity": {"stringValue": "do"},
                    "useful_count": {"numberValue": 2},
                    "missed_count": {"numberValue": 1},
                    "overlooked_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }
        ]
    }
    result = backend.retrieve(project="testproj", polarity="do")
    assert len(result["do"]) == 1
    refl = result["do"][0]
    # Body-derived fields default; metadata-derived counters survive.
    assert refl["id"] == "rec-malformed"
    assert refl["title"] == ""
    assert refl["use_cases"] == ""
    assert refl["hints"] == []
    assert refl["confidence"] == 0.0
    assert refl["tech"] is None
    assert refl["phase"] == "general"
    assert refl["useful_count"] == 2
    assert refl["evidence_count"] == 3  # useful_count + missed_count
    assert refl["times_misled"] == 0
    assert refl["updated_at"] is None or isinstance(refl["updated_at"], str)


def test_retrieve_missing_content_key_falls_back_to_valid_shape(backend, mock_data_client) -> None:
    """A record with no content key at all parses as empty text -> empty
    body, same graceful degradation as malformed JSON."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-no-content",
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "metadata": {
                    "polarity": {"stringValue": "do"},
                    "useful_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }
        ]
    }
    result = backend.retrieve(project="testproj", polarity="do")
    assert len(result["do"]) == 1
    refl = result["do"][0]
    assert refl["id"] == "rec-no-content"
    assert refl["title"] == ""
    assert refl["hints"] == []
    assert refl["confidence"] == 0.0


def test_retrieve_non_dict_json_content_falls_back_to_valid_shape(
    backend, mock_data_client
) -> None:
    """Valid JSON that is not an object (e.g. a list) exercises the
    isinstance(body, dict) guards — every body-derived field defaults."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-list-body",
                "content": {"text": "[1, 2, 3]"},
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "metadata": {
                    "polarity": {"stringValue": "do"},
                    "useful_count": {"numberValue": 0},
                    "status": {"stringValue": "active"},
                },
            }
        ]
    }
    result = backend.retrieve(project="testproj", polarity="do")
    assert len(result["do"]) == 1
    refl = result["do"][0]
    assert refl["id"] == "rec-list-body"
    assert refl["title"] == ""
    assert refl["use_cases"] == ""
    assert refl["hints"] == []
    assert refl["confidence"] == 0.0
    assert refl["tech"] is None
    assert refl["phase"] == "general"


def test_session_bootstrap_fires_3_parallel_list_calls(backend, mock_data_client) -> None:
    """Two reflection namespace calls (project + general/promoted merge,
    shared with retrieve()) against episodic + one against semantic. No
    metadataFilters anywhere — polarity is not a legal filter key on real
    AWS and status filtering is client-side.

    Uses list_memory_records (not retrieve_memory_records) because
    bootstrap is recency / metadata-only — no semantic search query."""
    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    backend.session_bootstrap(session_id="test-session", project="testproj")
    # 3 calls total — 2 reflection (episodic) + 1 semantic
    assert mock_data_client.list_memory_records.call_count == 3

    targets = []
    for call in mock_data_client.list_memory_records.call_args_list:
        assert "metadataFilters" not in call.kwargs
        targets.append((call.kwargs["memoryId"], call.kwargs["namespace"]))

    assert ("mem-epi-def4567890", "projects/testproj/reflections/") in targets
    assert ("mem-epi-def4567890", "general/reflections/") in targets
    assert ("mem-sem-abc1234567", "projects/testproj/semantic/") in targets


def test_session_bootstrap_counts_promoted_general_reflections(
    backend, mock_data_client
) -> None:
    """Promoted reflections (general namespace, status=promoted) show up in
    the bootstrap counts — the live-review invisibility major."""
    def stub(**kwargs):
        if kwargs.get("namespace") == "general/reflections/":
            return {"memoryRecordSummaries": [
                _reflection_summary(
                    "rec-promoted", namespace="/general/reflections/",
                    status="promoted", polarity="do",
                )
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.session_bootstrap(session_id="s", project="testproj")
    assert result["reflections_counts"] == {"do": 1, "dont": 0, "neutral": 0}


def test_session_bootstrap_honours_cwd_param(
    backend, mock_data_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """project=None + cwd → the project resolves via project_name(cwd)
    (sqlite parity), NOT the construction-time project. A .better-memory
    override file in cwd makes the resolution deterministic."""
    proj_dir = tmp_path / "somewhere"
    proj_dir.mkdir()
    (proj_dir / ".better-memory").write_text("cwdproj\n", encoding="utf-8")
    monkeypatch.delenv("BETTER_MEMORY_PROJECT", raising=False)

    mock_data_client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    result = backend.session_bootstrap(session_id="s", cwd=proj_dir)
    assert result["project"] == "cwdproj"
    namespaces = {
        call.kwargs["namespace"]
        for call in mock_data_client.list_memory_records.call_args_list
    }
    assert "projects/cwdproj/reflections/" in namespaces
    assert "projects/cwdproj/semantic/" in namespaces
    assert "projects/testproj/reflections/" not in namespaces


def test_session_bootstrap_returns_envelope_matching_sqlite_shape(
    backend, mock_data_client
) -> None:
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


# ===== T1: _parse_reflection_record body-first with metadata fallback =====


def test_parse_reflection_extracted_record_unchanged(backend) -> None:
    """INVARIANT (migration §1b): an AWS-extracted record — state in
    metadata, NONE of the migration state keys in the body — parses exactly
    as it did before the body-first change. Every field is asserted against
    the pre-change computation (evidence_count = useful+missed;
    polarity/status/counters from metadata; updated_at from createdAt)."""
    import json

    created = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    rec = {
        "memoryRecordId": "rec-extracted",
        "content": {"text": json.dumps({
            "title": "Extracted title",
            "use_cases": "Applies when X",
            "hints": "First hint. Second hint.",
            "confidence": "0.8",
            "tech": "python",
            "phase": "build",
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": created,
        "metadata": {
            "polarity": {"stringValue": "dont"},
            "status": {"stringValue": "active"},
            "useful_count": {"numberValue": 4},
            "missed_count": {"numberValue": 2},
            "times_misled": {"numberValue": 1},
            "overlooked_count": {"numberValue": 3},
        },
    }

    parsed = backend._parse_reflection_record(rec)

    assert parsed == {
        "id": "rec-extracted",
        "title": "Extracted title",
        "phase": "build",
        "use_cases": "Applies when X",
        "hints": ["First hint. Second hint."],
        "confidence": 0.8,
        "tech": "python",
        # evidence_count computed from metadata useful(4)+missed(2)
        "evidence_count": 6,
        "useful_count": 4,
        # times_overlooked mirrors _overlooked_count (metadata overlooked_count=3).
        "times_overlooked": 3,
        # AgentCore has no exposure/rating sweep, so times_ignored is always 0
        # (PR #84 review: this is the true recorded signal, not a corruption).
        "times_ignored": 0,
        "times_misled": 1,
        # updated_at derived from createdAt (no body updated_at, no sys key)
        "updated_at": created.isoformat(),
        "_overlooked_count": 3,
        "_updated_at_ts": created.timestamp(),
        "_polarity": "dont",
        "_status": "active",
    }


def test_parse_reflection_aws_situation_schema_renders_content(backend) -> None:
    """AgentCore's episodicReflections strategy auto-extracts records with its
    OWN body schema {situation, intent, assessment, justification, reflection,
    turns} -- NONE of our body keys (title/use_cases/hints). Before the dual-
    schema parse these rendered blank in the UI and retrieve. Assert the parser
    now derives real title/use_cases/hints from the AWS body, AND still reads
    the learning-loop counters from metadata (so credit/Wilson are unaffected)."""
    import json

    created = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    rec = {
        "memoryRecordId": "rec-aws",
        "content": {"text": json.dumps({
            "situation": "The user submitted two dense technical specs.",
            "intent": "Have the assistant record the architectural decisions.",
            "assessment": "No",
            "justification": "The session was closed by a system signal first.",
            "reflection": (
                "Sessions closed before a reply lose the submitted spec. "
                "Persist inbound specs on receipt."
            ),
            "turns": [{"situation": "detail"}],
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": created,
        "metadata": {
            "useful_count": {"numberValue": 5},
            "overlooked_count": {"numberValue": 2},
            "times_misled": {"numberValue": 0},
        },
    }

    parsed = backend._parse_reflection_record(rec)

    # Content derived from the AWS body (was blank before this change).
    assert parsed["title"], "AWS-extracted record must not render a blank title"
    assert "Sessions closed" in parsed["title"]  # from `reflection`
    assert parsed["use_cases"] == "The user submitted two dense technical specs."
    assert parsed["hints"], "AWS-extracted record must not render blank hints"
    assert any("Persist inbound specs" in h for h in parsed["hints"])
    # Learning-loop counters STILL read from metadata -> credit/Wilson unchanged.
    assert parsed["useful_count"] == 5
    assert parsed["times_overlooked"] == 2
    assert parsed["id"] == "rec-aws"


def test_parse_reflection_our_body_schema_not_touched_by_aws_branch(backend) -> None:
    """Regression: a record whose body carries OUR `title` key must NOT be
    rewritten by the AWS-schema branch -- title/use_cases/hints come straight
    from the body even if a stray `situation` key is also present."""
    import json

    rec = {
        "memoryRecordId": "rec-ours",
        "content": {"text": json.dumps({
            "title": "Real curated title",
            "use_cases": "When doing X",
            "hints": "Do the thing.",
            "situation": "should be ignored -- the body has a real title",
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "metadata": {},
    }

    parsed = backend._parse_reflection_record(rec)

    assert parsed["title"] == "Real curated title"
    assert parsed["use_cases"] == "When doing X"
    assert parsed["hints"] == ["Do the thing."]


def test_parse_reflection_body_state_record_uses_body(backend) -> None:
    """A migrated record carries ALL reflection state in the JSON body and
    an EMPTY metadata map. It must parse from the body: polarity/status,
    useful_count/times_misled/times_overlooked counters, evidence_count and
    updated_at all resolve body-first (§6.1/§6.2)."""
    import json

    rec = {
        "memoryRecordId": "rec-migrated",
        "content": {"text": json.dumps({
            "title": "Migrated title",
            "use_cases": "Applies when Y",
            "hints": ["Hint one.", "Hint two."],
            "confidence": 0.9,
            "tech": "rust",
            "phase": "plan",
            "evidence_count": 12,
            "updated_at": "2026-06-01T09:30:00+00:00",
            "polarity": "do",
            "status": "promoted",
            "useful_count": 7,
            "times_misled": 2,
            "times_overlooked": 5,
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        # No metadata map at all — migrated reflections carry none.
        "metadata": {},
    }

    parsed = backend._parse_reflection_record(rec)

    # Body counters win over the (absent) metadata numberValues.
    assert parsed["useful_count"] == 7
    assert parsed["times_misled"] == 2
    assert parsed["_overlooked_count"] == 5
    # Public times_overlooked mirrors the internal counter; this body has
    # no ignored_count key, so times_ignored falls back to 0.
    assert parsed["times_overlooked"] == 5
    assert parsed["times_ignored"] == 0
    # evidence_count taken from the body (NOT recomputed as useful+missed=7).
    assert parsed["evidence_count"] == 12
    # updated_at is the body ISO string, and it drives the ranking ts.
    assert parsed["updated_at"] == "2026-06-01T09:30:00+00:00"
    assert parsed["_updated_at_ts"] == datetime(
        2026, 6, 1, 9, 30, tzinfo=UTC
    ).timestamp()
    # Bucket selector comes from the body polarity — CRITICAL for migrated
    # records, which carry polarity nowhere else.
    assert parsed["_polarity"] == "do"
    assert parsed["_status"] == "promoted"


def test_parse_reflection_body_ignored_count(backend) -> None:
    """ignored_count read from body (migrated reflection)."""
    import json

    rec = {
        "memoryRecordId": "rec-body-ignored",
        "content": {"text": json.dumps({
            "title": "Test",
            "use_cases": "test",
            "hints": [],
            "confidence": 0.8,
            "tech": "python",
            "phase": "general",
            "ignored_count": 3,
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {},
    }

    parsed = backend._parse_reflection_record(rec)
    assert parsed["times_ignored"] == 3


def test_migrated_record_round_trips_all_wilson_counters(backend) -> None:
    """build_reflection_record output parses back with every Wilson input
    intact — locks the migration builder's body keys to the reader's keys
    (a drift here silently zeroes counters for every migrated memory)."""
    from better_memory.storage.agentcore_migrate import build_reflection_record

    row = {
        "id": "refl-rt-1",
        "project": "testproj",
        "title": "Round trip",
        "use_cases": "when migrating",
        "hints": "[]",
        "confidence": 0.7,
        "status": "confirmed",
        "evidence_count": 9,
        "updated_at": "2026-07-01T00:00:00+00:00",
        "scope": "project",
        "polarity": "do",
        "useful_count": 4,
        "times_misled": 1,
        "times_overlooked": 2,
        "times_ignored": 6,
    }
    built = build_reflection_record(row, strategy_id="strat-episodic")
    assert built is not None

    parsed = backend._parse_reflection_record({
        "memoryRecordId": "rec-rt-1",
        "content": built["content"],
        "namespaces": built["namespaces"],
        "createdAt": datetime(2026, 7, 1, tzinfo=UTC),
        "metadata": {},
    })

    assert parsed["useful_count"] == 4
    assert parsed["times_misled"] == 1
    assert parsed["times_overlooked"] == 2
    assert parsed["times_ignored"] == 6


def test_parse_reflection_metadata_ignored_count(backend) -> None:
    """ignored_count read from metadata (AWS-extracted reflection)."""
    import json

    rec = {
        "memoryRecordId": "rec-meta-ignored",
        "content": {"text": json.dumps({
            "title": "Test",
            "use_cases": "test",
            "hints": [],
            "confidence": 0.8,
            "tech": "python",
            "phase": "general",
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {
            "ignored_count": {"numberValue": 2},
        },
    }

    parsed = backend._parse_reflection_record(rec)
    assert parsed["times_ignored"] == 2


def test_parse_reflection_no_ignored_count(backend) -> None:
    """ignored_count absent in both body and metadata defaults to 0."""
    import json

    rec = {
        "memoryRecordId": "rec-no-ignored",
        "content": {"text": json.dumps({
            "title": "Test",
            "use_cases": "test",
            "hints": [],
            "confidence": 0.8,
            "tech": "python",
            "phase": "general",
        })},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {},
    }

    parsed = backend._parse_reflection_record(rec)
    assert parsed["times_ignored"] == 0


def test_retrieve_buckets_migrated_record_by_body_polarity(
    backend, mock_data_client
) -> None:
    """End-to-end through retrieve(): a body-state record with no metadata
    lands in the bucket named by its BODY polarity and surfaces its body
    counters — proving _fetch_reflection_buckets honors the body-first parse."""
    import json

    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [{
                "memoryRecordId": "rec-migrated",
                "content": {"text": json.dumps({
                    "title": "Migrated", "use_cases": "u",
                    "hints": ["h"], "confidence": 0.9,
                    "polarity": "dont", "status": "active",
                    "useful_count": 7, "times_misled": 2,
                    "times_overlooked": 5, "evidence_count": 12,
                    "updated_at": "2026-06-01T09:30:00+00:00",
                })},
                "namespaces": ["projects/testproj/reflections/"],
                "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
                "metadata": {},
            }]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")

    assert [r["id"] for r in result["dont"]] == ["rec-migrated"]
    assert result["do"] == []
    assert result["neutral"] == []
    refl = result["dont"][0]
    assert refl["useful_count"] == 7
    assert refl["evidence_count"] == 12
    assert refl["updated_at"] == "2026-06-01T09:30:00+00:00"
    # times_overlooked/times_ignored must survive to the public payload —
    # the Wilson prior in services/relevant.py reads both keys directly.
    assert refl["times_overlooked"] == 5
    assert refl["times_ignored"] == 0
    # Internal helpers stripped from the public payload.
    assert not any(k.startswith("_") for k in refl)


# ===== T2: reflection rating on migrated (body-state) records =====
#
# AWS silently drops custom metadata on client-authored BASE records in the
# episodic reflections namespace (design §1b), so batch_update metadata writes
# are a no-op there. For migrated records (body carries source_backend=='sqlite')
# the rating paths must read-modify-write the JSON CONTENT BODY instead
# (content updates persist). AWS-extracted records (no marker) keep the
# metadata path unchanged.


def _make_migrated_reflection_record(rec_id: str, **body_overrides) -> dict:
    """A migrated (SQLite-origin) reflection: all rating state in the JSON
    content body, empty metadata (AWS drops it on client BASE records)."""
    body = {
        "title": "Migrated title",
        "use_cases": "when X",
        "hints": ["h1"],
        "confidence": 0.8,
        "tech": None,
        "phase": "general",
        "evidence_count": 3,
        "updated_at": "2026-05-01T00:00:00+00:00",
        "polarity": "do",
        "status": "active",
        "useful_count": 0,
        "times_misled": 0,
        "times_overlooked": 0,
        "missed_count": 0,
        "ignored_count": 0,
        "last_credited_at": "2026-05-01T00:00:00+00:00",
        "source_row_id": "42",
        "source_backend": "sqlite",
    }
    body.update(body_overrides)
    return {
        "memoryRecord": {
            "memoryRecordId": rec_id,
            "content": {"text": json.dumps(body)},
            "memoryStrategyId": "episodicReflections-qPr9876543",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 1, tzinfo=UTC),
            # Empty — AWS drops the custom metadata map on client records.
            "metadata": {},
        }
    }


class _FakeEpisodicStore:
    """Minimal in-memory bedrock-agentcore data client that PERSISTS content /
    namespace updates, so a subsequent get_memory_record reflects them
    (proves the migrated read-modify-write round-trips)."""

    def __init__(self, record: dict) -> None:
        self._record = record
        self.update_calls: list[dict] = []

    def get_memory_record(self, *, memoryId, memoryRecordId):  # noqa: N803
        return self._record

    def batch_update_memory_records(self, *, memoryId, records):  # noqa: N803
        self.update_calls.append({"memoryId": memoryId, "records": records})
        mr = self._record["memoryRecord"]
        for r in records:
            if "content" in r:
                mr["content"] = r["content"]
            if "namespaces" in r:
                mr["namespaces"] = r["namespaces"]
            if "metadata" in r:
                mr["metadata"] = r["metadata"]
        return {
            "successfulRecords": [
                {"memoryRecordId": records[0]["memoryRecordId"], "status": "SUCCEEDED"}
            ],
            "failedRecords": [],
        }


def _migrated_backend(ac_config, mock_control_client, record) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=_FakeEpisodicStore(record),
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
    )


def test_t2_credit_one_migrated_rewrites_body_counter(
    ac_config, mock_control_client
) -> None:
    record = _make_migrated_reflection_record("mig-1", useful_count=2)
    backend = _migrated_backend(ac_config, mock_control_client, record)
    fake = backend._data

    result = backend.credit_one(
        session_id="s", kind="reflection", id="mig-1", classification="cited",
        evidence="cited the migrated reflection",
    )
    assert result == {"applied": "cited", "skipped": None}

    # It was a CONTENT update, NOT a metadata update.
    sent = fake.update_calls[-1]["records"][0]
    assert "content" in sent
    assert "metadata" not in sent
    assert fake.update_calls[-1]["memoryId"] == "mem-epi-def4567890"

    body = json.loads(sent["content"]["text"])
    assert body["useful_count"] == 3
    assert body["source_backend"] == "sqlite"  # marker preserved
    # last_credited_at refreshed in the BODY (not metadata).
    assert isinstance(body["last_credited_at"], str)
    assert body["last_credited_at"] != "2026-05-01T00:00:00+00:00"
    # updated_at MUST NOT move on a rating: sqlite crediting never bumps
    # reflections.updated_at, and the reader resolves it body-first (§6.2), so
    # bumping here would corrupt age_days + the updated_at DESC tiebreak parity.
    assert body["updated_at"] == "2026-05-01T00:00:00+00:00"

    # Read-back through the reader reflects the bumped counter.
    parsed = backend._parse_reflection_record(
        fake.get_memory_record(memoryId="x", memoryRecordId="mig-1")["memoryRecord"]
    )
    assert parsed is not None
    assert parsed["useful_count"] == 3


def test_t2_credit_one_migrated_overlooked_bumps_times_overlooked(
    ac_config, mock_control_client
) -> None:
    """classification 'overlooked' → metadata key overlooked_count, but in the
    body it lands under the SQLite column name times_overlooked."""
    record = _make_migrated_reflection_record("mig-o", times_overlooked=4)
    backend = _migrated_backend(ac_config, mock_control_client, record)
    fake = backend._data

    result = backend.credit_one(
        session_id="s", kind="reflection", id="mig-o", classification="overlooked",
        evidence="retrieved but not acted on",
    )
    assert result == {"applied": "overlooked", "skipped": None}

    body = json.loads(fake.update_calls[-1]["records"][0]["content"]["text"])
    assert body["times_overlooked"] == 5
    assert "overlooked_count" not in body

    parsed = backend._parse_reflection_record(
        fake.get_memory_record(memoryId="x", memoryRecordId="mig-o")["memoryRecord"]
    )
    assert parsed is not None
    assert parsed["_overlooked_count"] == 5


def test_t2_record_use_migrated_success_rewrites_body(
    ac_config, mock_control_client
) -> None:
    record = _make_migrated_reflection_record("mig-r", useful_count=1)
    backend = _migrated_backend(ac_config, mock_control_client, record)
    fake = backend._data

    backend.record_use("mig-r", outcome="success")

    sent = fake.update_calls[-1]["records"][0]
    assert "content" in sent and "metadata" not in sent
    body = json.loads(sent["content"]["text"])
    assert body["useful_count"] == 2

    parsed = backend._parse_reflection_record(
        fake.get_memory_record(memoryId="x", memoryRecordId="mig-r")["memoryRecord"]
    )
    assert parsed is not None
    assert parsed["useful_count"] == 2


def test_t2_record_use_migrated_failure_bumps_missed_count_in_body(
    ac_config, mock_control_client
) -> None:
    record = _make_migrated_reflection_record("mig-f", missed_count=0)
    backend = _migrated_backend(ac_config, mock_control_client, record)
    fake = backend._data

    backend.record_use("mig-f", outcome="failure")

    body = json.loads(fake.update_calls[-1]["records"][0]["content"]["text"])
    assert body["missed_count"] == 1
    assert body["useful_count"] == 0


def test_t2_retire_reflection_migrated_rewrites_body_status(
    ac_config, mock_control_client
) -> None:
    record = _make_migrated_reflection_record("mig-s")
    backend = _migrated_backend(ac_config, mock_control_client, record)
    fake = backend._data

    backend.retire_reflection(reflection_id="mig-s")

    sent = fake.update_calls[-1]["records"][0]
    assert "content" in sent and "metadata" not in sent
    # namespace move still applied via the namespaces field.
    assert sent["namespaces"] == ["projects/testproj/retired/"]
    body = json.loads(sent["content"]["text"])
    assert body["status"] == "retired"

    parsed = backend._parse_reflection_record(
        fake.get_memory_record(memoryId="x", memoryRecordId="mig-s")["memoryRecord"]
    )
    assert parsed is not None
    assert parsed["_status"] == "retired"


def test_t2_credit_one_extracted_uses_metadata_path_unchanged(
    backend, mock_data_client
) -> None:
    """AWS-extracted reflection (no source_backend marker) → the existing
    metadata write path, NOT a content update."""
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "ext-1", useful_count=2
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "ext-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    result = backend.credit_one(
        session_id="s", kind="reflection", id="ext-1", classification="cited",
        evidence="cited the extracted reflection",
    )
    assert result == {"applied": "cited", "skipped": None}

    sent = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert "metadata" in sent
    assert "content" not in sent
    assert sent["metadata"]["useful_count"]["numberValue"] == 3
    assert set(sent["metadata"]["last_credited_at"]) == {"stringValue"}


def test_t2_record_use_extracted_uses_metadata_path_unchanged(
    backend, mock_data_client
) -> None:
    mock_data_client.get_memory_record.return_value = _make_record_response(
        "ext-r", useful_count=1
    )
    mock_data_client.batch_update_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "ext-r", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    backend.record_use("ext-r", outcome="success")

    sent = mock_data_client.batch_update_memory_records.call_args.kwargs["records"][0]
    assert "metadata" in sent
    assert "content" not in sent
    assert sent["metadata"]["useful_count"]["numberValue"] == 2


# ===== Task 2: local exposure ledger (record_exposures / list_session_exposures) =====


@pytest.fixture
def local_conn(tmp_path):
    """Real tmp sqlite connection with migrations applied — mirrors the
    services/session_bootstrap unit tests' fixture. Closed at teardown."""
    conn = connect(tmp_path / "ledger.db")
    try:
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def backend_with_local_conn(
    ac_config, mock_data_client, mock_control_client, local_conn
) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
        local_conn=local_conn,
    )


def _seed_local_reflection(conn, rid: str, *, title: str) -> None:
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count)
           VALUES (?, ?, 'testproj', 'general', 'do', 'context', '[]',
                   0.8, '2026-01-01', '2026-01-01', 0)""",
        (rid, title),
    )
    conn.commit()


def test_record_exposures_writes_to_local_ledger_when_available(
    backend_with_local_conn, local_conn
) -> None:
    """Task 2: with a local_conn wired in, record_exposures delegates to the
    shared exposure_log primitive and commits — a roundtrip through
    list_session_exposures surfaces the row with its display text."""
    _seed_local_reflection(local_conn, "refl-ledger-1", title="ledger roundtrip title")

    backend_with_local_conn.record_exposures(
        session_id="test-session-xyz",
        items=[("reflection", "refl-ledger-1")],
        source="contextual",
    )

    result = backend_with_local_conn.list_session_exposures(session_id="test-session-xyz")
    assert result["session_id"] == "test-session-xyz"
    assert len(result["exposures"]) == 1
    exposure = result["exposures"][0]
    assert exposure["kind"] == "reflection"
    assert exposure["id"] == "refl-ledger-1"
    assert exposure["title"] == "ledger roundtrip title"
    assert exposure["source"] == "contextual"
    assert exposure["exposed_at"]


def test_record_exposures_without_local_conn_stays_legacy_noop(
    backend, mock_data_client, mock_control_client
) -> None:
    """No local_conn wired (the `backend` fixture default) — record_exposures
    must keep the pre-existing no-op contract: no wire calls, no exception."""
    result = backend.record_exposures(
        session_id="s", items=[("reflection", "r1")], source="contextual",
    )
    assert result is None
    assert mock_data_client.method_calls == []
    assert mock_control_client.method_calls == []


def test_list_session_exposures_without_local_conn_stays_legacy_empty(backend) -> None:
    """No local_conn wired — list_session_exposures keeps the pre-existing
    empty-envelope contract."""
    result = backend.list_session_exposures(session_id="test-session-xyz")
    assert result == {"session_id": "test-session-xyz", "exposures": []}


def test_record_exposures_empty_session_id_writes_nothing(
    backend_with_local_conn, local_conn
) -> None:
    """Even with a real local_conn available, an empty session_id must not
    write any row (mirrors exposure_log.record's own best-effort guard)."""
    backend_with_local_conn.record_exposures(
        session_id="", items=[("reflection", "r1")], source="contextual",
    )
    row = local_conn.execute("SELECT COUNT(*) AS n FROM session_memory_exposure").fetchone()
    assert row["n"] == 0


def test_list_session_exposures_empty_session_id_unresolvable_returns_empty_envelope(
    backend_with_local_conn,
) -> None:
    """An empty session_id with no live env/marker AND no construction-time
    fallback (backend_with_local_conn was built with a real session id, so
    this exercises the FALLBACK to that construction-time value rather than
    the raise path — see the dedicated unresolvable test below for that)."""
    result = backend_with_local_conn.list_session_exposures(session_id="")
    # Falls back to the construction-time session_id ("test-session-xyz"),
    # matching _require_session_id's fallback chain.
    assert result["session_id"] == "test-session-xyz"
    assert result["exposures"] == []


def test_list_session_exposures_empty_session_id_truly_unresolvable(
    ac_config, mock_data_client, mock_control_client, local_conn,
) -> None:
    """No passed session_id, no live env/marker, AND no construction-time
    session_id (None) — must NOT raise; degrades to the empty envelope with
    session_id=None, matching sqlite's own no-session shape."""
    backend = AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id=None,
        project="testproj",
        local_conn=local_conn,
    )
    result = backend.list_session_exposures(session_id="")
    assert result == {"session_id": None, "exposures": []}


# ===== Task 4: retrieve-path exposures (source='retrieve', via_exploration) =====


def test_retrieve_records_exposures_with_exploration_tag(
    backend_with_local_conn, local_conn, mock_data_client
) -> None:
    """retrieve() writes source='retrieve' exposure rows for every returned
    reflection id when a local ledger is available; the reflection that took
    the reserved exploration slot is tagged via_exploration=1."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
                _wilson_ranking_record("proven-2", useful=15, ignored=5),
                _wilson_ranking_record("untested", useful=1, ignored=0),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend_with_local_conn.retrieve(project="testproj", limit_per_bucket=3)
    assert [r["id"] for r in result["do"]] == ["proven-1", "proven-2", "untested"]

    rows = local_conn.execute(
        "SELECT memory_id, source, via_exploration FROM session_memory_exposure "
        "WHERE session_id = 'test-session-xyz' ORDER BY memory_id"
    ).fetchall()
    by_id = {r["memory_id"]: r for r in rows}
    assert set(by_id) == {"proven-1", "proven-2", "untested"}
    assert all(r["source"] == "retrieve" for r in rows)
    assert by_id["untested"]["via_exploration"] == 1
    assert by_id["proven-1"]["via_exploration"] == 0
    assert by_id["proven-2"]["via_exploration"] == 0


def test_retrieve_without_local_conn_writes_nothing_and_buckets_unaffected(
    backend, mock_data_client
) -> None:
    """No local_conn wired (the default `backend` fixture) — retrieve must
    not raise, must write no exposure rows (there's no ledger to write to),
    and must return buckets identical to the local-ledger case."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend.retrieve(project="testproj")
    assert [r["id"] for r in result["do"]] == ["proven-1"]


def test_retrieve_track_exposure_false_writes_nothing(
    backend_with_local_conn, local_conn, mock_data_client
) -> None:
    """track_exposure=False suppresses the retrieve-path exposure write even
    though a local ledger is available — mirrors sqlite's track_exposure
    contract (used by callers, e.g. session bootstrap, that manage their own
    exposure tracking)."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("proven-1", useful=20, ignored=5),
            ]}
        return {"memoryRecordSummaries": []}

    mock_data_client.list_memory_records.side_effect = stub
    result = backend_with_local_conn.retrieve(project="testproj", track_exposure=False)
    assert [r["id"] for r in result["do"]] == ["proven-1"]
    row = local_conn.execute(
        "SELECT COUNT(*) AS n FROM session_memory_exposure"
    ).fetchone()
    assert row["n"] == 0


def test_new_capability_flags_all_false(backend) -> None:
    """AgentCore exposes only extracted memory records: no raw-observation
    store, no provenance chain, no local retention-run ledger, no
    pending_review lifecycle, no free-text reflection edit."""
    assert backend.supports_observations is False
    assert backend.supports_provenance is False
    assert backend.supports_retention_runs is False
    assert backend.supports_reflection_review is False
    assert backend.supports_reflection_text_edit is False


def test_supports_episodes_still_false_regression(backend) -> None:
    """Regression pin: the pre-existing episodes flag stays False."""
    assert backend.supports_episodes is False


# ===== reflection_get: row-only accessor (no provenance) =====


def test_reflection_get_parses_body_record(backend, mock_data_client) -> None:
    body = json.dumps({
        "title": "Body reflection", "use_cases": "when X",
        "hints": ["h1", "h2"], "confidence": "0.8", "polarity": "do",
        "status": "active", "phase": "planning",
    })
    mock_data_client.get_memory_record.return_value = {"memoryRecord": {
        "memoryRecordId": "rec-1",
        "content": {"text": body},
        "namespaces": ["projects/testproj/reflections/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 4}},
    }}
    got = backend.reflection_get(reflection_id="rec-1")
    assert got["id"] == "rec-1"
    assert got["title"] == "Body reflection"
    assert got["status"] == "active"
    assert got["polarity"] == "do"
    assert got["scope"] == "project"
    assert got["useful_count"] == 4
    assert got["last_useful_at"] is None
    # hints serialized as a JSON string so the drawer's decode_hints filter
    # decodes it identically to the sqlite column shape.
    assert json.loads(got["hints"]) == ["h1", "h2"]


def test_reflection_get_returns_none_on_404(backend, mock_data_client, monkeypatch) -> None:
    from better_memory.storage import agentcore as ac_module

    monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)
    mock_data_client.get_memory_record.side_effect = _FakeClientError(
        code="ResourceNotFoundException", message="missing"
    )
    assert backend.reflection_get(reflection_id="gone") is None


# ===== reflection_list: flat, Wilson-ordered accessor + status remap =====


def _retired_record(rec_id):
    import json
    return {
        "memoryRecordId": rec_id,
        "content": {"text": json.dumps({
            "title": rec_id, "use_cases": "u", "hints": "h",
            "confidence": "0.9", "polarity": "do", "status": "retired",
        })},
        "namespaces": ["projects/testproj/retired/"],
        "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
        "metadata": {"status": {"stringValue": "retired"}},
    }


def test_reflection_list_flat_wilson_order(backend, mock_data_client) -> None:
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-workhorse", useful=67, ignored=125),
                _wilson_ranking_record("r-newcomer", useful=3, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-newcomer", "r-workhorse"]
    # row-key completeness against the template's field list
    assert set(rows[0]) == {
        "id", "title", "project", "tech", "phase", "polarity", "confidence",
        "status", "use_cases", "evidence_count", "updated_at",
        "useful_count", "times_misled", "times_overlooked",
    }
    assert not any(k.startswith("_") for k in rows[0])


def test_reflection_list_default_status_is_active_promoted(backend, mock_data_client) -> None:
    """[[status-remap]] status=None on agentcore admits {active, promoted}
    -- NOT sqlite's {pending_review, confirmed}, since agentcore has no
    pending_review state. A record with status=promoted must be included
    by default; a status the set does not admit must be excluded.

    _wilson_ranking_record hardcodes status=active; patch one record's
    metadata to promoted to exercise the {active, promoted} admit set."""
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-active", useful=5, ignored=1),
            ]}
        if kwargs["namespace"] == "general/reflections/":
            rec = _wilson_ranking_record("r-promoted", useful=5, ignored=1)
            rec["metadata"]["status"] = {"stringValue": "promoted"}
            return {"memoryRecordSummaries": [rec]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert {r["id"] for r in rows} == {"r-active", "r-promoted"}
    assert {r["status"] for r in rows} == {"active", "promoted"}


def test_reflection_list_default_excludes_retired(backend, mock_data_client) -> None:
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-active", useful=5, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-active"]
    # retired namespaces are NOT queried under the default status set
    queried = {c.kwargs["namespace"] for c in mock_data_client.list_memory_records.call_args_list}
    assert "projects/testproj/retired/" not in queried


def test_reflection_list_status_retired_queries_retired_namespaces(
    backend, mock_data_client
) -> None:
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/retired/":
            return {"memoryRecordSummaries": [_retired_record("r-old")]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj", status="retired")
    assert [r["id"] for r in rows] == ["r-old"]
    assert rows[0]["status"] == "retired"
    queried = {c.kwargs["namespace"] for c in mock_data_client.list_memory_records.call_args_list}
    assert {"projects/testproj/retired/", "general/retired/"} <= queried


def test_reflection_list_polarity_filter_drops_non_matches(backend, mock_data_client) -> None:
    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-do", useful=5, ignored=1),  # polarity 'do'
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    assert backend.reflection_list(project="testproj", polarity="dont") == []


def test_reflection_list_best_effort_degrades_on_namespace_error(backend, mock_data_client) -> None:
    """[[guard-needs-triggering-test]] One namespace raising must NOT 500 the
    whole list -- the surviving namespace's rows come through."""
    def stub(**kwargs):
        if kwargs["namespace"] == "general/reflections/":
            raise _FakeClientError(code="ThrottlingException", message="rate exceeded")
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _wilson_ranking_record("r-survivor", useful=5, ignored=1),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-survivor"]


def test_reflection_list_dedups_across_namespaces_project_wins(backend, mock_data_client) -> None:
    """[[dedup-project-wins]] The same reflection id present in BOTH the
    project reflections namespace and the general reflections namespace
    must collapse to a single row -- the PROJECT namespace's copy wins.
    ``namespaces`` is iterated project-first (refl_project before
    refl_general) and ``seen`` short-circuits on first-seen id, so the
    general copy (deliberately given a different title/confidence here)
    must not survive into the merged result. If the namespace order or
    the seen-check were reversed, this would assert on the general copy's
    values instead and fail."""
    def _dupe_record(rec_id: str, *, title: str, confidence: str) -> dict:
        return {
            "memoryRecordId": rec_id,
            "content": {"text": json.dumps({
                "title": title, "use_cases": "u", "hints": "h", "confidence": confidence,
            })},
            "memoryStrategyId": "x",
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "metadata": {
                "polarity": {"stringValue": "do"},
                "useful_count": {"numberValue": 5},
                "missed_count": {"numberValue": 0},
                "ignored_count": {"numberValue": 1},
                "times_misled": {"numberValue": 0},
                "overlooked_count": {"numberValue": 0},
                "status": {"stringValue": "active"},
            },
        }

    def stub(**kwargs):
        if kwargs["namespace"] == "projects/testproj/reflections/":
            return {"memoryRecordSummaries": [
                _dupe_record("r-dupe", title="project-copy", confidence="0.9"),
            ]}
        if kwargs["namespace"] == "general/reflections/":
            return {"memoryRecordSummaries": [
                _dupe_record("r-dupe", title="general-copy", confidence="0.4"),
            ]}
        return {"memoryRecordSummaries": []}
    mock_data_client.list_memory_records.side_effect = stub
    rows = backend.reflection_list(project="testproj")
    assert [r["id"] for r in rows] == ["r-dupe"]
    assert rows[0]["title"] == "project-copy"
    assert rows[0]["confidence"] == pytest.approx(0.9)


# ===== semantic_get: single-record accessor =====


def test_semantic_get_maps_record(backend, mock_data_client):
    from better_memory.services.semantic import SemanticMemory
    mock_data_client.get_memory_record.return_value = {"memoryRecord": {
        "memoryRecordId": "sm-1",
        "content": {"text": "prefer uv"},
        "namespaces": ["/general/semantic/"],
        "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
        "metadata": {"useful_count": {"numberValue": 2}},
    }}
    got = backend.semantic_get(id="sm-1")
    assert isinstance(got, SemanticMemory)
    assert got.id == "sm-1" and got.scope == "general" and got.useful_count == 2


def test_semantic_get_returns_none_on_404(backend, mock_data_client, monkeypatch):
    import better_memory.storage.agentcore as ac_module
    monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)
    mock_data_client.get_memory_record.side_effect = _FakeClientError(
        code="ResourceNotFoundException", message="missing"
    )
    assert backend.semantic_get(id="gone") is None


# ===== distinct_projects: list_actors + migration-ledger namespace parse =====


def test_list_actors_parses_actor_summaries(backend, mock_data_client, ac_config) -> None:
    mock_data_client.list_actors.return_value = {
        "actorSummaries": [{"actorId": "alpha"}, {"actorId": "beta"}],
    }
    assert sorted(backend.list_actors()) == ["alpha", "beta"]
    mock_data_client.list_actors.assert_called_once_with(
        memoryId=ac_config.episodic.memory_id
    )


def test_list_actors_pages_through_nexttoken(
    backend, mock_data_client, ac_config
) -> None:
    """M1: list_actors must page through ListActors -- actors on page 2+
    were silently dropped when only the first page was read."""
    mock_data_client.list_actors.side_effect = [
        {
            "actorSummaries": [{"actorId": "alpha"}],
            "nextToken": "page-2",
        },
        {
            "actorSummaries": [{"actorId": "beta"}],
        },
    ]
    assert sorted(backend.list_actors()) == ["alpha", "beta"]
    assert mock_data_client.list_actors.call_count == 2
    first_kwargs = mock_data_client.list_actors.call_args_list[0].kwargs
    second_kwargs = mock_data_client.list_actors.call_args_list[1].kwargs
    assert first_kwargs == {"memoryId": ac_config.episodic.memory_id}
    assert second_kwargs == {
        "memoryId": ac_config.episodic.memory_id,
        "nextToken": "page-2",
    }


def test_distinct_projects_unions_actors_and_ledger_namespaces(
    backend_with_local_conn, local_conn, mock_data_client
) -> None:
    """Guard: namespace-parse rule (projects/{p}/... -> p; general/... ->
    general) feeds distinct_projects alongside ListActors."""
    from better_memory.storage.agentcore_migrate import ensure_ledger

    ensure_ledger(local_conn)
    local_conn.executemany(
        "INSERT INTO agentcore_migration "
        "(source_kind, source_id, namespace, content_hash, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("reflection", "r1", "projects/gamma/reflections/", "h1", "active"),
            ("semantic", "s1", "general/semantic/", "h2", "active"),
        ],
    )
    local_conn.commit()
    mock_data_client.list_actors.return_value = {
        "actorSummaries": [{"actorId": "alpha"}, {"actorId": "beta"}],
    }
    assert backend_with_local_conn.distinct_projects() == [
        "alpha", "beta", "gamma", "general",
    ]


def test_distinct_projects_degrades_to_ledger_when_listactors_raises(
    backend_with_local_conn, local_conn, mock_data_client
) -> None:
    """Guard: a ListActors error (e.g. AWS throttling) must not raise into
    the dropdown -- it degrades to the ledger-only project set."""
    from better_memory.storage.agentcore_migrate import ensure_ledger

    ensure_ledger(local_conn)
    local_conn.execute(
        "INSERT INTO agentcore_migration "
        "(source_kind, source_id, namespace, content_hash, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("reflection", "r1", "projects/gamma/reflections/", "h1", "active"),
    )
    local_conn.commit()
    mock_data_client.list_actors.side_effect = RuntimeError("throttled")
    assert backend_with_local_conn.distinct_projects() == ["gamma"]


def test_distinct_projects_empty_when_both_fail(backend, mock_data_client) -> None:
    """Guard: ListActors failing AND no local ledger (the plain `backend`
    fixture has no local_conn) degrades all the way to an empty list."""
    mock_data_client.list_actors.side_effect = RuntimeError("throttled")
    assert backend.distinct_projects() == []
