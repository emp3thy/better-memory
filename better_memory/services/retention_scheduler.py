"""24h-guarded retention runner.

Wraps RetentionService with a "has it run in the last 24h?" check
and an audit-trail row in retention_runs. Caller (memory.retrieve)
invokes maybe_run after spool drain; the timestamp guard ensures
retention runs at most once per 24h.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from better_memory import _diag
from better_memory.services.retention import RetentionReport, RetentionService

_RETENTION_DAYS = 90
_PRUNE_AGE_DAYS = 365
_GUARD_HOURS = 24


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RetentionScheduler:
    """24h-guarded wrapper around RetentionService."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        auto_prune: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._auto_prune = auto_prune
        self._clock: Callable[[], datetime] = clock or _default_clock

    def maybe_run(self, *, triggered_by: str) -> None:
        """Run retention IF >24h since last run. Records to retention_runs.

        If two callers race past the guard within ~50ms, both will run
        RetentionService.run() and both write a row. SQLite serializes;
        the second is essentially free (UPDATE matches zero new rows).
        Documented in the design as accepted.
        """
        fn = "RetentionScheduler.maybe_run"
        with _diag.trace(fn, triggered_by=triggered_by):
            _diag.step(fn, "check_guard")
            if self._too_soon():
                _diag.step(fn, "skipped_within_24h_guard")
                return
            _diag.step(fn, "running_retention")
            report = RetentionService(self._conn).run(
                retention_days=_RETENTION_DAYS,
                prune=self._auto_prune,
                prune_age_days=_PRUNE_AGE_DAYS,
            )
            _diag.step(fn, "recording_run")
            self._record_run(report, triggered_by=triggered_by)
            _diag.step(fn, "done")

    def _too_soon(self) -> bool:
        threshold = (
            self._clock() - timedelta(hours=_GUARD_HOURS)
        ).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM retention_runs WHERE run_at > ? LIMIT 1",
            (threshold,),
        ).fetchone()
        return row is not None

    def _record_run(
        self, report: RetentionReport, *, triggered_by: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO retention_runs (run_at, "
            "archived_via_retired_reflection, "
            "archived_via_consumed_without_reflection, "
            "archived_via_no_outcome_episode, pruned, triggered_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._clock().isoformat(),
                report.archived_via_retired_reflection,
                report.archived_via_consumed_without_reflection,
                report.archived_via_no_outcome_episode,
                report.pruned,
                triggered_by,
            ),
        )
        self._conn.commit()
