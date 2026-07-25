"""T3 — agentcore semantic read-parity (design §1b/§3.2/§6.3).

Two invariants proven hermetically (no AWS):

1. ``AgentCoreBackend.semantic_list`` returns objects matching the
   SqliteBackend ``SemanticMemory`` shape (``better_memory/services/semantic.py``),
   populated from the declared metadata counters — NOT plain dicts. This is
   what makes ``relevant.py`` (attribute access via ``getattr``) see real
   content + non-zero counters instead of silently defaulting to ``''`` / ``0``.

2. ``retrieve_relevant`` reads those objects and surfaces the counters.

Plus a schema assertion (design §1b/§3.2): the userPreference strategy's
declared ``memoryRecordSchema`` DECLARES the migration idempotency key
``source_row_id`` — undeclared keys are silently dropped by AWS on client
BASE writes, which would break semantic idempotency reconcile.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from better_memory.cli._agentcore_strategies import (
    SEMANTIC_METADATA_SCHEMA,
    semantic_strategy_block,
)
from better_memory.services.relevant import retrieve_relevant
from better_memory.services.semantic import SemanticMemory
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
def backend(ac_config, mock_data_client) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=MagicMock(name="control"),
        session_id="test-session-xyz",
        project="testproj",
    )


def _semantic_summary(
    *,
    record_id: str = "sm-1",
    content: str = "prefer uv over pip for dependency management",
    useful_count: int = 4,
    times_misled: int = 1,
    overlooked_count: int = 2,
    namespaces: list[str] | None = None,
) -> dict:
    """A list_memory_records summary carrying the declared counters the
    migrator writes (design §3.2). Counters are numberValue; last_credited_at
    is the collapsed stringValue timestamp."""
    return {
        "memoryRecordId": record_id,
        "content": {"text": content},
        "namespaces": namespaces or ["projects/testproj/semantic/"],
        "createdAt": datetime(2026, 5, 25, tzinfo=UTC),
        "updatedAt": datetime(2026, 6, 1, tzinfo=UTC),
        "metadata": {
            "useful_count": {"numberValue": useful_count},
            "times_misled": {"numberValue": times_misled},
            "overlooked_count": {"numberValue": overlooked_count},
            "last_credited_at": {"stringValue": "2026-06-01T00:00:00+00:00"},
            "status": {"stringValue": "active"},
            "source_row_id": {"stringValue": "row-123"},
        },
    }


# ----- Part 1a: semantic_list returns SemanticMemory-shaped objects -----


def test_semantic_list_returns_semanticmemory_shaped_objects(
    backend, mock_data_client
) -> None:
    """§6.3: semantic_list must return objects with the SemanticMemory shape,
    NOT plain dicts. Attribute access (relevant.py's getattr) must resolve to
    the real content + counters, not silent defaults."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [_semantic_summary()]
    }
    result = backend.semantic_list()
    assert len(result) == 1
    item = result[0]

    # Object shape, not a dict — dict subscripting would work on a dict but
    # relevant.py uses getattr, which only resolves on the dataclass.
    assert not isinstance(item, dict)
    assert isinstance(item, SemanticMemory)

    # Counter-bearing, populated from the declared metadata numberValues.
    assert item.id == "sm-1"
    assert item.content == "prefer uv over pip for dependency management"
    assert item.useful_count == 4
    assert item.times_misled == 1
    assert item.times_overlooked == 2
    assert item.scope == "project"
    # updated_at must be a usable ISO timestamp for relevant.py age_days.
    assert item.updated_at
    assert "2026" in item.updated_at

    # Every SemanticMemory field is present (shape parity with the SQLite
    # read model) — attribute access never AttributeErrors.
    for field_name in (
        "id", "content", "project", "scope", "created_at", "updated_at",
        "useful_count", "last_useful_at", "times_misled", "last_misled_at",
        "times_overlooked", "last_overlooked_at",
    ):
        assert hasattr(item, field_name), field_name


def test_semantic_list_counters_default_to_zero_when_metadata_absent(
    backend, mock_data_client
) -> None:
    """AWS-extracted / freshly-created records with no counter metadata must
    still yield a SemanticMemory with zeroed counters (no crash, no None)."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "sm-bare",
                "content": {"text": "bare fact"},
                "namespaces": ["projects/testproj/semantic/"],
            }
        ]
    }
    item = backend.semantic_list()[0]
    assert isinstance(item, SemanticMemory)
    assert item.content == "bare fact"
    assert item.useful_count == 0
    assert item.times_misled == 0
    assert item.times_overlooked == 0


def test_semantic_list_scope_classification_still_normalizes_leading_slash(
    backend, mock_data_client
) -> None:
    """Regression: the leading-slash namespace normalization survives the
    dict→object change."""
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            _semantic_summary(record_id="g", namespaces=["/general/semantic/"]),
            _semantic_summary(
                record_id="p", namespaces=["/projects/testproj/semantic/"]
            ),
        ]
    }
    scopes = {m.id: m.scope for m in backend.semantic_list()}
    assert scopes == {"g": "general", "p": "project"}


# ----- Part 1b: relevant.py reads the counter-bearing objects -----


def test_relevant_reads_agentcore_semantic_counters_and_content(
    backend, mock_data_client
) -> None:
    """End-to-end §6.3 proof: retrieve_relevant over the agentcore backend
    surfaces the real content + non-zero useful_count. Before the fix,
    semantic_list returned dicts and getattr(s,'content') / getattr(s,
    'useful_count') defaulted to '' / 0, so this memory would never clear
    the keyword-fallback evidence gate and semantic injection was dead.

    retrieve_memory_records is explicitly made to raise here: this test
    predates Task 6's relevance_ranks (a server-side search leg
    retrieve_relevant now also consults in agentcore mode) and exercises
    the keyword-fallback path specifically, not the relevance-rank-map
    path -- an unconfigured MagicMock would otherwise behave as a
    legitimate empty search result (MagicMock's default __iter__), which
    correctly does NOT trigger the keyword fallback post-Task-6 and would
    make this assertion fail for reasons unrelated to what it actually
    tests (semantic_list's attribute-access parity)."""
    mock_data_client.retrieve_memory_records.side_effect = RuntimeError(
        "relevance search intentionally unavailable in this test"
    )
    # No reflections; semantic namespace has one strongly-matching memory.
    mock_data_client.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            _semantic_summary(
                content="always prefer uv over pip for python installs",
                useful_count=7,
            )
        ]
    }
    out = retrieve_relevant(
        backend,
        query="uv pip python",
        project="testproj",
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert len(out) == 1
    hit = out[0]
    assert hit.kind == "semantic"
    assert hit.text == "always prefer uv over pip for python installs"
    # The non-zero useful_count made it through attribute access.
    assert hit.useful_count == 7
    assert hit.age_days is not None  # updated_at parsed → real age


# ----- Part 2: declared schema carries source_row_id -----


def test_userpreference_schema_declares_source_row_id() -> None:
    """§1b/§3.2: client BASE writes only retain keys DECLARED in the
    userPreference memoryRecordSchema. The migration idempotency key
    source_row_id MUST be declared or it is silently dropped."""
    keys = {entry["key"] for entry in SEMANTIC_METADATA_SCHEMA}
    assert "source_row_id" in keys
    src = next(e for e in SEMANTIC_METADATA_SCHEMA if e["key"] == "source_row_id")
    assert src["type"] == "STRING"

    # Counters already relied upon by the read path stay declared.
    assert {"useful_count", "times_misled", "overlooked_count"} <= keys
    # last_credited_at / status used by the semantic write path stay declared.
    assert {"last_credited_at", "status"} <= keys


def test_semantic_strategy_block_top_level_schema_declares_source_row_id() -> None:
    """The declaration must live in the strategy block actually passed to
    create_memory/provision — the top-level userPreference memoryRecordSchema."""
    block = semantic_strategy_block()
    schema = block["userPreferenceMemoryStrategy"]["memoryRecordSchema"][
        "metadataSchema"
    ]
    keys = {entry["key"] for entry in schema}
    assert "source_row_id" in keys
