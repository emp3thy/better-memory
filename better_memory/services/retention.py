"""Observation retention — spec §9 archive rules + optional prune.

Retention is a manual MCP-invoked operation; there is no automatic
scheduling (spec §13). Reflections are never auto-deleted — this
module is observation-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RetentionReport:
    """Counts emitted by ``RetentionService.run``.

    ``archived_via_*`` count rows that transitioned from a non-archived
    status into ``archived`` during this run. The three rules can in
    principle target the same row; the SQL fires them in order, so a
    row that matches more than one rule is counted under the first
    matching rule and skipped by the rest.

    ``pruned`` counts archived rows hard-deleted when ``prune=True``.
    """

    archived_via_retired_reflection: int
    archived_via_consumed_without_reflection: int
    archived_via_no_outcome_episode: int
    pruned: int


class RetentionService:
    """Implements spec §9 retention rules.

    Methods:
    - ``run_archive(retention_days)`` — flip eligible observations to
      ``status='archived'`` per the four rules. Idempotent.
    - ``run(retention_days, prune, prune_age_days, dry_run)`` — wraps
      ``run_archive`` and optionally hard-deletes archived rows older
      than ``prune_age_days``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock

    # --------------------------------------------------------- public

    def run(
        self,
        *,
        retention_days: int = 90,
        prune: bool = False,
        prune_age_days: int = 365,
        dry_run: bool = False,
    ) -> RetentionReport:
        """Top-level entry: archive then optionally prune."""
        if dry_run:
            return self._dry_run(
                retention_days=retention_days,
                prune=prune,
                prune_age_days=prune_age_days,
            )

        archive_report = self.run_archive(retention_days=retention_days)
        pruned = 0
        if prune:
            pruned = self._prune(prune_age_days=prune_age_days)
        return RetentionReport(
            archived_via_retired_reflection=archive_report.archived_via_retired_reflection,
            archived_via_consumed_without_reflection=archive_report.archived_via_consumed_without_reflection,
            archived_via_no_outcome_episode=archive_report.archived_via_no_outcome_episode,
            pruned=pruned,
        )

    def run_archive(self, *, retention_days: int = 90) -> RetentionReport:
        """Apply the three archive rules. Returns counts."""
        threshold = (
            self._clock() - timedelta(days=retention_days)
        ).isoformat()
        now = self._clock().isoformat()

        a = self._archive_rule_a_retired_reflection(threshold, now)
        b = self._archive_rule_b_consumed_without_reflection(threshold, now)
        c = self._archive_rule_c_no_outcome_episode(threshold, now)
        self._conn.commit()

        return RetentionReport(
            archived_via_retired_reflection=a,
            archived_via_consumed_without_reflection=b,
            archived_via_no_outcome_episode=c,
            pruned=0,
        )

    # --------------------------------------------------------- private

    def _archive_rule_a_retired_reflection(
        self, threshold: str, now: str
    ) -> int:
        """Rule A: obs linked only to retired reflections, oldest
        retirement >= retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE id IN (
                SELECT o.id
                FROM observations o
                WHERE o.status != 'archived'
                  AND EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      WHERE rs.observation_id = o.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                        AND r.status != 'retired'
                  )
                  AND (
                      SELECT MAX(r.updated_at)
                      FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                  ) <= ?
            )
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _archive_rule_b_consumed_without_reflection(
        self, threshold: str, now: str
    ) -> int:
        """Rule B: status=consumed_without_reflection AND
        status_changed_at >= retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE status = 'consumed_without_reflection'
              AND status_changed_at <= ?
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _archive_rule_c_no_outcome_episode(
        self, threshold: str, now: str
    ) -> int:
        """Rule C: episode.outcome='no_outcome' AND ended_at >=
        retention_days old."""
        cursor = self._conn.execute(
            """
            UPDATE observations
            SET status = 'archived', status_changed_at = ?
            WHERE status != 'archived'
              AND episode_id IN (
                  SELECT id FROM episodes
                  WHERE outcome = 'no_outcome'
                    AND ended_at IS NOT NULL
                    AND ended_at <= ?
              )
            """,
            (now, threshold),
        )
        return cursor.rowcount or 0

    def _prune(self, *, prune_age_days: int) -> int:
        """Hard-delete archived rows older than prune_age_days.

        Only prunes observations with NO reflection_sources rows.
        Sourced observations stay archived but undeleted (their
        evidence trail is preserved for audit). This is conservative
        but correct — the spec doesn't require pruning sourced rows.

        The FTS5 observation_fts table is kept in sync by an AFTER
        DELETE trigger on observations. The vec0 observation_embeddings
        virtual table has no such trigger, so this method deletes
        embedding rows explicitly before removing the observations.
        """
        threshold = (
            self._clock() - timedelta(days=prune_age_days)
        ).isoformat()

        eligible_ids = [
            r["id"]
            for r in self._conn.execute(
                """
                SELECT id FROM observations
                WHERE status = 'archived'
                  AND status_changed_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      WHERE rs.observation_id = observations.id
                  )
                """,
                (threshold,),
            ).fetchall()
        ]
        if not eligible_ids:
            return 0

        placeholders = ", ".join("?" * len(eligible_ids))
        # Delete embeddings first (no AFTER DELETE trigger on the vec0 table).
        self._conn.execute(
            f"DELETE FROM observation_embeddings "
            f"WHERE observation_id IN ({placeholders})",
            eligible_ids,
        )
        # Then delete the observations themselves (FTS5 trigger handles the
        # observation_fts cleanup automatically).
        cursor = self._conn.execute(
            f"DELETE FROM observations WHERE id IN ({placeholders})",
            eligible_ids,
        )
        self._conn.commit()
        return cursor.rowcount or 0

    def _dry_run(
        self, *, retention_days: int, prune: bool, prune_age_days: int,
    ) -> RetentionReport:
        """Run select-only versions of all rules; commit nothing.

        Mirrors run_archive's rule ordering: a row that matches multiple
        rules is counted under the first matching rule only.
        """
        threshold = (
            self._clock() - timedelta(days=retention_days)
        ).isoformat()

        # Rule A — linked-only-to-retired.
        rule_a_ids = {
            r["id"]
            for r in self._conn.execute(
                """
                SELECT o.id
                FROM observations o
                WHERE o.status != 'archived'
                  AND EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      WHERE rs.observation_id = o.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                        AND r.status != 'retired'
                  )
                  AND (
                      SELECT MAX(r.updated_at)
                      FROM reflection_sources rs
                      JOIN reflections r ON r.id = rs.reflection_id
                      WHERE rs.observation_id = o.id
                  ) <= ?
                """,
                (threshold,),
            ).fetchall()
        }

        # Rule B — consumed_without_reflection.
        rule_b_ids = {
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM observations "
                "WHERE status = 'consumed_without_reflection' "
                "AND status_changed_at <= ?",
                (threshold,),
            ).fetchall()
        } - rule_a_ids

        # Rule C — no_outcome episode.
        rule_c_ids = {
            r["id"]
            for r in self._conn.execute(
                """
                SELECT id FROM observations
                WHERE status != 'archived'
                  AND episode_id IN (
                      SELECT id FROM episodes
                      WHERE outcome = 'no_outcome'
                        AND ended_at IS NOT NULL
                        AND ended_at <= ?
                  )
                """,
                (threshold,),
            ).fetchall()
        } - rule_a_ids - rule_b_ids

        pruned = 0
        if prune:
            now = self._clock().isoformat()
            prune_threshold = (
                self._clock() - timedelta(days=prune_age_days)
            ).isoformat()
            # Currently-archived rows that satisfy the prune predicate.
            already_archived_pruneable = {
                r["id"]
                for r in self._conn.execute(
                    """
                    SELECT id FROM observations
                    WHERE status = 'archived'
                      AND status_changed_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM reflection_sources rs
                          WHERE rs.observation_id = observations.id
                      )
                    """,
                    (prune_threshold,),
                ).fetchall()
            }

            # In a real run, run_archive flips eligible rows to 'archived'
            # with status_changed_at = now BEFORE _prune sees them. If
            # `now <= prune_threshold` (i.e. prune_age_days <= 0), those
            # newly-archived rows ALSO satisfy the prune predicate.
            # Mirror that here so dry-run counts match real-run counts.
            if now <= prune_threshold:
                # Rules B and C bind the row's status to 'archived' with
                # the row's own status_changed_at = now — same as run_archive.
                # Filter out any that have reflection_sources (prune guard).
                # Only B's source rows can have reflection_sources removed
                # (Rule A rows BY DEFINITION have at least one reflection_source
                # and are excluded from prune by the NOT EXISTS guard);
                # so include only B and C ids that lack reflection_sources.
                candidate_ids = (rule_b_ids | rule_c_ids) - already_archived_pruneable
                if candidate_ids:
                    placeholders = ", ".join("?" * len(candidate_ids))
                    sourced = {
                        r["observation_id"]
                        for r in self._conn.execute(
                            f"SELECT DISTINCT observation_id FROM reflection_sources "
                            f"WHERE observation_id IN ({placeholders})",
                            list(candidate_ids),
                        ).fetchall()
                    }
                    already_archived_pruneable |= candidate_ids - sourced

            pruned = len(already_archived_pruneable)

        return RetentionReport(
            archived_via_retired_reflection=len(rule_a_ids),
            archived_via_consumed_without_reflection=len(rule_b_ids),
            archived_via_no_outcome_episode=len(rule_c_ids),
            pruned=pruned,
        )
