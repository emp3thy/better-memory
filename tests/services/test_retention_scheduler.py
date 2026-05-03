"""Unit tests for RetentionScheduler.

Tests use a controlled clock + a stub for RetentionService to keep
behaviour isolated from actual archive logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.retention import RetentionReport
from better_memory.services.retention_scheduler import RetentionScheduler


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    apply_migrations(c)
    yield c
    c.close()


def _seed_run(conn, *, run_at: datetime, triggered_by: str = "retrieve") -> None:
    """Insert a fake retention_runs row for the guard to see."""
    conn.execute(
        "INSERT INTO retention_runs (run_at, "
        "archived_via_retired_reflection, "
        "archived_via_consumed_without_reflection, "
        "archived_via_no_outcome_episode, pruned, triggered_by) "
        "VALUES (?, 0, 0, 0, 0, ?)",
        (run_at.isoformat(), triggered_by),
    )
    conn.commit()


def _empty_report() -> RetentionReport:
    return RetentionReport(
        archived_via_retired_reflection=0,
        archived_via_consumed_without_reflection=0,
        archived_via_no_outcome_episode=0,
        pruned=0,
    )


def test_first_call_runs_retention(conn) -> None:
    """No prior run → scheduler invokes RetentionService and records."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

    rows = conn.execute(
        "SELECT triggered_by FROM retention_runs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["triggered_by"] == "retrieve"


def test_within_guard_skips(conn) -> None:
    """Last run was 1 hour ago → scheduler is a no-op."""
    fake_now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
    _seed_run(conn, run_at=fake_now - timedelta(hours=1))

    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        sched = RetentionScheduler(
            conn, auto_prune=False, clock=lambda: fake_now
        )
        sched.maybe_run(triggered_by="retrieve")

        MockService.assert_not_called()
    rows = conn.execute("SELECT id FROM retention_runs").fetchall()
    assert len(rows) == 1  # the seeded row, no new row


def test_after_guard_runs_again(conn) -> None:
    """Last run was 25 hours ago → scheduler runs again."""
    fake_now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
    _seed_run(conn, run_at=fake_now - timedelta(hours=25))

    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(
            conn, auto_prune=False, clock=lambda: fake_now
        )
        sched.maybe_run(triggered_by="retrieve")

        MockService.assert_called_once()
    rows = conn.execute("SELECT id FROM retention_runs").fetchall()
    assert len(rows) == 2  # seeded + new


def test_records_triggered_by(conn) -> None:
    """The triggered_by string the caller passes is persisted."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="manual")

    row = conn.execute(
        "SELECT triggered_by FROM retention_runs"
    ).fetchone()
    assert row["triggered_by"] == "manual"


def test_records_counts_from_report(conn) -> None:
    """Counts from RetentionReport are persisted to the row."""
    report = RetentionReport(
        archived_via_retired_reflection=5,
        archived_via_consumed_without_reflection=3,
        archived_via_no_outcome_episode=2,
        pruned=7,
    )
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = report
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

    row = conn.execute(
        "SELECT * FROM retention_runs"
    ).fetchone()
    assert row["archived_via_retired_reflection"] == 5
    assert row["archived_via_consumed_without_reflection"] == 3
    assert row["archived_via_no_outcome_episode"] == 2
    assert row["pruned"] == 7


def test_auto_prune_false_passes_prune_false(conn) -> None:
    """auto_prune=False → RetentionService.run(prune=False)."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=False)
        sched.maybe_run(triggered_by="retrieve")

        call = MockService.return_value.run.call_args
        assert call.kwargs["prune"] is False


def test_auto_prune_true_passes_prune_true(conn) -> None:
    """auto_prune=True → RetentionService.run(prune=True)."""
    with patch(
        "better_memory.services.retention_scheduler.RetentionService"
    ) as MockService:
        MockService.return_value.run.return_value = _empty_report()
        sched = RetentionScheduler(conn, auto_prune=True)
        sched.maybe_run(triggered_by="retrieve")

        call = MockService.return_value.run.call_args
        assert call.kwargs["prune"] is True
