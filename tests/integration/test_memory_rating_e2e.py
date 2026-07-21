"""End-to-end: bootstrap exposes → retrieve exposes more → mid-session
credit rates some → session-end sweep handles the rest.

Clock notes
-----------
The session_memory_exposure PK is (session_id, memory_kind, memory_id,
exposed_at).  Bootstrap and mid-session retrieves both write `exposed_at =
self._clock().isoformat()`.  If both calls share an identical timestamp, the
second INSERT OR IGNORE would silently skip (duplicate PK) and we'd end up
with only 5 rows instead of 10.

Fix: use a two-step clock that returns T0 during bootstrap and T1 (one minute
later) during the mid-session retrieve calls.  Both SessionBootstrapService and
the retrieve services accept a `clock` kwarg, so this is clean.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.semantic import SemanticMemoryService
from better_memory.services.session_bootstrap import SessionBootstrapService


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
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_reflection(c, rid: str) -> None:
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, rid),
    )
    c.commit()


def _seed_semantic(c, sid: str) -> None:
    c.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project',
                   '2026-01-01', '2026-01-01')""",
        (sid,),
    )
    c.commit()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_full_rating_loop(conn, monkeypatch):
    """Full closed loop: bootstrap → retrieve → credit → sweep → verify."""
    T0 = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    T1 = T0 + timedelta(minutes=1)   # mid-session retrieve timestamp

    # Bootstrap clock: always returns T0.
    boot_clock = lambda: T0
    # Mid-session clock: always returns T1 (distinct from T0 → separate PK rows).
    retrieve_clock = lambda: T1

    monkeypatch.setenv("CLAUDE_SESSION_ID", "SESS-1")

    # Seed three reflections (polarity='do'), two semantic memories.
    for r in ("r1", "r2", "r3"):
        _seed_reflection(conn, r)
    for s in ("s1", "s2"):
        _seed_semantic(conn, s)

    # ------------------------------------------------------------------
    # 1) Bootstrap injects all five memories.
    #    Bootstrap internally calls retrieve_reflections + list_for_project
    #    with track_exposure=False, then writes its own source='bootstrap'
    #    rows.  No source='retrieve' rows appear from bootstrap itself.
    # ------------------------------------------------------------------
    boot = SessionBootstrapService(conn, clock=boot_clock)
    boot.bootstrap(project="p", session_id="SESS-1")

    after_boot = conn.execute(
        "SELECT source, COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1' GROUP BY source"
    ).fetchall()
    sources_after_boot = {r["source"]: r["n"] for r in after_boot}
    assert sources_after_boot.get("bootstrap", 0) == 5, (
        f"Expected 5 bootstrap rows after boot; got {sources_after_boot}"
    )
    assert sources_after_boot.get("retrieve", 0) == 0, (
        f"Expected 0 retrieve rows after boot; got {sources_after_boot}"
    )

    # ------------------------------------------------------------------
    # 2) Mid-session: explicit retrieve calls re-serve the same five
    #    memories.  A session's exposures are a SET — the write path skips
    #    memories already exposed in this session (whatever the source), so
    #    the totals do not move.  (Before the dedup guard, exposed_at made
    #    the PK distinct and each re-serve added a row, double-counting every
    #    statistic over the raw table.)
    # ------------------------------------------------------------------
    refl = ReflectionSynthesisService(conn, clock=retrieve_clock)
    refl.retrieve_reflections(project="p")   # track_exposure=True (default)

    sem = SemanticMemoryService(conn, clock=retrieve_clock)
    sem.list_for_project(project="p")         # track_exposure=True (default)

    sources = conn.execute(
        "SELECT source, COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1' GROUP BY source"
    ).fetchall()
    source_counts = {r["source"]: r["n"] for r in sources}
    assert source_counts.get("bootstrap", 0) == 5, (
        f"Expected 5 bootstrap rows; got {source_counts}"
    )
    assert source_counts.get("retrieve", 0) == 0, (
        f"Re-serves must not add rows; got {source_counts}"
    )

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1'"
    ).fetchone()["n"]
    assert total == 5, f"Expected 5 total exposure rows; got {total}"

    # ------------------------------------------------------------------
    # 3) Credit r1 (reflection, cited) and s1 (semantic, shaped) mid-session.
    # ------------------------------------------------------------------
    rating_clock = lambda: T1
    rating = MemoryRatingService(conn, clock=rating_clock)

    r1_credit = rating.credit_one(
        session_id="SESS-1", kind="reflection", id="r1", classification="cited",
    )
    assert r1_credit == {"applied": "cited", "skipped": None}

    s1_credit = rating.credit_one(
        session_id="SESS-1", kind="semantic", id="s1", classification="shaped",
    )
    assert s1_credit == {"applied": "shaped", "skipped": None}

    # ------------------------------------------------------------------
    # 4) After credit_one, r1's single exposure row is stamped with
    #    rated_at and useful_count on the memory row is 1.
    # ------------------------------------------------------------------
    r1_useful = conn.execute(
        "SELECT useful_count FROM reflections WHERE id='r1'"
    ).fetchone()["useful_count"]
    assert r1_useful == 1, f"r1 useful_count should be 1; got {r1_useful}"

    s1_useful = conn.execute(
        "SELECT useful_count FROM semantic_memories WHERE id='s1'"
    ).fetchone()["useful_count"]
    assert s1_useful == 1, f"s1 useful_count should be 1; got {s1_useful}"

    # The rows for r1 + s1 are rated; the unrated set excludes them.
    unrated_rows = conn.execute(
        "SELECT memory_kind, memory_id FROM session_memory_exposure "
        "WHERE session_id='SESS-1' AND rated_at IS NULL"
    ).fetchall()
    unrated_ids = {(r["memory_kind"], r["memory_id"]) for r in unrated_rows}
    assert ("reflection", "r1") not in unrated_ids, "r1 should be fully rated"
    assert ("semantic", "s1") not in unrated_ids, "s1 should be fully rated"
    # r2, r3, s2 each have exactly one exposure row → 3 unrated rows.
    assert len(unrated_rows) == 3, (
        f"Expected 3 unrated rows (r2, r3, s2); got {len(unrated_rows)}"
    )

    # ------------------------------------------------------------------
    # 5) Session-end sweep: apply_session_ratings for the remaining distinct
    #    memories.  The method deduplicates (kind, id) by design; supplying
    #    one entry per distinct memory (not one per exposure row) is correct.
    # ------------------------------------------------------------------
    unrated_distinct = list({
        (r["memory_kind"], r["memory_id"]) for r in unrated_rows
    })
    ratings = [
        {"kind": kind, "id": mid, "class": "ignored"}
        for kind, mid in unrated_distinct
    ]
    result = rating.apply_session_ratings(
        session_id="SESS-1", ratings=ratings,
    )

    # 3 distinct memories (r2, r3, s2) → ignored count 3.
    assert result["applied"]["ignored"] == 3, (
        f"Expected 3 ignored; got {result['applied']}"
    )
    # All skip buckets are 0.
    assert all(v == 0 for v in result["skipped"].values()), (
        f"Expected no skips; got {result['skipped']}"
    )

    # ------------------------------------------------------------------
    # 6) No unrated rows remain.
    # ------------------------------------------------------------------
    still_unrated = conn.execute(
        "SELECT COUNT(*) AS n FROM session_memory_exposure "
        "WHERE session_id='SESS-1' AND rated_at IS NULL"
    ).fetchone()["n"]
    assert still_unrated == 0, f"Expected 0 unrated rows at end; got {still_unrated}"

    # ------------------------------------------------------------------
    # 7) Final counters:
    #    r1: useful_count=1 (credit_one cited)
    #    r2, r3: useful_count=0 (sweep ignored)
    #    s1: useful_count=1 (credit_one shaped)
    #    s2: useful_count=0 (sweep ignored)
    #    No misled bumps.
    # ------------------------------------------------------------------
    refl_counts = {
        row["id"]: row["useful_count"]
        for row in conn.execute(
            "SELECT id, useful_count FROM reflections "
            "WHERE id IN ('r1','r2','r3')"
        ).fetchall()
    }
    assert refl_counts == {"r1": 1, "r2": 0, "r3": 0}, (
        f"Unexpected reflection counts: {refl_counts}"
    )

    sem_counts = {
        row["id"]: row["useful_count"]
        for row in conn.execute(
            "SELECT id, useful_count FROM semantic_memories "
            "WHERE id IN ('s1','s2')"
        ).fetchall()
    }
    assert sem_counts == {"s1": 1, "s2": 0}, (
        f"Unexpected semantic counts: {sem_counts}"
    )

    # No misled bumps anywhere.
    r1_misled = conn.execute(
        "SELECT times_misled FROM reflections WHERE id='r1'"
    ).fetchone()["times_misled"]
    assert r1_misled == 0

    s1_misled = conn.execute(
        "SELECT times_misled FROM semantic_memories WHERE id='s1'"
    ).fetchone()["times_misled"]
    assert s1_misled == 0
