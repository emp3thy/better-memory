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

from better_memory import _diag
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
    fires episode-lifecycle side-effects for ``commit_close`` and
    ``session_end`` events. Each side-effect is guarded by per-event
    try/except so the side-effect can never cause drain to lose data.
    ``episodes=None`` (the default) preserves Phase 1/2 behaviour exactly.

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
        fn = "SpoolService.drain"
        with _diag.trace(fn, spool_dir=str(self._spool_dir)):
            spool = self._spool_dir
            spool.mkdir(parents=True, exist_ok=True)
            quarantine = spool / ".quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)

            files = sorted(spool.glob("*.json"))
            _diag.step(fn, "scanned_spool", n_files=len(files))

            quarantined = 0
            inserted: list[Path] = []

            # ---- Pass 1: parse + insert (no file unlinks yet) -------------
            inserted_payloads: list[dict[str, object]] = []
            _diag.step(fn, "pass1_begin")
            for path in files:
                try:
                    payload = self._insert_one(path)
                except Exception:
                    self._quarantine(path, quarantine)
                    quarantined += 1
                else:
                    inserted.append(path)
                    inserted_payloads.append(payload)
            _diag.step(
                fn,
                "pass1_done",
                inserted=len(inserted),
                quarantined=quarantined,
            )

            # ---- Pass 2: commit once per batch ----------------------------
            _diag.step(fn, "pass2_commit_begin")
            self._conn.commit()
            _diag.step(fn, "pass2_commit_done")

            # ---- Pass 2.5: Phase 3/4 side-effects on committed payloads ---
            if self._episodes is not None:
                _diag.step(fn, "pass2.5_side_effects_begin")
                for payload in inserted_payloads:
                    event_type = payload.get("event_type")
                    if event_type == "commit_close":
                        _diag.step(fn, "side_effect_commit_close")
                        self._maybe_close_episode_for_commit(payload)
                    elif event_type == "session_end":
                        _diag.step(fn, "side_effect_session_end")
                        self._maybe_close_episode_for_session_end(payload)
                _diag.step(fn, "pass2.5_side_effects_done")

            # ---- Pass 3: unlink committed files ---------------------------
            _diag.step(fn, "pass3_unlink_begin", n=len(inserted))
            drained = 0
            for path in inserted:
                try:
                    path.unlink()
                except OSError:
                    self._quarantine(path, quarantine)
                drained += 1
            _diag.step(fn, "pass3_unlink_done", drained=drained)

            return DrainReport(drained=drained, quarantined=quarantined)

    # ----------------------------------------------------------------- helpers
    def _insert_one(self, path: Path) -> dict[str, object]:
        """Parse ``path`` and INSERT its contents into ``hook_events``.

        Returns the parsed payload so callers can inspect ``event_type``
        for post-commit side-effects (commit_close / session_end handling).
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

    def _maybe_close_episode_for_session_end(
        self, payload: dict[str, object]
    ) -> None:
        """Close the active background (unhardened) episode on session_end.

        Hardened episodes (goal != NULL) are left open so the next session's
        reconcile prompt can resolve them with a real outcome — matches spec
        §3. Background episodes have no goal to reconcile, so closing them
        immediately as ``no_outcome`` / ``session_end_reconciled`` keeps the
        open-episode list clean. Errors are swallowed: drain side-effects
        must not fail drain.
        """
        if self._episodes is None:
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            active = self._episodes.active_episode(session_id)
            if active is None or active.goal is not None:
                return
            self._episodes.close_active(
                session_id=session_id,
                outcome="no_outcome",
                close_reason="session_end_reconciled",
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
