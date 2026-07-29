from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.cli.backfill_embeddings import backfill
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, status="pending_review"):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at, status)
           VALUES (?, 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?)""", (rid, status))
    conn.commit()


def _seed_semantic(conn, sid):
    conn.execute(
        """INSERT INTO semantic_memories (id, content, project, scope,
           created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,))
    conn.commit()


def test_backfills_reflections_and_semantics(conn):
    _seed_reflection(conn, "r1")
    _seed_semantic(conn, "s1")
    stats = backfill(conn, FakeEmbedder())
    assert stats == {"reflections": 1, "semantics": 1, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0] == 1


def test_idempotent(conn):
    _seed_reflection(conn, "r1")
    fake = FakeEmbedder()
    backfill(conn, fake)
    assert backfill(conn, fake) == {"reflections": 0, "semantics": 0, "skipped": 0}


def test_retired_reflections_skipped(conn):
    _seed_reflection(conn, "r1", status="retired")
    assert backfill(conn, FakeEmbedder()) == {
        "reflections": 0, "semantics": 0, "skipped": 0}


def test_embed_failure_counted_as_skipped(conn):
    _seed_reflection(conn, "r1")
    stats = backfill(conn, FakeEmbedder(fail=True))
    assert stats == {"reflections": 0, "semantics": 0, "skipped": 1}


def test_insert_error_on_one_row_does_not_kill_the_whole_batch(conn):
    """A per-row INSERT failure (e.g. UNIQUE collision from a concurrent
    self-heal writer landing between our snapshot and our INSERT, or a plain
    lock) must be counted as skipped rather than aborting the entire run.
    Before the fix the exception propagated and rolled back every row we'd
    already written this run."""
    import sqlite3

    _seed_reflection(conn, "r1")
    _seed_reflection(conn, "r2")

    class _FlakyConn:
        """Proxy the real connection but fail the INSERT for r1 only.

        sqlite3.Connection disallows attribute assignment, so monkeypatch
        doesn't work — mirror test_spool.py's _ExplodingCommitConn trick."""

        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("INSERT INTO reflection_embeddings") and (
                args and args[0] and args[0][0] == "r1"
            ):
                self.calls += 1
                raise sqlite3.IntegrityError("UNIQUE constraint failed")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    proxy = _FlakyConn(conn)
    stats = backfill(proxy, FakeEmbedder())
    # r1 failed → skipped; r2 landed.
    assert stats["reflections"] == 1
    assert stats["skipped"] == 1
    assert proxy.calls == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM reflection_embeddings"
    ).fetchone()[0] == 1


def test_failed_insert_does_not_destroy_race_landed_pre_existing_embedding(
    conn,
):
    """The precise race #101 defends against: SELECT-snapshot picks r1
    (no embedding), a concurrent memory.retrieve self-heal writes r1's
    embedding, then backfill's DELETE-then-INSERT arrives. If the INSERT
    fails, the DELETE must be rolled back — otherwise the batch-level
    commit() would durably erase the race-landed embedding, leaving r1
    with no vector at all. The per-row SAVEPOINT is what keeps that from
    happening.
    """
    import sqlite3

    import sqlite_vec

    _seed_reflection(conn, "r1")
    _seed_reflection(conn, "r2")

    class _RacyConn:
        """Simulate the self-heal race: after backfill's SELECT-missing
        picks up r1, insert r1's embedding via a side channel (the
        concurrent writer), then fail backfill's own INSERT for r1."""

        def __init__(self, inner):
            self._inner = inner
            self._race_landed = False

        def execute(self, sql, *args, **kwargs):
            result = self._inner.execute(sql, *args, **kwargs)
            # After the SELECT-missing snapshot, land the race row.
            if (
                not self._race_landed
                and sql.lstrip().upper().startswith("SELECT R.ID")
            ):
                self._race_landed = True
                self._inner.execute(
                    "INSERT INTO reflection_embeddings "
                    "(reflection_id, embedding) VALUES (?, ?)",
                    ("r1", sqlite_vec.serialize_float32([0.5] * 768)),
                )
                self._inner.commit()
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    # Also fail r1's INSERT after DELETE — simulates the UNIQUE collision
    # the self-heal race can raise on real sqlite-vec builds. Compose the
    # two proxies.
    class _AlsoFailR1Insert:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("INSERT INTO reflection_embeddings") and (
                args and args[0] and args[0][0] == "r1"
            ):
                raise sqlite3.IntegrityError("UNIQUE constraint failed")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    proxy = _AlsoFailR1Insert(_RacyConn(conn))
    stats = backfill(proxy, FakeEmbedder())

    assert stats["reflections"] == 1  # r2
    assert stats["skipped"] == 1  # r1
    # The pre-existing race-landed embedding for r1 must have survived
    # the failed DELETE-then-INSERT via the per-row SAVEPOINT rollback.
    r1_rows = conn.execute(
        "SELECT COUNT(*) FROM reflection_embeddings WHERE reflection_id = 'r1'"
    ).fetchone()[0]
    assert r1_rows == 1, (
        "SAVEPOINT rollback failed: race-landed r1 embedding was durably "
        "erased by the batch commit"
    )
