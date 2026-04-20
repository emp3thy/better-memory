"""Tests for :class:`better_memory.services.insight.InsightService`.

These tests use an in-memory (temp-file) migrated SQLite database. Phase 6
does not require embedding insights, so tests do not exercise the optional
embedder path (covered indirectly — see ``test_create_with_embed_true``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)

import sqlite_vec

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.insight import (
    Insight,
    InsightSearchResult,
    InsightService,
)

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


class _AdvancingClock:
    """Clock that returns monotonically-increasing UTC timestamps."""

    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        value = self._current
        self._current = self._current + timedelta(seconds=1)
        return value


@pytest.fixture
def advancing_clock() -> _AdvancingClock:
    return _AdvancingClock(datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def service(conn: sqlite3.Connection, fixed_clock: Any) -> InsightService:
    return InsightService(conn, clock=fixed_clock, session_id="sess-abc")


class _StubEmbedder:
    """Minimal stub of :class:`OllamaEmbedder` for the optional-embed test."""

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else [0.01] * 768
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self._vector)


# Helper used by several tests to seed an observation row directly — avoids
# pulling ObservationService (and its embedder) into this test module.
def _seed_observation(conn: sqlite3.Connection, *, content: str = "obs") -> str:
    obs_id = uuid4().hex
    conn.execute(
        """
        INSERT INTO observations (id, content, project, created_at)
        VALUES (?, ?, 'test-project', '2026-04-18T12:00:00+00:00')
        """,
        (obs_id, content),
    )
    conn.commit()
    return obs_id


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


async def test_create_returns_non_empty_id(service: InsightService) -> None:
    insight_id = await service.create(
        title="do this",
        content="always prefer X over Y",
        polarity="do",
    )
    assert isinstance(insight_id, str)
    assert insight_id


async def test_create_inserts_row_with_fields(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(
        title="do this",
        content="always prefer X over Y",
        project="proj-a",
        component="auth",
        status="pending_review",
        confidence="low",
        polarity="do",
    )
    row = conn.execute(
        "SELECT id, title, content, project, component, status, confidence, "
        "polarity, evidence_count FROM insights WHERE id = ?",
        (insight_id,),
    ).fetchone()
    assert row is not None
    assert row["id"] == insight_id
    assert row["title"] == "do this"
    assert row["content"] == "always prefer X over Y"
    assert row["project"] == "proj-a"
    assert row["component"] == "auth"
    assert row["status"] == "pending_review"
    assert row["confidence"] == "low"
    assert row["polarity"] == "do"
    assert row["evidence_count"] == 0


async def test_create_writes_audit_row(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(
        title="do this",
        content="body",
        component="auth",
        polarity="do",
    )
    rows = conn.execute(
        "SELECT entity_type, entity_id, action, actor, detail, session_id "
        "FROM audit_log WHERE entity_id = ?",
        (insight_id,),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_type"] == "insight"
    assert row["entity_id"] == insight_id
    assert row["action"] == "created"
    assert row["actor"] == "ai"
    assert row["session_id"] == "sess-abc"
    detail = json.loads(row["detail"])
    assert detail["title"] == "do this"
    assert detail["polarity"] == "do"
    assert detail["status"] == "pending_review"
    assert detail["confidence"] == "low"
    assert detail["component"] == "auth"


async def test_create_invalid_polarity_raises(service: InsightService) -> None:
    with pytest.raises(ValueError):
        await service.create(title="t", content="c", polarity="bogus")  # type: ignore[arg-type]


async def test_create_does_not_embed_by_default(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    embedder = _StubEmbedder()
    svc = InsightService(
        conn, clock=fixed_clock, session_id="sess-abc", embedder=embedder
    )
    insight_id = await svc.create(title="t", content="c")
    # No embedder call because embed=False.
    assert embedder.calls == []
    # No vector row inserted either.
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM insight_embeddings WHERE insight_id = ?",
        (insight_id,),
    ).fetchone()["c"]
    assert count == 0


async def test_create_with_embed_true_stores_vector(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    embedder = _StubEmbedder()
    svc = InsightService(
        conn, clock=fixed_clock, session_id="sess-abc", embedder=embedder
    )
    insight_id = await svc.create(
        title="embed me",
        content="content to embed",
        embed=True,
    )
    assert embedder.calls == ["embed me\n\ncontent to embed"]
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM insight_embeddings WHERE insight_id = ?",
        (insight_id,),
    ).fetchone()["c"]
    assert count == 1


async def test_create_with_embed_true_but_no_embedder_raises(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    svc = InsightService(conn, clock=fixed_clock, session_id="sess-abc")
    with pytest.raises(RuntimeError):
        await svc.create(title="t", content="c", embed=True)


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


async def test_get_returns_insight(service: InsightService) -> None:
    insight_id = await service.create(
        title="hello", content="world", polarity="do"
    )
    got = service.get(insight_id)
    assert got is not None
    assert isinstance(got, Insight)
    assert got.id == insight_id
    assert got.title == "hello"
    assert got.polarity == "do"


def test_get_returns_none_for_missing(service: InsightService) -> None:
    assert service.get("nonexistent") is None


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


async def test_update_status_changes_and_writes_status_changed_audit(
    conn: sqlite3.Connection,
    advancing_clock: _AdvancingClock,
) -> None:
    svc = InsightService(conn, clock=advancing_clock, session_id="sess-abc")
    insight_id = await svc.create(title="t", content="c")

    svc.update(insight_id, status="confirmed")

    row = conn.execute(
        "SELECT status, updated_at, created_at FROM insights WHERE id = ?",
        (insight_id,),
    ).fetchone()
    assert row["status"] == "confirmed"
    # Advancing clock — updated_at should be strictly later than created_at.
    assert row["updated_at"] > row["created_at"]

    audit_rows = conn.execute(
        "SELECT action, from_status, to_status FROM audit_log "
        "WHERE entity_id = ? ORDER BY created_at",
        (insight_id,),
    ).fetchall()
    # created + status_changed
    assert len(audit_rows) == 2
    changed = audit_rows[1]
    assert changed["action"] == "status_changed"
    assert changed["from_status"] == "pending_review"
    assert changed["to_status"] == "confirmed"


async def test_update_non_status_field_writes_updated_audit(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="old title", content="c")
    service.update(insight_id, title="new title")

    row = conn.execute(
        "SELECT title FROM insights WHERE id = ?", (insight_id,)
    ).fetchone()
    assert row["title"] == "new title"

    audit_rows = conn.execute(
        "SELECT action, from_status, to_status, detail FROM audit_log "
        "WHERE entity_id = ? ORDER BY created_at",
        (insight_id,),
    ).fetchall()
    assert len(audit_rows) == 2
    updated = audit_rows[1]
    assert updated["action"] == "updated"
    assert updated["from_status"] is None
    assert updated["to_status"] is None
    detail = json.loads(updated["detail"])
    assert "title" in detail["fields"]


def test_update_missing_insight_raises(service: InsightService) -> None:
    with pytest.raises(ValueError):
        service.update("nonexistent", title="whatever")


async def test_update_invalid_polarity_raises(service: InsightService) -> None:
    insight_id = await service.create(title="t", content="c")
    with pytest.raises(ValueError):
        service.update(insight_id, polarity="bogus")  # type: ignore[arg-type]


async def test_update_noop_when_no_fields_given(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="t", content="c")
    # Calling update with nothing to change should still succeed; it is a no-op.
    service.update(insight_id)

    audit_rows = conn.execute(
        "SELECT action FROM audit_log WHERE entity_id = ? ORDER BY created_at",
        (insight_id,),
    ).fetchall()
    # Only the initial 'created' row — no audit for empty updates.
    assert [r["action"] for r in audit_rows] == ["created"]


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


async def test_delete_removes_insight_and_related_rows(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    a_id = await service.create(title="A", content="a-body")
    b_id = await service.create(title="B", content="b-body")
    obs_id = _seed_observation(conn)
    service.add_source(a_id, obs_id)
    service.add_relation(a_id, b_id, "related")

    service.delete(a_id)

    # Insight row gone.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM insights WHERE id = ?", (a_id,)
        ).fetchone()["c"]
        == 0
    )
    # insight_sources row for a_id gone.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM insight_sources WHERE insight_id = ?",
            (a_id,),
        ).fetchone()["c"]
        == 0
    )
    # insight_relations rows referencing a_id gone.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM insight_relations "
            "WHERE from_insight_id = ? OR to_insight_id = ?",
            (a_id, a_id),
        ).fetchone()["c"]
        == 0
    )
    # b_id still present.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM insights WHERE id = ?", (b_id,)
        ).fetchone()["c"]
        == 1
    )


async def test_delete_writes_retired_audit(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="rm me", content="c")
    service.delete(insight_id)

    rows = conn.execute(
        "SELECT action, detail FROM audit_log "
        "WHERE entity_id = ? AND action = 'retired'",
        (insight_id,),
    ).fetchall()
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["title"] == "rm me"


def test_delete_missing_insight_raises(service: InsightService) -> None:
    with pytest.raises(ValueError):
        service.delete("nonexistent")


# ---------------------------------------------------------------------------
# add_source()
# ---------------------------------------------------------------------------


async def test_add_source_inserts_row_and_bumps_evidence_count(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="t", content="c")
    obs_id = _seed_observation(conn)

    service.add_source(insight_id, obs_id)

    # Row present.
    row = conn.execute(
        "SELECT insight_id, observation_id FROM insight_sources "
        "WHERE insight_id = ? AND observation_id = ?",
        (insight_id, obs_id),
    ).fetchone()
    assert row is not None

    # evidence_count bumped.
    ec = conn.execute(
        "SELECT evidence_count FROM insights WHERE id = ?", (insight_id,)
    ).fetchone()["evidence_count"]
    assert ec == 1


async def test_add_source_writes_audit(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="t", content="c")
    obs_id = _seed_observation(conn)

    service.add_source(insight_id, obs_id)

    rows = conn.execute(
        "SELECT action, detail FROM audit_log "
        "WHERE entity_id = ? AND action = 'evidence_added'",
        (insight_id,),
    ).fetchall()
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["observation_id"] == obs_id


async def test_add_source_unknown_observation_raises(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="t", content="c")
    with pytest.raises(ValueError):
        service.add_source(insight_id, "nonexistent-obs")


async def test_add_source_unknown_insight_raises(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    obs_id = _seed_observation(conn)
    with pytest.raises(ValueError):
        service.add_source("nonexistent-insight", obs_id)


async def test_add_source_duplicate_raises(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    insight_id = await service.create(title="t", content="c")
    obs_id = _seed_observation(conn)
    service.add_source(insight_id, obs_id)
    with pytest.raises(ValueError):
        service.add_source(insight_id, obs_id)


# ---------------------------------------------------------------------------
# add_relation()
# ---------------------------------------------------------------------------


async def test_add_relation_inserts_row_and_writes_audit(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    a_id = await service.create(title="A", content="a")
    b_id = await service.create(title="B", content="b")

    service.add_relation(a_id, b_id, "related")

    row = conn.execute(
        "SELECT from_insight_id, to_insight_id, relation_type "
        "FROM insight_relations WHERE from_insight_id = ? AND to_insight_id = ?",
        (a_id, b_id),
    ).fetchone()
    assert row is not None
    assert row["relation_type"] == "related"

    audit_rows = conn.execute(
        "SELECT action, entity_id, detail FROM audit_log "
        "WHERE entity_id = ? AND action = 'relation_added'",
        (a_id,),
    ).fetchall()
    assert len(audit_rows) == 1
    detail = json.loads(audit_rows[0]["detail"])
    assert detail["to_insight_id"] == b_id
    assert detail["relation_type"] == "related"


async def test_add_relation_self_raises(service: InsightService) -> None:
    a_id = await service.create(title="A", content="a")
    with pytest.raises(ValueError):
        service.add_relation(a_id, a_id, "related")


async def test_add_relation_invalid_type_raises(service: InsightService) -> None:
    a_id = await service.create(title="A", content="a")
    b_id = await service.create(title="B", content="b")
    with pytest.raises(ValueError):
        service.add_relation(a_id, b_id, "bogus")  # type: ignore[arg-type]


async def test_add_relation_unknown_insight_raises(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    a_id = await service.create(title="A", content="a")
    with pytest.raises(ValueError):
        service.add_relation(a_id, "nonexistent", "related")


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_bm25_finds_matching_insight(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    await service.create(title="alpha", content="irrelevant")
    matching_id = await service.create(
        title="beta", content="this contains marker token"
    )

    results = service.search("marker")
    assert len(results) == 1
    assert isinstance(results[0], InsightSearchResult)
    assert results[0].insight.id == matching_id


async def test_search_polarity_split_returns_only_dont(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    """Plan verification: polarity='dont' must exclude 'do' and 'neutral'."""
    do_id = await service.create(
        title="do this", content="prefer the approach", polarity="do"
    )
    dont_id = await service.create(
        title="dont that", content="avoid the approach", polarity="dont"
    )

    results = service.search(None, polarity="dont")
    ids = [r.insight.id for r in results]
    assert dont_id in ids
    assert do_id not in ids
    assert all(r.insight.polarity == "dont" for r in results)


async def test_search_none_returns_all_ordered_created_desc(
    conn: sqlite3.Connection,
    advancing_clock: _AdvancingClock,
) -> None:
    svc = InsightService(conn, clock=advancing_clock, session_id="sess-abc")
    first_id = await svc.create(title="first", content="c1")
    second_id = await svc.create(title="second", content="c2")
    third_id = await svc.create(title="third", content="c3")

    results = svc.search(None)
    ids = [r.insight.id for r in results]
    assert ids == [third_id, second_id, first_id]
    # rank is 0.0 for non-query ordering.
    assert all(r.rank == 0.0 for r in results)


async def test_search_project_filter(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    a_id = await service.create(title="in X", content="body", project="X")
    await service.create(title="in Y", content="body", project="Y")

    results = service.search(None, project="X")
    ids = [r.insight.id for r in results]
    assert ids == [a_id]


async def test_search_status_filter(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    pending_id = await service.create(title="pending", content="a")
    confirmed_id = await service.create(title="confirmed", content="b")
    service.update(confirmed_id, status="confirmed")

    results = service.search(None, status="confirmed")
    ids = [r.insight.id for r in results]
    assert ids == [confirmed_id]
    assert pending_id not in ids


async def test_search_component_filter(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    auth_id = await service.create(title="t1", content="c", component="auth")
    await service.create(title="t2", content="c", component="billing")

    results = service.search(None, component="auth")
    ids = [r.insight.id for r in results]
    assert ids == [auth_id]


async def test_search_query_with_polarity_filter_combines(
    conn: sqlite3.Connection, service: InsightService
) -> None:
    await service.create(
        title="do marker", content="do body", polarity="do"
    )
    dont_id = await service.create(
        title="dont marker", content="dont body", polarity="dont"
    )

    results = service.search("marker", polarity="dont")
    ids = [r.insight.id for r in results]
    assert ids == [dont_id]


async def test_search_limit_is_respected(
    conn: sqlite3.Connection,
    advancing_clock: _AdvancingClock,
) -> None:
    svc = InsightService(conn, clock=advancing_clock, session_id="sess-abc")
    for i in range(5):
        await svc.create(title=f"t{i}", content="c")

    results = svc.search(None, limit=2)
    assert len(results) == 2


async def test_search_with_hyphenated_query_does_not_crash(
    service: InsightService,
) -> None:
    """Regression: ``better-memory`` once raised ``no such column: memory``.

    FTS5 parses ``-memory`` as a column-exclusion filter; the service must
    sanitise operator characters out of user text before calling MATCH.
    """
    matching_id = await service.create(
        title="better-memory retrieval", content="project commit push conventions"
    )

    results = service.search("better-memory project commit push conventions")

    assert [r.insight.id for r in results] == [matching_id]


async def test_search_with_fts5_operator_chars_does_not_crash(
    service: InsightService,
) -> None:
    """Any combination of FTS5 operators must survive: colon, quote, paren."""
    await service.create(title="alpha", content="beta gamma")

    # None of these should propagate sqlite3.OperationalError.
    service.search('alpha:beta "gamma" (delta)')
    service.search("AND OR NOT NEAR")


# ---------------------------------------------------------------------------
# Smoke: sqlite_vec is available (used when embed=True)
# ---------------------------------------------------------------------------


def test_vec_extension_is_loaded(conn: sqlite3.Connection) -> None:
    """Sanity check: sqlite-vec module is loaded on the test connection."""
    # serialize_float32 is pure Python and doesn't touch the DB, but we check
    # the vec0 virtual table is accessible too.
    blob = sqlite_vec.serialize_float32([0.0] * 768)
    assert isinstance(blob, (bytes, bytearray, memoryview))
    conn.execute("SELECT COUNT(*) FROM insight_embeddings").fetchone()
