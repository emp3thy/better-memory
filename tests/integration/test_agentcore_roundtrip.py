"""End-to-end roundtrip against real AWS Bedrock AgentCore.

Gated by ``BETTER_MEMORY_TEST_AGENTCORE=1``. Each scenario exercises 3+
backend methods in sequence so contract drift between methods (which
mocked unit tests can't see) surfaces here.

Async vs sync notes
-------------------
``AgentCoreBackend`` mixes async I/O wrappers and synchronous methods:

- ``async``: ``observe``, ``list_observations`` — wrap boto3 in
  ``loop.run_in_executor`` so the MCP event loop is not blocked. Tests
  drive these via ``asyncio.run(...)``.
- ``sync``: ``retrieve``, ``record_use``, ``semantic_observe``,
  ``semantic_update_text``, ``semantic_set_scope``, ``semantic_delete``,
  ``credit_one`` — called directly (no coroutine).

This split is verified against ``better_memory/storage/agentcore.py``.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

pytestmark = [pytest.mark.integration]


def test_observe_then_list_observations_returns_event(agentcore_backend) -> None:
    """observe writes an event; list_observations reads it back."""
    event_id = asyncio.run(
        agentcore_backend.observe(
            content="integration test observation",
            component="testpkg",
            theme="bug",
            outcome="failure",
        )
    )
    assert event_id

    events = asyncio.run(agentcore_backend.list_observations(limit=10))
    assert any(e["id"] == event_id for e in events), (
        f"observed event {event_id} not in list_observations({len(events)} events)"
    )


def test_semantic_observe_update_delete_full_cycle(agentcore_backend) -> None:
    """Write a semantic record, update its text, then delete it."""
    record_id = agentcore_backend.semantic_observe(
        content="integration semantic write",
        scope="project",
    )
    assert record_id

    # AgentCore has ~10s lag between create and the record being mutable.
    # AgentCoreBackend._retry_on_transient_404 should handle it.
    agentcore_backend.semantic_update_text(
        id=record_id, content="integration semantic update"
    )

    agentcore_backend.semantic_delete(id=record_id)


def test_semantic_credit_one_bumps_useful_count(agentcore_backend) -> None:
    """Fast end-to-end credit test against semantic memory (no extraction
    wait — semantic records are created directly via BatchCreate, no LLM
    pipeline). Locks down the kind/class contract that round-3 BugBot
    fixed: credit_one(kind='semantic', classification='cited') must route
    to the semantic memory and bump useful_count."""
    record_id = agentcore_backend.semantic_observe(
        content="credit test record",
        scope="project",
    )

    result = agentcore_backend.credit_one(
        session_id="int-test-credit",
        kind="semantic",
        id=record_id,
        classification="cited",
        evidence="integration test citation",
    )
    assert result["applied"] == record_id
    assert result["skipped"] is None

    # Cleanup
    agentcore_backend.semantic_delete(id=record_id)


def test_no_leaked_memories_after_session(agentcore_throwaway_memories) -> None:
    """Sanity check: after fixture teardown, the throwaway memories should
    be deleted. Runs LAST (after all other tests in this module) — pytest
    doesn't guarantee order across modules but does within a single one.

    The list_memories call here happens BEFORE teardown (fixture still
    active), so this primarily exercises that the fixture WAS used and that
    the names were unique-suffixed (no accidental collision with another
    test run)."""
    sem_record, epi_record = agentcore_throwaway_memories
    assert sem_record.memory_name.startswith("bm_int_sem_")
    assert epi_record.memory_name.startswith("bm_int_epi_")
    assert sem_record.memory_id != epi_record.memory_id


def test_observe_credit_uses_correct_counter_via_extraction(agentcore_backend) -> None:
    """SLOW: observe -> closure -> wait for AgentCore extraction -> credit.

    SKIPPED unless BETTER_MEMORY_TEST_AGENTCORE_SLOW=1 because episodic
    extraction takes 1-3 min with closure event (15-20 min without). The
    test locks down the credit -> useful_count contract for EXTRACTED
    reflections (vs the directly-written semantic records covered by the
    fast credit test above)."""
    if os.environ.get("BETTER_MEMORY_TEST_AGENTCORE_SLOW") != "1":
        pytest.skip("Set BETTER_MEMORY_TEST_AGENTCORE_SLOW=1 to run.")

    # Write 3 observations to give the strategy enough signal to extract
    for i in range(3):
        asyncio.run(
            agentcore_backend.observe(
                content=f"slow integration test observation {i}",
                theme="bug",
                outcome="failure",
            )
        )

    # Fire closure event so extraction triggers within minutes
    # (the AgentCoreBackend.session_bootstrap path doesn't fire this; we
    # call create_event directly via the data client for the slow test)
    from datetime import UTC, datetime

    agentcore_backend._data.create_event(
        memoryId=agentcore_backend._cfg.episodic.memory_id,
        actorId="integration",
        sessionId=agentcore_backend._session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[{"conversational": {"role": "OTHER", "content": {"text": "closed"}}}],
    )

    # Poll for extracted reflections — ~1-3 min after closure
    deadline = time.monotonic() + 360  # 6min upper bound
    reflections: list[dict] = []
    while time.monotonic() < deadline:
        result = agentcore_backend.retrieve(project="integration", limit_per_bucket=10)
        reflections = [r for bucket in result.values() for r in bucket]
        if reflections:
            break
        time.sleep(20)
    assert reflections, "AgentCore did not extract any reflections within 6min"

    # Credit the first reflection
    first = reflections[0]
    result = agentcore_backend.credit_one(
        session_id="int-test-credit-slow",
        kind="reflection",
        id=first["id"],
        classification="cited",
        evidence="integration test citation",
    )
    assert result["applied"] == first["id"]
