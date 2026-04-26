"""Spool drain service.

The hook scripts (``better_memory.hooks.observer`` and
``better_memory.hooks.session_close``) deposit one JSON file per event into a
spool directory. They never touch the database. :class:`SpoolService` reads
those files, inserts corresponding rows into the ``hook_events`` table, and
either deletes the source files on success or moves malformed files to a
``.quarantine/`` subdirectory for later inspection.

Design rules
------------
* Per-file try/except so one bad payload does not block the whole drain.
* Idempotent — a second call with no new files returns ``DrainReport(0, 0)``.
* Top-level glob only — the ``.quarantine`` subdirectory is never re-scanned.
* Commit once per batch; the ``hook_events`` table is append-only and each row
  is independently meaningful, so per-file rollbacks have no semantic value.
* Commit-before-unlink: spool files are only deleted after the batch commit
  succeeds. If ``commit()`` raises (disk full, lock held, etc.) the files
  remain in the spool and a subsequent drain retries them. Data integrity
  takes precedence over cleanup — a row must never be ``lost`` because its
  file was deleted before the transaction was durable.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from better_memory.config import get_config
from better_memory.services.episode import EpisodeService

# Fields the spool file must contain for us to consider it well-formed.
# Everything else is optional — the schema allows NULLs on ``tool``, ``file``,
# ``content_snippet``, ``cwd`` and ``session_id``.
_REQUIRED_FIELDS: tuple[str, ...] = ("event_type", "timestamp")


@dataclass(frozen=True)
class DrainReport:
    """Outcome of a single :meth:`SpoolService.drain` call."""

    drained: int
    quarantined: int


class SpoolService:
    """Drain spool files into the ``hook_events`` table.

    Connection ownership
    --------------------
    Like the other write-path services, ``SpoolService`` owns the provided
    :class:`sqlite3.Connection` for the duration of :meth:`drain` and commits
    once the batch has been processed.

    When an ``EpisodeService`` is injected via the ``episodes`` kwarg, drain
    also calls ``episodes.open_background(session_id, project)`` for each
    ``session_start`` event it processes. Idempotent: skipped if the session
    already has an active episode. Guarded by per-event try/except so the
    side-effect can never cause drain to lose data. ``episodes=None`` (the
    default) preserves Phase 1/2 behaviour exactly.

    For ``commit_close`` events (Phase 4: opt-in post-commit hook), drain
    calls ``episodes.close_active(session_id=..., outcome='success',
    close_reason='goal_complete')``. Idempotent: if no active episode exists
    for the session the ValueError is swallowed so drain stays resilient
    against stale or duplicate markers.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        spool_dir: Path | None = None,
        *,
        episodes: EpisodeService | None = None,
    ) -> None:
        self._conn = conn
        self._spool_dir = (
            Path(spool_dir) if spool_dir is not None else get_config().spool_dir
        )
        self._episodes = episodes

    # ------------------------------------------------------------------ public
    def drain(self) -> DrainReport:
        """Read every top-level ``*.json`` file, insert rows, delete files.

        Malformed files (bad JSON, missing required fields, insert error) are
        moved to ``<spool>/.quarantine/`` under their original name.

        The method runs in three passes so we only delete source files after
        the database transaction has been committed:

        1. Parse-and-insert every top-level JSON file. Bad files are
           quarantined immediately; successfully inserted files are queued
           for deletion.
        2. ``conn.commit()`` — if this raises, the queued files are left on
           disk for a subsequent drain to retry and the exception propagates.
        3. Unlink each committed file. Unlink failures (e.g. Windows file
           locks) quarantine the source so it isn't re-inserted next drain.
        """
        spool = self._spool_dir
        spool.mkdir(parents=True, exist_ok=True)
        quarantine = spool / ".quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)

        # ``glob("*.json")`` is non-recursive — the ``.quarantine`` subdir is
        # skipped naturally. Sort so the oldest timestamp-prefixed filename is
        # processed first and inserts land in chronological order.
        files = sorted(spool.glob("*.json"))

        quarantined = 0
        # Files whose rows were queued on the connection. Cleanup happens
        # AFTER commit() returns successfully.
        inserted: list[Path] = []

        # ---- Pass 1: parse + insert (no file unlinks yet) -----------------
        inserted_payloads: list[dict[str, object]] = []
        for path in files:
            try:
                payload = self._insert_one(path)
            except Exception:
                # Any failure — JSON parse error, missing field, DB error —
                # quarantines the file so the rest of the batch can drain.
                self._quarantine(path, quarantine)
                quarantined += 1
            else:
                inserted.append(path)
                inserted_payloads.append(payload)

        # ---- Pass 2: commit once per batch --------------------------------
        # If commit raises, ``inserted`` files stay on disk; the exception
        # propagates so the caller knows the drain did not complete. A
        # subsequent drain will re-read those files and retry.
        self._conn.commit()

        # ---- Pass 2.5: Phase 3/4 side-effects on committed payloads -------
        # Runs AFTER the batch commit so the hook_events rows are durable
        # before any episode-lifecycle side-effect fires. Each side-effect
        # is guarded individually so one bad payload cannot block the rest
        # of the batch from being unlinked.
        if self._episodes is not None:
            for payload in inserted_payloads:
                event_type = payload.get("event_type")
                if event_type == "session_start":
                    self._maybe_open_episode_for_session_start(payload)
                elif event_type == "commit_close":
                    self._maybe_close_episode_for_commit(payload)

        # ---- Pass 3: unlink committed files -------------------------------
        # Only reached if commit() succeeded. Every file in ``inserted`` now
        # has a durable row, so losing the file is safe; failing to unlink
        # is a bookkeeping problem (quarantine to prevent re-insertion on
        # the next drain).
        drained = 0
        for path in inserted:
            try:
                path.unlink()
            except OSError:
                # File couldn't be deleted (e.g. locked on Windows). The row
                # is already committed, so a re-drain would double-insert.
                # Quarantine the source instead to prevent duplication.
                self._quarantine(path, quarantine)
            drained += 1

        return DrainReport(drained=drained, quarantined=quarantined)

    # ----------------------------------------------------------------- helpers
    def _insert_one(self, path: Path) -> dict[str, object]:
        """Parse ``path`` and INSERT its contents into ``hook_events``.

        Returns the parsed payload so callers can inspect ``event_type``
        for post-commit side-effects (Phase 3 session_start handling).
        Raises on any validation or DB error so the caller can quarantine.
        """
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("spool payload is not a JSON object")

        for field in _REQUIRED_FIELDS:
            if field not in data or data[field] in (None, ""):
                raise ValueError(f"spool payload missing required field: {field}")

        self._conn.execute(
            """
            INSERT INTO hook_events (
                id, event_type, tool, file, content_snippet, cwd, session_id,
                event_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                data["event_type"],
                data.get("tool"),
                data.get("file"),
                data.get("content_snippet"),
                data.get("cwd"),
                data.get("session_id"),
                data["timestamp"],
            ),
        )
        return data

    def _maybe_open_episode_for_session_start(
        self, payload: dict[str, object]
    ) -> None:
        """Lazy-open a background episode for a drained session_start event.

        Idempotent: if the session already has an active episode, skip.
        Guarded by try/except so domain failures do not block drain's
        main job of inserting hook_events rows or unlinking spool files.
        """
        if self._episodes is None:
            return
        session_id = payload.get("session_id")
        project = payload.get("project")
        if not isinstance(session_id, str) or not session_id:
            return
        if not isinstance(project, str) or not project:
            return
        try:
            if self._episodes.active_episode(session_id) is None:
                self._episodes.open_background(
                    session_id=session_id, project=project
                )
        except Exception:  # noqa: BLE001 — drain side-effects must not fail drain
            pass

    def _maybe_close_episode_for_commit(
        self, payload: dict[str, object]
    ) -> None:
        """Close the active episode for a drained commit_close event.

        Uses ``EpisodeService.close_active`` with ``outcome='success'``
        and ``close_reason='goal_complete'`` per spec §3. Swallows the
        ValueError that close_active raises when no active episode
        exists — a stale or duplicate commit_close marker must not
        fail drain.
        """
        if self._episodes is None:
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            self._episodes.close_active(
                session_id=session_id,
                outcome="success",
                close_reason="goal_complete",
            )
        except ValueError:
            # No active episode for this session — stale/duplicate marker.
            pass
        except Exception:  # noqa: BLE001 — drain side-effects must not fail drain
            pass

    @staticmethod
    def _quarantine(src: Path, quarantine_dir: Path) -> None:
        """Move ``src`` into ``quarantine_dir`` keeping its original name."""
        dest = quarantine_dir / src.name
        try:
            # ``Path.replace`` overwrites the destination atomically on the
            # same filesystem. Fall back to ``shutil.move`` if it isn't.
            src.replace(dest)
        except OSError:
            try:
                shutil.move(str(src), str(dest))
            except OSError:
                # As a last resort, drop the file so we don't re-read it on
                # the next drain. Losing a malformed file is preferable to
                # spinning on it forever.
                try:
                    src.unlink()
                except OSError:
                    pass
