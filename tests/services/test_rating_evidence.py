"""Non-ignored ratings must carry a one-line evidence statement.

The ordering is the variance killer: the rater writes the evidence line
BEFORE choosing the class; nothing to point at means the class is
`ignored`. The server enforces the contract loudly - a violating batch is
rejected whole, before the savepoint, like every other validation error.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.memory_rating import (
    EVIDENCE_MAX_CHARS,
    MemoryRatingService,
)


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid="r1", session="s1"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""", (rid, rid))
    conn.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, 'reflection', ?, '2026-01-01', 'retrieve')""",
        (session, rid))
    conn.commit()


def _stored_evidence(conn, rid="r1"):
    return conn.execute(
        "SELECT evidence FROM session_memory_exposure WHERE memory_id = ?",
        (rid,)).fetchone()[0]


class TestApplyBatchEvidence:
    def test_shaped_without_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "shaped"}])

    def test_blank_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1",
                          "class": "cited", "evidence": "   "}])

    def test_overlong_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="500"):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "shaped",
                          "evidence": "x" * (EVIDENCE_MAX_CHARS + 1)}])

    def test_ignored_needs_no_evidence(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        out = svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored"}])
        assert out["applied"]["ignored"] == 1
        assert _stored_evidence(conn) is None

    def test_valid_evidence_stored_trimmed(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "shaped",
                      "evidence": "  guided the retention fix approach  "}])
        assert _stored_evidence(conn) == "guided the retention fix approach"

    def test_batch_atomic_on_evidence_violation(self, conn):
        # One bad item rejects the WHOLE batch before anything applies.
        _seed(conn, "r1")
        _seed(conn, "r2")
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError):
            svc.apply_session_ratings(
                session_id="s1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "ignored"},
                    {"kind": "reflection", "id": "r2", "class": "shaped"},
                ])
        rows = conn.execute(
            "SELECT rated_at FROM session_memory_exposure").fetchall()
        assert all(r[0] is None for r in rows)

    def test_ignored_with_evidence_stores_it(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        svc.apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored",
                      "evidence": "checked but task was unrelated"}])
        assert _stored_evidence(conn) == "checked but task was unrelated"


class TestCreditEvidence:
    def test_credit_requires_evidence(self, conn):
        """Deviation from the brief: credit_one's `evidence` parameter has
        a default of None (compat shim for the not-yet-updated MCP
        `memory.credit` handler — see Task 3), not a required keyword-only
        arg. Calling without it no longer raises TypeError; it reaches
        _validate_evidence with evidence=None, which fails validation for
        every non-ignored credit class with a ValueError mentioning
        'evidence'. See memory_rating.py credit_one docstring."""
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.credit_one(session_id="s1", kind="reflection", id="r1",
                           classification="cited")     # no evidence kwarg

    def test_credit_blank_evidence_rejected(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        with pytest.raises(ValueError, match="evidence"):
            svc.credit_one(session_id="s1", kind="reflection", id="r1",
                           classification="cited", evidence="")

    def test_credit_stores_evidence(self, conn):
        _seed(conn)
        svc = MemoryRatingService(conn)
        out = svc.credit_one(session_id="s1", kind="reflection", id="r1",
                             classification="shaped",
                             evidence="applied its retry guidance")
        assert out == {"applied": "shaped", "skipped": None}
        assert _stored_evidence(conn) == "applied its retry guidance"
