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


def _seed_episode(conn, *, ep_id: str, project: str, outcome: str = None,
                  ended_at: str = None) -> None:
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
