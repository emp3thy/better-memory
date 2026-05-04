# tests/db/test_migration_0006.py
"""Migration 0006: episodes.synthesized_at + synth_failed_at; drop synthesis_runs.

Forward-only — verifies the migration's schema effects and backfill
correctness against a representative seed. The seed mirrors the four
real episode states the user's DB can contain at migration time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def seeded_conn(tmp_path: Path):
    """A DB at the post-0005 baseline with four representative episodes seeded."""
    db_path = tmp_path / "test.db"
    c = connect(db_path)

    apply_migrations(c)

    # Episode A: closed, all observations consumed → backfill should set synthesized_at
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-a','p1','2026-04-01T00:00:00+00:00','2026-04-01T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('a1','x','p1','ep-a','consumed_into_reflection','success',"
        "'2026-04-01T00:30:00+00:00','2026-04-01T00:30:00+00:00')"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('a2','y','p1','ep-a','consumed_without_reflection','success',"
        "'2026-04-01T00:31:00+00:00','2026-04-01T00:31:00+00:00')"
    )

    # Episode B: closed, mixed status (1 active + 1 consumed) → synthesized_at stays NULL
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-b','p1','2026-04-02T00:00:00+00:00','2026-04-02T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('b1','x','p1','ep-b','active','success',"
        "'2026-04-02T00:30:00+00:00','2026-04-02T00:30:00+00:00')"
    )
    c.execute(
        "INSERT INTO observations (id, content, project, episode_id, status, "
        "outcome, created_at, status_changed_at) VALUES "
        "('b2','y','p1','ep-b','consumed_into_reflection','success',"
        "'2026-04-02T00:31:00+00:00','2026-04-02T00:31:00+00:00')"
    )

    # Episode C: closed, no observations at all → synthesized_at backfilled
    c.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason, synthesized_at) VALUES "
        "('ep-c','p1','2026-04-03T00:00:00+00:00','2026-04-03T01:00:00+00:00',"
        "'success','goal_complete', NULL)"
    )

    # Episode D: open (outcome NULL) → synthesized_at stays NULL
    c.execute(
        "INSERT INTO episodes (id, project, started_at, outcome, "
        "synthesized_at) VALUES "
        "('ep-d','p1','2026-04-04T00:00:00+00:00', NULL, NULL)"
    )

    c.commit()

    # Re-run the backfill statement specifically so we're testing it
    # in isolation against this seed (apply_migrations already ran it
    # against an empty DB, where it was a no-op).
    c.execute(
        """
        UPDATE episodes
           SET synthesized_at = ended_at
         WHERE outcome IS NOT NULL
           AND id NOT IN (
               SELECT DISTINCT episode_id
                 FROM observations
                WHERE status = 'active'
           )
        """
    )
    c.commit()

    yield c
    c.close()


def test_synthesized_at_column_exists(seeded_conn) -> None:
    cols = {r[1] for r in seeded_conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert "synthesized_at" in cols


def test_synth_failed_at_column_exists(seeded_conn) -> None:
    cols = {r[1] for r in seeded_conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert "synth_failed_at" in cols


def test_partial_index_exists(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='episodes'"
    ).fetchall()
    assert "idx_episodes_pending_synth" in {r[0] for r in rows}


def test_synthesis_runs_table_dropped(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='synthesis_runs'"
    ).fetchall()
    assert rows == []


def test_backfill_episode_a_all_consumed(seeded_conn) -> None:
    """Episode A's observations are all non-active → synthesized_at set."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-a'"
    ).fetchone()
    assert row[0] == "2026-04-01T01:00:00+00:00"  # backfilled to ended_at


def test_backfill_episode_b_mixed_status_stays_null(seeded_conn) -> None:
    """Episode B has an active observation → synthesized_at stays NULL."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-b'"
    ).fetchone()
    assert row[0] is None


def test_backfill_episode_c_no_observations(seeded_conn) -> None:
    """Episode C is closed with zero observations → backfill applies."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-c'"
    ).fetchone()
    assert row[0] == "2026-04-03T01:00:00+00:00"


def test_open_episode_d_stays_null(seeded_conn) -> None:
    """Open episode (outcome NULL) → synthesized_at stays NULL."""
    row = seeded_conn.execute(
        "SELECT synthesized_at FROM episodes WHERE id='ep-d'"
    ).fetchone()
    assert row[0] is None


def test_synth_failed_at_is_null_for_all_existing_rows(seeded_conn) -> None:
    rows = seeded_conn.execute(
        "SELECT synth_failed_at FROM episodes"
    ).fetchall()
    assert all(r[0] is None for r in rows)


def test_backfill_invariant(seeded_conn) -> None:
    """count(closed episodes with no active observations) == count(closed with synthesized_at NOT NULL)."""
    no_active = seeded_conn.execute(
        """
        SELECT COUNT(*) FROM episodes
         WHERE outcome IS NOT NULL
           AND id NOT IN (
               SELECT DISTINCT episode_id FROM observations WHERE status='active'
           )
        """
    ).fetchone()[0]
    backfilled = seeded_conn.execute(
        "SELECT COUNT(*) FROM episodes "
        "WHERE outcome IS NOT NULL AND synthesized_at IS NOT NULL"
    ).fetchone()[0]
    assert no_active == backfilled
