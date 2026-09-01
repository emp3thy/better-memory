"""Tests for :mod:`better_memory.search.hybrid`.

The hybrid search layer is pure-SQLite (no embedder). We manually insert
observations so we can control every input deterministically.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_LEGACY_SKIP = pytest.mark.skip(
    reason="Awaiting Phase 2 episodic service layer — see docs/superpowers/specs/2026-04-20-episodic-memory-design.md"
)

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.search.hybrid import SearchFilters, hybrid_search


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
    created_at: datetime | None = None,
) -> None:
    """Insert a fully-specified observation row."""
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
    conn.commit()


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


@_LEGACY_SKIP
def test_empty_query_returns_empty_list(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="anything")
    assert hybrid_search(conn, clock=clock) == []


@_LEGACY_SKIP
def test_text_only_hybrid_ranks_matching_first(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="python bug caught")
    _seed(conn, obs_id="b", content="python feature request")

    # FTS5 default is implicit AND; use OR so both rows match but 'a' scores
    # higher against the query "python bug".
    results = hybrid_search(conn, query_text="python OR bug", clock=clock)

    assert len(results) == 2
    assert results[0].id == "a"


@_LEGACY_SKIP
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


@_LEGACY_SKIP
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


@_LEGACY_SKIP
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


@_LEGACY_SKIP
def test_status_filter_defaults_to_active(conn: sqlite3.Connection, clock) -> None:
    _seed(conn, obs_id="a", content="marker", status="active")
    _seed(conn, obs_id="b", content="marker", status="archived")

    results = hybrid_search(conn, query_text="marker", clock=clock)
    assert [r.id for r in results] == ["a"]


@_LEGACY_SKIP
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


@_LEGACY_SKIP
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


@_LEGACY_SKIP
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


@_LEGACY_SKIP
def test_reinforcement_boosts_same_similarity_item(
    conn: sqlite3.Connection, clock
) -> None:
    # Identical content -> equal raw BM25 similarity. A's reinforcement_score
    # is high so it must rank first.
    _seed(
        conn,
        obs_id="high",
        content="marker alpha",
        reinforcement_score=5.0,
    )
    _seed(
        conn,
        obs_id="low",
        content="marker alpha",
        reinforcement_score=0.0,
    )

    results = hybrid_search(
        conn,
        query_text="marker alpha",
        clock=clock,
    )
    ids = [r.id for r in results]
    assert ids.index("high") < ids.index("low")


@_LEGACY_SKIP
def test_recency_decay_boosts_new(
    conn: sqlite3.Connection, fixed_now: datetime, clock
) -> None:
    _seed(
        conn,
        obs_id="new",
        content="marker alpha",
        created_at=fixed_now,
    )
    _seed(
        conn,
        obs_id="old",
        content="marker alpha",
        created_at=fixed_now - timedelta(days=90),
    )

    results = hybrid_search(
        conn,
        query_text="marker alpha",
        filters=SearchFilters(window_days=None),
        clock=clock,
    )
    ids = [r.id for r in results]
    assert ids.index("new") < ids.index("old")


@_LEGACY_SKIP
def test_limit_caps_results(conn: sqlite3.Connection, clock) -> None:
    for i in range(5):
        _seed(conn, obs_id=f"id{i}", content=f"marker item{i}")

    results = hybrid_search(conn, query_text="marker", limit=2, clock=clock)
    assert len(results) == 2


@_LEGACY_SKIP
def test_returns_final_score_descending(conn: sqlite3.Connection, clock) -> None:
    for i in range(3):
        _seed(conn, obs_id=f"id{i}", content=f"marker item{i}")

    results = hybrid_search(conn, query_text="marker", clock=clock)
    scores = [r.final_score for r in results]
    assert scores == sorted(scores, reverse=True)


@_LEGACY_SKIP
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


# ---------------------------------------------------------------------------
# Trigram leg — always-on BM25 companion to word-FTS5 (no toggle any more:
# remove-ollama-embeddings Task 7 deleted the second_source parameter along
# with the vec0 leg it used to select between).
# ---------------------------------------------------------------------------
#
# These tests use a self-contained ``_seed_obs`` helper rather than the legacy
# ``_seed`` above. The legacy helper predates the Phase 1 episodic schema (it
# omits the now-mandatory ``episode_id``) which is why every test using it
# carries ``@_LEGACY_SKIP``. The new helper inserts a parent episode first.


def _seed_obs(
    conn: sqlite3.Connection,
    *,
    obs_id: str,
    content: str,
    project: str = "alpha",
    outcome: str = "neutral",
    created_at: datetime | None = None,
) -> None:
    """Insert an observation (and its parent episode) compatible with current schema."""
    episode_id = f"ep-{obs_id}"
    created = (
        created_at or datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    ).isoformat()
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, goal, synthesized_at) "
        "VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL)",
        (episode_id, project, created, f"goal for {obs_id}"),
    )
    conn.execute(
        """
        INSERT INTO observations (
            id, content, project, component, theme, session_id,
            trigger_type, status, outcome, reinforcement_score, scope_path,
            created_at, status_changed_at, episode_id
        ) VALUES (?, ?, ?, NULL, NULL, 'sess', NULL, 'active', ?, 0.0, NULL,
                  ?, ?, ?)
        """,
        (obs_id, content, project, outcome, created, created, episode_id),
    )
    conn.commit()


def test_trigram_leg_finds_word_match(conn: sqlite3.Connection, clock) -> None:
    """The trigram BM25 leg (always fused in via RRF) surfaces a word match
    same as the word-FTS5 leg would."""
    _seed_obs(conn, obs_id="hit", content="pytest junit-xml on windows")
    _seed_obs(conn, obs_id="miss", content="completely unrelated topic")

    results = hybrid_search(
        conn,
        query_text="pytest windows",
        filters=SearchFilters(window_days=None),
        limit=2,
        clock=clock,
    )

    assert len(results) >= 1
    assert results[0].id == "hit"


def test_trigram_leg_matches_substring_beyond_word_tokenizer(
    conn: sqlite3.Connection, clock
) -> None:
    """Trigram source matches substrings the word tokenizer misses."""
    _seed_obs(conn, obs_id="sub", content="testing junitxml output")

    results = hybrid_search(
        conn,
        query_text="estin",
        filters=SearchFilters(window_days=None),
        limit=5,
        clock=clock,
    )

    assert any(r.id == "sub" for r in results)
