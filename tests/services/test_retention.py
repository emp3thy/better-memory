"""Tests for RetentionService — spec §9 archive rules + prune."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionReport, RetentionService


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
    fixed = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_episode(conn, *, ep_id: str, project: str,
                  outcome: str | None = None,
                  ended_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, ended_at, outcome, "
        "close_reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ep_id, project, "2026-04-01T00:00:00+00:00", ended_at, outcome,
         "abandoned" if outcome == "abandoned" else None),
    )
    conn.commit()


def _seed_observation(
    conn, *, obs_id: str, ep_id: str, project: str = "proj-a",
    status: str = "active",
    status_changed_at: str = "2026-04-01T00:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, episode_id, status, "
        "created_at, status_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (obs_id, f"content {obs_id}", project, ep_id, status,
         status_changed_at, status_changed_at),
    )
    conn.commit()


def _seed_reflection(
    conn, *, refl_id: str, project: str = "proj-a",
    status: str = "confirmed",
    updated_at: str = "2026-04-01T00:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'general', 'do', 'u', '[]', 0.7, ?, ?, ?)",
        (refl_id, f"title-{refl_id}", project, status,
         "2026-04-01T00:00:00+00:00", updated_at),
    )
    conn.commit()


def _link(conn, refl_id: str, obs_id: str) -> None:
    conn.execute(
        "INSERT INTO reflection_sources (reflection_id, observation_id) "
        "VALUES (?, ?)", (refl_id, obs_id),
    )
    conn.commit()


class TestRuleAObsLinkedOnlyToRetiredReflection:
    """Spec §9: observations linked only to retired reflections, archived
    90 days after the reflection retired."""

    def test_archives_when_only_link_is_retired_and_old(
        self, conn, fixed_clock
    ):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        # Retired 100 days before fixed_clock (2026-08-01) = 2026-04-23.
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-04-23T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        svc = RetentionService(conn, clock=fixed_clock)
        report = svc.run_archive(retention_days=90)

        assert report.archived_via_retired_reflection == 1
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "archived"

    def test_keeps_when_retirement_is_recent(self, conn, fixed_clock):
        # Same setup but retired 30 days before the clock — under the
        # 90-day threshold, so keep.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-07-02T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_retired_reflection == 0
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"

    def test_keeps_when_also_linked_to_confirmed_reflection(
        self, conn, fixed_clock
    ):
        # Spec §9 rule 2: "Observations linked to non-retired reflections
        # kept indefinitely." Even if ONE of the linked reflections is
        # confirmed, don't archive.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r-retired", status="retired",
                         updated_at="2026-04-23T00:00:00+00:00")
        _seed_reflection(conn, refl_id="r-confirmed", status="confirmed",
                         updated_at="2026-04-23T00:00:00+00:00")
        _link(conn, "r-retired", "obs-1")
        _link(conn, "r-confirmed", "obs-1")

        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_retired_reflection == 0


class TestRuleBConsumedWithoutReflection:
    """Spec §9: observations with status=consumed_without_reflection
    archived 90 days after consumption."""

    def test_archives_when_consumption_is_old(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="consumed_without_reflection",
            status_changed_at="2026-04-01T00:00:00+00:00",  # 122 days old
        )
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_consumed_without_reflection == 1

    def test_keeps_when_consumption_is_recent(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="consumed_without_reflection",
            status_changed_at="2026-07-15T00:00:00+00:00",  # 17 days old
        )
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_consumed_without_reflection == 0


class TestRuleCNoOutcomeEpisode:
    """Spec §9: observations in no_outcome episodes archived 90 days
    after the episode closed."""

    def test_archives_when_episode_closed_long_ago(self, conn, fixed_clock):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 1

    def test_keeps_when_episode_outcome_not_no_outcome(
        self, conn, fixed_clock
    ):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="abandoned", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0

    def test_keeps_when_episode_still_open(self, conn, fixed_clock):
        # ended_at IS NULL — episode hasn't closed.
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome=None, ended_at=None,
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1", status="active")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0


class TestArchivedRowsAreIdempotent:
    def test_already_archived_obs_not_recounted(self, conn, fixed_clock):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="archived")
        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_no_outcome_episode == 0


class TestPrune:
    def test_prune_off_does_not_delete(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-1", ep_id="e1",
            status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",  # 1.5 years old
        )
        # We need to break the FK chain to allow pruning — the test
        # expects nothing pruned with prune=False (default).
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=False
        )
        assert report.pruned == 0
        row = conn.execute(
            "SELECT id FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row is not None  # not deleted

    def test_prune_on_deletes_old_archived_rows(self, conn, fixed_clock):
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-old", ep_id="e1", status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",  # 1.5 years
        )
        _seed_observation(
            conn, obs_id="obs-recent", ep_id="e1", status="archived",
            status_changed_at="2026-06-01T00:00:00+00:00",  # 2 months
        )
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=365,
        )
        assert report.pruned == 1
        rows = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM observations"
            ).fetchall()
        }
        assert "obs-old" not in rows  # deleted
        assert "obs-recent" in rows  # kept (under prune_age_days)


class TestDryRun:
    def test_dry_run_returns_counts_without_writing(
        self, conn, fixed_clock
    ):
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="active")
        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, dry_run=True
        )
        assert report.archived_via_no_outcome_episode == 1
        # But the observation status didn't actually change.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-1'"
        ).fetchone()
        assert row["status"] == "active"


class TestIdempotency:
    def test_run_archive_twice_produces_zero_archives_second_time(
        self, conn, fixed_clock
    ):
        """Running retention twice in a row should produce zero archives
        on the second pass — all eligible rows were archived in pass 1."""
        # Set up rows matching all three rules.
        # Rule A: linked-only-to-retired.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(conn, obs_id="obs-a", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r-a", status="retired",
                         updated_at="2026-04-23T00:00:00+00:00")
        _link(conn, "r-a", "obs-a")
        # Rule B: consumed_without_reflection, old.
        _seed_observation(
            conn, obs_id="obs-b", ep_id="e1",
            status="consumed_without_reflection",
            status_changed_at="2026-04-01T00:00:00+00:00",
        )
        # Rule C: no_outcome episode.
        _seed_episode(
            conn, ep_id="e2", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-c", ep_id="e2",
                          status="active")

        svc = RetentionService(conn, clock=fixed_clock)
        first = svc.run_archive(retention_days=90)
        assert (
            first.archived_via_retired_reflection
            + first.archived_via_consumed_without_reflection
            + first.archived_via_no_outcome_episode
        ) == 3

        second = svc.run_archive(retention_days=90)
        assert second.archived_via_retired_reflection == 0
        assert second.archived_via_consumed_without_reflection == 0
        assert second.archived_via_no_outcome_episode == 0


class TestMultiRuleOverlap:
    def test_row_matching_a_and_c_counted_under_a_only(
        self, conn, fixed_clock
    ):
        """An obs whose only reflection is retired AND whose episode
        is no_outcome matches both Rule A and Rule C. It should be
        counted under Rule A (the first matching rule)."""
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-04-01T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        report = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert report.archived_via_retired_reflection == 1
        assert report.archived_via_no_outcome_episode == 0

    def test_dry_run_matches_run_archive_under_overlap(
        self, conn, fixed_clock
    ):
        """Dry-run counts must match run_archive counts for the same
        DB state. (Regression test for the dry-run-over-counts bug.)"""
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-1", ep_id="e1",
                          status="consumed_into_reflection")
        _seed_reflection(conn, refl_id="r1", status="retired",
                         updated_at="2026-04-01T00:00:00+00:00")
        _link(conn, "r1", "obs-1")

        # Dry run first.
        dry = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, dry_run=True
        )
        # Real run on a fresh state would archive once — counts must
        # match dry-run.
        real = RetentionService(conn, clock=fixed_clock).run_archive(
            retention_days=90
        )
        assert dry.archived_via_retired_reflection == real.archived_via_retired_reflection
        assert dry.archived_via_consumed_without_reflection == real.archived_via_consumed_without_reflection
        assert dry.archived_via_no_outcome_episode == real.archived_via_no_outcome_episode


class TestDryRunPruneAtZeroAge:
    """Regression: when prune_age_days=0, a real run archives + immediately
    prunes the same row. Dry-run must report the same prune count, not
    miss the rows that would be archived during the run."""

    def test_dry_run_includes_newly_archivable_rows_at_zero_prune_age(
        self, conn, fixed_clock
    ):
        # Rule C: no_outcome episode closed long ago. Real run would
        # archive obs-c then prune it (since prune_age_days=0 means
        # any row archived at now satisfies status_changed_at <= now).
        _seed_episode(
            conn, ep_id="e1", project="proj-a",
            outcome="no_outcome", ended_at="2026-04-01T00:00:00+00:00",
        )
        _seed_observation(conn, obs_id="obs-c", ep_id="e1", status="active")

        svc = RetentionService(conn, clock=fixed_clock)
        dry = svc.run(retention_days=90, prune=True, prune_age_days=0,
                      dry_run=True)
        # Dry-run must predict 1 prune. Without the fix this would be 0.
        assert dry.pruned == 1
        assert dry.archived_via_no_outcome_episode == 1

        # Confirm the real run produces the same prune count.
        real = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=0,
        )
        assert real.pruned == 1
        assert real.archived_via_no_outcome_episode == 1


class TestPruneCleansEmbeddings:
    def test_prune_deletes_corresponding_embeddings_row(
        self, conn, fixed_clock
    ):
        """When _prune deletes an observation, its row in
        observation_embeddings must also go — vec0 has no DELETE
        trigger so retention has to clean up explicitly."""
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-old", ep_id="e1", status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",
        )
        # Insert a fake embedding row keyed to obs-old.
        # vec0 syntax: a 768-dim FLOAT vector. Use a literal.
        import struct
        embedding = struct.pack("768f", *(0.0 for _ in range(768)))
        conn.execute(
            "INSERT INTO observation_embeddings (observation_id, embedding) "
            "VALUES (?, ?)",
            ("obs-old", embedding),
        )
        conn.commit()

        # Confirm setup.
        emb_before = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_embeddings "
            "WHERE observation_id = 'obs-old'"
        ).fetchone()["n"]
        assert emb_before == 1

        report = RetentionService(conn, clock=fixed_clock).run(
            retention_days=90, prune=True, prune_age_days=365,
        )
        assert report.pruned == 1

        emb_after = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_embeddings "
            "WHERE observation_id = 'obs-old'"
        ).fetchone()["n"]
        assert emb_after == 0  # embeddings row must be gone too


class TestRetentionAtomicity:
    """Multi-statement archive/prune operations must roll back as a
    unit on failure. Without a SAVEPOINT, partial UPDATEs sit in the
    implicit SQLite transaction and would be persisted by the next
    ``conn.commit()`` from any service sharing the connection.
    """

    def test_archive_rolls_back_partial_state_when_a_rule_raises(
        self, conn, fixed_clock
    ):
        # Rule A would archive obs-A; we'll then make Rule B raise.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-A", ep_id="e1",
            status_changed_at="2025-01-01T00:00:00+00:00",
        )
        _seed_reflection(
            conn, refl_id="r-old", status="retired",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        _link(conn, "r-old", "obs-A")

        svc = RetentionService(conn, clock=fixed_clock)

        def _boom(*_a, **_k):
            raise RuntimeError("simulated rule_b failure")
        # Monkey-patch rule B to raise AFTER rule A's UPDATE has run.
        svc._archive_rule_b_consumed_without_reflection = _boom

        with pytest.raises(RuntimeError, match="simulated rule_b failure"):
            svc.run_archive(retention_days=90)

        # Simulate any other service committing the connection later.
        conn.commit()

        # Rule A's UPDATE must be rolled back — obs-A still 'active'.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'obs-A'"
        ).fetchone()
        assert row["status"] == "active"

    def test_prune_rolls_back_when_observation_delete_raises(
        self, conn, fixed_clock
    ):
        # Seed an archived obs old enough to prune, plus its embedding row.
        _seed_episode(conn, ep_id="e1", project="proj-a")
        _seed_observation(
            conn, obs_id="obs-old", ep_id="e1", status="archived",
            status_changed_at="2025-01-01T00:00:00+00:00",
        )
        import struct
        embedding = struct.pack("768f", *(0.0 for _ in range(768)))
        conn.execute(
            "INSERT INTO observation_embeddings (observation_id, embedding) "
            "VALUES (?, ?)", ("obs-old", embedding),
        )
        conn.commit()

        svc = RetentionService(conn, clock=fixed_clock)

        # Make the observations DELETE raise AFTER the embeddings DELETE
        # has already succeeded.
        def _boom(_ids):
            raise RuntimeError("simulated observation delete failure")
        svc._delete_observations = _boom  # type: ignore[method-assign]

        with pytest.raises(
            RuntimeError, match="simulated observation delete failure"
        ):
            svc.run(retention_days=90, prune=True, prune_age_days=365)

        # Simulate any other service committing the connection later.
        conn.commit()

        # The embeddings DELETE must have been rolled back too.
        emb_count = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_embeddings "
            "WHERE observation_id = 'obs-old'"
        ).fetchone()["n"]
        obs_count = conn.execute(
            "SELECT COUNT(*) AS n FROM observations "
            "WHERE id = 'obs-old'"
        ).fetchone()["n"]
        assert emb_count == 1, "embedding row must survive rollback"
        assert obs_count == 1, "observation row must survive rollback"
