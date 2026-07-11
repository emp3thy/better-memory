"""End-to-end: contextual injection exposes a memory -> it shows up in
session exposures -> rating it 'cited' bumps useful_count -> a later
retrieve_relevant call ranks it (activation grew).

Mirrors tests/integration/test_memory_rating_e2e.py's fixtures/seeding idiom:
a bare sqlite connection with migrations applied, direct SQL seeding, direct
SELECT assertions on the underlying tables.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.relevant import retrieve_relevant
from better_memory.storage.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------

def _seed_reflection(c, rid: str, title: str) -> None:
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'proj', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, title),
    )
    c.commit()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_contextual_injection_full_rating_loop(conn, monkeypatch, tmp_path):
    """contextual exposure -> list_session_exposures includes it ->
    apply_session_ratings('cited') bumps useful_count -> next retrieval
    ranks it up."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    # 1. Seed one reflection whose title shares >= 2 keywords with the
    #    query 'pytest windows redirect' (shares all three here).
    _seed_reflection(conn, "r1", "pytest windows redirect capture")

    # 2. Build the sqlite backend.
    backend = SqliteBackend(
        memory_conn=conn, embedder=None, session_id="e2e-sess", project="proj",
    )

    # 3. Retrieve relevant memories for the query; the seeded reflection
    #    must be in the result.
    items = retrieve_relevant(backend, query="pytest windows redirect", project="proj")
    assert any(m.kind == "reflection" and m.id == "r1" for m in items), (
        f"expected r1 in retrieve_relevant results; got {items}"
    )
    before_useful = next(m.useful_count for m in items if m.id == "r1")
    assert before_useful == 0

    # 4. Record the contextual exposure for everything just injected.
    backend.record_exposures(
        session_id="e2e-sess",
        items=[(m.kind, m.id) for m in items],
        source="contextual",
    )

    # 5. list_session_exposures must include r1 with source == 'contextual'.
    exposures = backend.list_session_exposures(session_id="e2e-sess")
    assert exposures["session_id"] == "e2e-sess"
    r1_exposure = next(
        e for e in exposures["exposures"]
        if e["kind"] == "reflection" and e["id"] == "r1"
    )
    assert r1_exposure["source"] == "contextual"

    # 6. Rate it 'cited' at session end.
    result = backend.apply_session_ratings(
        session_id="e2e-sess",
        ratings=[{"kind": "reflection", "id": "r1", "class": "cited"}],
    )
    assert result["applied"]["cited"] == 1
    assert all(v == 0 for v in result["skipped"].values())

    # 7. useful_count on the underlying row is bumped.
    useful_count = conn.execute(
        "SELECT useful_count FROM reflections WHERE id='r1'"
    ).fetchone()["useful_count"]
    assert useful_count == 1, f"r1 useful_count should be 1; got {useful_count}"

    # The exposure row is now stamped as rated -- it drops out of the
    # unrated session-exposure list.
    exposures_after = backend.list_session_exposures(session_id="e2e-sess")
    assert not any(
        e["kind"] == "reflection" and e["id"] == "r1"
        for e in exposures_after["exposures"]
    )

    # A subsequent retrieval still ranks r1 (now with a higher activation
    # score thanks to the credited useful_count).
    items_after = retrieve_relevant(backend, query="pytest windows redirect", project="proj")
    r1_after = next(m for m in items_after if m.id == "r1")
    assert r1_after.useful_count == 1
    r1_before_score = next(m.score for m in items if m.id == "r1")
    assert r1_after.score > r1_before_score
