"""Tests for :mod:`better_memory.search.hybrid`.

The hybrid search layer is pure-SQLite (no embedder). We manually insert
observations + their vectors so we can control every input deterministically.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)

import sqlite_vec

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.search.hybrid import SearchFilters, hybrid_search

_VEC_DIM = 768


# ---------------------------------------------------------------------------
# Fixtures / helpers
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
def fixed_now() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(fixed_now: datetime):
    return lambda: fixed_now


def _unit_vector(axis: int, dim: int = _VEC_DIM) -> list[float]:
    """Return a unit-length 768-vector with 1.0 on one axis."""
    v = [0.0] * dim
    v[axis % dim] = 1.0
    return v


def _seed(
    conn: sqlite3.Connection,
    *,
    obs_id: str,
    content: str,
    project: str = "alpha",
    component: str | None = None,
    theme: str | None = None,
    outcome: str = "neutral",
    reinforcement_score: float = 0.0,
    scope_path: str | None = None,
    status: str = "active",
    vector: list[float] | None = None,
    created_at: datetime | None = None,
) -> None:
    """Insert a fully-specified observation row and its embedding."""
    vec = vector if vector is not None else _unit_vector(0)
    created = (
        (created_at or datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC))
        .isoformat()
    )

    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, session_id,
            trigger_type, status, outcome, reinforcement_score, scope_path,
            created_at
        ) VALUES (?, ?, ?, ?, ?, 'sess', NULL, ?, ?, ?, ?, ?)
        """,
        (
            obs_id,
            content,
            project,
            component,
            theme,
            status,
            outcome,
            reinforcement_score,
            scope_path,
            created,
        ),
    )
    conn.execute(
        "INSERT INTO observation_embeddings (observation_id, embedding) VALUES (?, ?)",
        (obs_id, sqlite_vec.serialize_float32(vec)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_list(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="anything")
    assert hybrid_search(conn, clock=clock) == []


def test_text_only_hybrid_ranks_matching_first(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="python bug caught")
    _seed(conn, obs_id="b", content="python feature request")

    # FTS5 default is implicit AND; use OR so both rows match but 'a' scores
    # higher against the query "python bug".
    results = hybrid_search(conn, query_text="python OR bug", clock=clock)

    assert len(results) == 2
    assert results[0].id == "a"


def test_vector_only_hybrid_ranks_closer_first(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="alpha", vector=_unit_vector(0))
    _seed(conn, obs_id="b", content="beta", vector=_unit_vector(1))

    # Query vector very close to axis 0 → id "a" should rank first.
    q = _unit_vector(0)
    results = hybrid_search(conn, query_vector=q, clock=clock)

    assert len(results) == 2
    assert results[0].id == "a"


def test_both_sources_merge_via_rrf(conn: sqlite3.Connection, clock) -> None:
    # 'a' matches both FTS and vector; 'b' matches FTS only; 'c' matches
    # vector only. 'a' should therefore rank above 'b' and 'c'.
    _seed(conn, obs_id="a", content="marker python", vector=_unit_vector(0))
    _seed(conn, obs_id="b", content="marker rust", vector=_unit_vector(5))
    _seed(conn, obs_id="c", content="unrelated word", vector=_unit_vector(1))

    q_vec = _unit_vector(0)
    results = hybrid_search(
        conn,
        query_text="marker python",
        query_vector=q_vec,
        clock=clock,
    )

    ids = [r.id for r in results]
    assert ids[0] == "a"


def test_filter_by_project(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="shared marker", project="alpha")
    _seed(conn, obs_id="b", content="shared marker", project="beta")

    results = hybrid_search(
        conn,
        query_text="shared marker",
        filters=SearchFilters(project="alpha"),
        clock=clock,
    )
    assert [r.id for r in results] == ["a"]


def test_filter_by_component(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="shared marker", component="auth")
    _seed(conn, obs_id="b", content="shared marker", component="db")

    results = hybrid_search(
        conn,
        query_text="shared marker",
        filters=SearchFilters(component="auth"),
        clock=clock,
    )
    assert [r.id for r in results] == ["a"]


def test_filter_by_scope_path(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="marker", scope_path="foo/bar")
    _seed(conn, obs_id="b", content="marker", scope_path="baz/qux")

    results = hybrid_search(
        conn,
        query_text="marker",
        filters=SearchFilters(scope_path="foo/bar"),
        clock=clock,
    )
    assert [r.id for r in results] == ["a"]


def test_status_filter_defaults_to_active(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="marker", status="active")
    _seed(conn, obs_id="b", content="marker", status="archived")

    results = hybrid_search(conn, query_text="marker", clock=clock)
    assert [r.id for r in results] == ["a"]


def test_status_filter_can_be_overridden_to_none(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="marker", status="active")
    _seed(conn, obs_id="b", content="marker", status="archived")

    results = hybrid_search(
        conn,
        query_text="marker",
        filters=SearchFilters(status=None),
        clock=clock,
    )
    assert {r.id for r in results} == {"a", "b"}


def test_window_days_excludes_older(
    conn: sqlite3.Connection, fixed_now: datetime, clock
) -> None:
    new_ts = fixed_now - timedelta(days=1)
    old_ts = fixed_now - timedelta(days=60)
    _seed(conn, obs_id="new", content="marker", created_at=new_ts)
    _seed(conn, obs_id="old", content="marker", created_at=old_ts)

    windowed = hybrid_search(
        conn,
        query_text="marker",
        filters=SearchFilters(window_days=30),
        clock=clock,
    )
    assert [r.id for r in windowed] == ["new"]

    unwindowed = hybrid_search(
        conn,
        query_text="marker",
        filters=SearchFilters(window_days=None),
        clock=clock,
    )
    assert {r.id for r in unwindowed} == {"new", "old"}


def test_outcome_filter(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="s", content="marker", outcome="success")
    _seed(conn, obs_id="f", content="marker", outcome="failure")
    _seed(conn, obs_id="n", content="marker", outcome="neutral")

    results = hybrid_search(
        conn,
        query_text="marker",
        filters=SearchFilters(outcome="failure"),
        clock=clock,
    )
    assert [r.id for r in results] == ["f"]
    assert all(r.outcome == "failure" for r in results)


# ---------------------------------------------------------------------------
# Reinforcement + recency (the key plan assertions)
# ---------------------------------------------------------------------------


def test_reinforcement_boosts_same_similarity_item(
    conn: sqlite3.Connection, clock
) -> None:
    # Identical content + identical vector → equal raw similarity. A's
    # reinforcement_score is high so it must rank first.
    _seed(
        conn,
        obs_id="high",
        content="marker alpha",
        reinforcement_score=5.0,
        vector=_unit_vector(0),
    )
    _seed(
        conn,
        obs_id="low",
        content="marker alpha",
        reinforcement_score=0.0,
        vector=_unit_vector(0),
    )

    results = hybrid_search(
        conn,
        query_text="marker alpha",
        query_vector=_unit_vector(0),
        clock=clock,
    )
    ids = [r.id for r in results]
    assert ids.index("high") < ids.index("low")


def test_recency_decay_boosts_new(
    conn: sqlite3.Connection, fixed_now: datetime, clock
) -> None:
    _seed(
        conn,
        obs_id="new",
        content="marker alpha",
        created_at=fixed_now,
        vector=_unit_vector(0),
    )
    _seed(
        conn,
        obs_id="old",
        content="marker alpha",
        created_at=fixed_now - timedelta(days=90),
        vector=_unit_vector(0),
    )

    results = hybrid_search(
        conn,
        query_text="marker alpha",
        query_vector=_unit_vector(0),
        filters=SearchFilters(window_days=None),
        clock=clock,
    )
    ids = [r.id for r in results]
    assert ids.index("new") < ids.index("old")


def test_limit_caps_results(conn: sqlite3.Connection, clock) -> None:
    for i in range(5):
        _seed(conn, obs_id=f"id{i}", content=f"marker item{i}")

    results = hybrid_search(conn, query_text="marker", limit=2, clock=clock)
    assert len(results) == 2


def test_returns_final_score_descending(conn: sqlite3.Connection, clock) -> None:
    for i in range(3):
        _seed(conn, obs_id=f"id{i}", content=f"marker item{i}")

    results = hybrid_search(conn, query_text="marker", clock=clock)
    scores = [r.final_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_search_result_carries_fields(conn: sqlite3.Connection, clock) -> None:
    _seed(
        conn,
        obs_id="a",
        content="marker alpha",
        component="auth",
        theme="login",
        outcome="success",
        reinforcement_score=2.5,
    )
    results = hybrid_search(conn, query_text="marker", clock=clock)
    assert len(results) == 1
    r = results[0]
    assert r.id == "a"
    assert r.content == "marker alpha"
    assert r.component == "auth"
    assert r.theme == "login"
    assert r.outcome == "success"
    assert r.reinforcement_score == pytest.approx(2.5)
    assert isinstance(r.created_at, str)
    assert isinstance(r.final_score, float)
