"""Tests for MemoryRatingService (credit_one and apply_session_ratings)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_exposure(conn, session_id, kind, memory_id, exposed_at="2026-05-11T11:00:00+00:00"):
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES (?, ?, ?, ?, 'bootstrap')",
        (session_id, kind, memory_id, exposed_at),
    )
    conn.commit()


def _seed_reflection(conn, rid="r1"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid,),
    )
    conn.commit()


def _seed_semantic(conn, sid="s1"):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,),
    )
    conn.commit()


class TestCreditOneClassEffects:
    def test_cited_bumps_useful_count_on_reflection(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": "cited", "skipped": None}
        row = conn.execute(
            "SELECT useful_count, last_useful_at FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1
        assert row["last_useful_at"] == "2026-05-11T12:00:00+00:00"

    def test_shaped_bumps_useful_count_on_semantic(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_semantic(conn, "s1")
        _seed_exposure(conn, "S1", "semantic", "s1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="semantic", id="s1", classification="shaped",
        )
        row = conn.execute(
            "SELECT useful_count FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert row["useful_count"] == 1

    def test_misled_bumps_times_misled(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="misled",
        )
        row = conn.execute(
            "SELECT useful_count, times_misled, last_misled_at "
            "FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0
        assert row["times_misled"] == 1
        assert row["last_misled_at"] == "2026-05-11T12:00:00+00:00"

    def test_credit_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        row = conn.execute(
            "SELECT rated_at, classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["rated_at"] == "2026-05-11T12:00:00+00:00"
        assert row["classification"] == "cited"


class TestCreditOneSkips:
    def test_skip_not_exposed(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        # NO exposure row
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "not_exposed"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0

    def test_skip_already_rated(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        # second call is the no-op
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "already_rated"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1  # not double-bumped

    def test_skip_memory_missing(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        # exposure exists but reflection row does NOT
        _seed_exposure(conn, "S1", "reflection", "r-missing")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r-missing",
            classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_missing"}

    def test_skip_memory_retired(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        conn.execute(
            "UPDATE reflections SET status='retired' WHERE id='r1'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_retired"}

    def test_skip_memory_superseded(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        conn.execute(
            "UPDATE reflections SET status='superseded' WHERE id='r1'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_retired"}

    def test_kind_semantic_with_reflection_id_returns_memory_missing(
        self, conn, fixed_clock,
    ):
        """If caller passes kind='semantic' but the id only exists in
        reflections, the service looks in semantic_memories, finds
        nothing, and correctly returns memory_missing."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")  # exists in reflections only
        _seed_exposure(conn, "S1", "semantic", "r1")  # exposed with wrong kind
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="semantic", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_missing"}

    def test_two_exposure_rows_one_rated_one_unrated_succeeds(
        self, conn, fixed_clock,
    ):
        """If a memory has TWO exposure rows (bootstrap + retrieve, both
        valid per spec §5.3), and one is already rated while the other
        is unrated, credit_one must rate against the unrated row — not
        falsely return already_rated.

        This guards against a SQLite fetchone()-order regression.
        """
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        # Two exposure rows with distinct exposed_at — mimics bootstrap
        # then mid-session retrieve.
        _seed_exposure(conn, "S1", "reflection", "r1",
                       exposed_at="2026-05-11T10:00:00+00:00")
        _seed_exposure(conn, "S1", "reflection", "r1",
                       exposed_at="2026-05-11T11:00:00+00:00")
        # Mark ONLY the earlier row as rated.
        conn.execute(
            "UPDATE session_memory_exposure SET rated_at='2026-05-11T10:30:00+00:00', "
            "classification='cited' WHERE session_id='S1' AND memory_id='r1' "
            "AND exposed_at='2026-05-11T10:00:00+00:00'"
        )
        conn.commit()

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="shaped",
        )
        assert result == {"applied": "shaped", "skipped": None}

    def test_all_exposure_rows_rated_returns_already_rated(
        self, conn, fixed_clock,
    ):
        """If a memory has multiple exposure rows and ALL are rated,
        credit_one returns already_rated."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1",
                       exposed_at="2026-05-11T10:00:00+00:00")
        _seed_exposure(conn, "S1", "reflection", "r1",
                       exposed_at="2026-05-11T11:00:00+00:00")
        # Mark BOTH as rated.
        conn.execute(
            "UPDATE session_memory_exposure SET rated_at='x', classification='cited' "
            "WHERE session_id='S1' AND memory_id='r1'"
        )
        conn.commit()

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "already_rated"}


class TestCreditOneValidation:
    def test_ignored_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="ignored"):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="ignored",
            )

    def test_unknown_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="bogus"):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="bogus",
            )

    def test_unknown_kind_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="observation"):
            svc.credit_one(
                session_id="S1", kind="observation", id="o1", classification="cited",
            )


class TestApplySessionRatings:
    def test_each_class_produces_right_updates(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_semantic(conn, "s1")
        _seed_semantic(conn, "s2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        _seed_exposure(conn, "S1", "semantic",   "s1")
        _seed_exposure(conn, "S1", "semantic",   "s2")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
                {"kind": "semantic",   "id": "s1", "class": "shaped"},
                {"kind": "semantic",   "id": "s2", "class": "misled"},
            ],
        )
        assert result["session_id"] == "S1"
        assert result["applied"] == {
            "cited": 1, "shaped": 1, "ignored": 1, "misled": 1,
        }
        assert all(v == 0 for v in result["skipped"].values())

        # Verify the actual columns.
        r1 = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r1'"
        ).fetchone()
        assert r1["useful_count"] == 1
        r2 = conn.execute(
            "SELECT useful_count, times_misled FROM reflections WHERE id='r2'"
        ).fetchone()
        assert r2["useful_count"] == 0  # ignored is a no-op on memory
        s1 = conn.execute(
            "SELECT useful_count FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert s1["useful_count"] == 1
        s2 = conn.execute(
            "SELECT times_misled FROM semantic_memories WHERE id='s2'"
        ).fetchone()
        assert s2["times_misled"] == 1

    def test_ignored_still_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.apply_session_ratings(
            session_id="S1",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored"}],
        )
        row = conn.execute(
            "SELECT rated_at, classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["rated_at"] is not None
        assert row["classification"] == "ignored"

    def test_all_four_skip_counts_exercised(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        # r1: exists + not exposed
        _seed_reflection(conn, "r1")
        # r2: exists + exposed + already rated
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r2")
        conn.execute(
            "UPDATE session_memory_exposure SET rated_at='x' "
            "WHERE memory_id='r2'"
        )
        conn.commit()
        # r3: missing + exposed
        _seed_exposure(conn, "S1", "reflection", "r3")
        # r4: exists + retired + exposed
        _seed_reflection(conn, "r4")
        conn.execute(
            "UPDATE reflections SET status='retired' WHERE id='r4'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r4")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "cited"},
                {"kind": "reflection", "id": "r3", "class": "cited"},
                {"kind": "reflection", "id": "r4", "class": "cited"},
            ],
        )
        assert result["applied"] == {
            "cited": 0, "shaped": 0, "ignored": 0, "misled": 0,
        }
        assert result["skipped"] == {
            "not_exposed": 1, "already_rated": 1,
            "memory_missing": 1, "memory_retired": 1,
        }

    def test_empty_ratings_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="ratings"):
            svc.apply_session_ratings(session_id="S1", ratings=[])

    def test_empty_session_id_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="session_id"):
            svc.apply_session_ratings(
                session_id="",
                ratings=[{"kind": "reflection", "id": "r1", "class": "cited"}],
            )

    def test_duplicate_kind_id_in_batch_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="duplicate"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "cited"},
                    {"kind": "reflection", "id": "r1", "class": "ignored"},
                ],
            )

    def test_invalid_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[{"kind": "reflection", "id": "r1", "class": "bogus"}],
            )

    def test_invalid_kind_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="kind"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[{"kind": "observation", "id": "o1", "class": "cited"}],
            )

    def test_missing_kind_field_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="missing required field"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[{"id": "r1", "class": "cited"}],  # no "kind"
            )

    def test_savepoint_rollback_on_unexpected_error(
        self, conn, fixed_clock, monkeypatch,
    ):
        """If something inside the loop raises, no partial state should land."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")

        svc = MemoryRatingService(conn, clock=fixed_clock)
        # Patch _apply_one so r1 succeeds, r2 raises.
        original = svc._apply_one
        calls = {"n": 0}

        def patched(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return original(**kw)

        monkeypatch.setattr(svc, "_apply_one", patched)
        with pytest.raises(RuntimeError, match="boom"):
            svc.apply_session_ratings(
                session_id="S1",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "cited"},
                    {"kind": "reflection", "id": "r2", "class": "cited"},
                ],
            )

        # r1's bump should have been rolled back.
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0

    def test_credit_then_sweep_skips_already_rated(self, conn, fixed_clock):
        """Credit one mid-session, then sweep — credited row is skipped."""
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        # Sweep both ids; r1 should land in skipped.already_rated.
        result = svc.apply_session_ratings(
            session_id="S1",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "ignored"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
            ],
        )
        assert result["applied"]["ignored"] == 1
        assert result["skipped"]["already_rated"] == 1
        # r1 still has useful_count == 1 from the credit (not bumped to 2).
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1
