"""Tests for :meth:`ObservationService.retrieve` — the three-bucket API.

Uses a stub embedder so the test doesn't contact Ollama.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.observation import BucketedResults, ObservationService

_VEC_DIM = 768
_VEC_FIXED = [0.01] * _VEC_DIM


class _StubEmbedder:
    """Minimal async embedder that always returns the same vector."""

    def __init__(self, *, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else list(_VEC_FIXED)
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self._vector)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock() -> Any:
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def embedder() -> _StubEmbedder:
    return _StubEmbedder()


@pytest.fixture
def service(
    conn: sqlite3.Connection, fixed_clock: Any, embedder: _StubEmbedder
) -> ObservationService:
    return ObservationService(
        conn,
        embedder,
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: None,
        session_id="sess-abc",
    )


# ---------------------------------------------------------------------------
# retrieve()
# ---------------------------------------------------------------------------


async def _seed_mix(service: ObservationService) -> list[str]:
    """Seed 20 observations (7 success, 7 failure, 6 neutral); return their ids."""
    ids: list[str] = []
    for i in range(7):
        ids.append(
            await service.create(f"marker success item {i}", outcome="success")
        )
    for i in range(7):
        ids.append(
            await service.create(f"marker failure item {i}", outcome="failure")
        )
    for i in range(6):
        ids.append(
            await service.create(f"marker neutral item {i}", outcome="neutral")
        )
    return ids


async def test_retrieve_returns_bucketed_results(
    service: ObservationService,
) -> None:
    await _seed_mix(service)
    result = await service.retrieve(query="marker")
    assert isinstance(result, BucketedResults)


async def test_retrieve_dont_bucket_contains_only_failures(
    service: ObservationService,
) -> None:
    await _seed_mix(service)
    result = await service.retrieve(query="marker")
    assert len(result.dont) > 0
    assert all(r.outcome == "failure" for r in result.dont)


async def test_retrieve_do_bucket_contains_only_successes(
    service: ObservationService,
) -> None:
    await _seed_mix(service)
    result = await service.retrieve(query="marker")
    assert len(result.do) > 0
    assert all(r.outcome == "success" for r in result.do)


async def test_retrieve_neutral_bucket_contains_only_neutrals(
    service: ObservationService,
) -> None:
    await _seed_mix(service)
    result = await service.retrieve(query="marker")
    assert len(result.neutral) > 0
    assert all(r.outcome == "neutral" for r in result.neutral)


async def test_retrieve_respects_bucket_limits(
    service: ObservationService,
) -> None:
    await _seed_mix(service)  # 7 success, 7 failure, 6 neutral
    result = await service.retrieve(
        query="marker",
        do_limit=3,
        dont_limit=2,
        neutral_limit=4,
    )
    assert len(result.do) <= 3
    assert len(result.dont) <= 2
    assert len(result.neutral) <= 4


async def test_retrieve_buckets_sorted_by_final_score_descending(
    service: ObservationService,
) -> None:
    await _seed_mix(service)
    result = await service.retrieve(query="marker")
    for bucket in (result.do, result.dont, result.neutral):
        scores = [r.final_score for r in bucket]
        assert scores == sorted(scores, reverse=True)


async def test_retrieve_embeds_query_once(
    service: ObservationService, embedder: _StubEmbedder
) -> None:
    await _seed_mix(service)
    embedder.calls.clear()
    await service.retrieve(query="marker")
    assert len(embedder.calls) == 1


async def test_retrieve_reinforcement_orders_within_do_bucket(
    service: ObservationService,
    conn: sqlite3.Connection,
) -> None:
    # Two success rows with identical content; bump the reinforcement of one.
    high_id = await service.create("marker duplicate", outcome="success")
    low_id = await service.create("marker duplicate", outcome="success")
    # Add a few distractors so the bucket has more than 2 rows to sort.
    for _ in range(3):
        await service.create("other success row", outcome="success")

    # Manually push high_id's reinforcement score above low_id.
    conn.execute(
        "UPDATE observations SET reinforcement_score = 5.0 WHERE id = ?",
        (high_id,),
    )
    conn.commit()

    result = await service.retrieve(query="marker duplicate")
    ids = [r.id for r in result.do]
    assert high_id in ids and low_id in ids
    assert ids.index(high_id) < ids.index(low_id)


async def test_retrieve_with_no_query_still_returns_bucketed(
    service: ObservationService,
) -> None:
    # query=None and no vector → hybrid_search returns [] for each bucket.
    await _seed_mix(service)
    result = await service.retrieve(query=None)
    assert result.do == []
    assert result.dont == []
    assert result.neutral == []


async def test_retrieve_with_hyphenated_query_ranks_fts_match_first(
    service: ObservationService,
) -> None:
    """Regression: ``better-memory`` once crashed FTS5 as ``-memory`` column.

    The safety net in hybrid search would swallow the error and return ``[]``
    for the FTS path, so users got vector-only hits (or nothing) for any
    hyphenated query. After sanitising, the FTS path delivers the matching
    row, which then ranks first in RRF fusion because only it contributes
    an FTS rank on top of the vector rank.
    """
    matched_id = await service.create(
        "better memory retrieval conventions", outcome="success"
    )
    await service.create("unrelated row about something else", outcome="success")

    result = await service.retrieve(query="better-memory retrieval conventions")

    assert len(result.do) >= 1
    assert result.do[0].id == matched_id


async def test_retrieve_with_fts5_operator_chars_does_not_crash(
    service: ObservationService,
) -> None:
    """Colons, quotes, parentheses must all survive end-to-end."""
    await service.create("alpha beta gamma", outcome="success")

    # None of these should propagate sqlite3.OperationalError.
    await service.retrieve(query='alpha:beta "gamma" (delta)')
    await service.retrieve(query="AND OR NOT NEAR")
