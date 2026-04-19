"""Background-job registry for the Management UI.

Phase 3: ``start_consolidation_job`` spawns a ``threading.Thread`` that
runs ``ConsolidationService.dry_run()`` and stores the result. The UI
polls ``/jobs/<id>`` until ``state.status == 'complete'`` (or
``'failed'``).
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from better_memory.db.connection import connect
from better_memory.llm.ollama import ChatCompleter
from better_memory.services.consolidation import (
    ConsolidationService,
    DryRunResult,
)

_lock = threading.Lock()
_current_job_id: str | None = None
_jobs: dict[str, "JobState"] = {}

# TODO(phase4): cap _jobs dict size once long-running jobs become common.


@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str = ""
    result: DryRunResult | None = None
    error: str | None = None


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def start_consolidation_job(
    *,
    db_path: Path,
    chat: ChatCompleter,
    project: str,
    stale_days: int = 30,
) -> JobState:
    """Spawn a consolidation thread. Returns the initial ``JobState``.

    The thread owns its own ``sqlite3.Connection`` (the UI's request
    connection is single-threaded). The lock prevents concurrent jobs.
    """
    global _current_job_id
    if not _lock.acquire(blocking=False):
        existing_id = _current_job_id
        if existing_id is not None and existing_id in _jobs:
            return _jobs[existing_id]
        return JobState(
            id="unknown",
            status="failed",
            error="Consolidation busy but no job recorded; retry shortly.",
        )

    job_id = uuid4().hex
    state = JobState(id=job_id, status="running", message="Running consolidation\u2026")
    _jobs[job_id] = state
    _current_job_id = job_id

    def _run() -> None:
        global _current_job_id
        try:
            conn = connect(db_path)
            try:
                svc = ConsolidationService(conn=conn, chat=chat)
                result = asyncio.run(
                    svc.dry_run(project=project, stale_days=stale_days)
                )
                state.result = result
                state.status = "complete"
                state.message = (
                    f"{len(result.branch)} candidate(s), "
                    f"{len(result.sweep)} sweep item(s)."
                )
            finally:
                conn.close()
        except Exception:
            state.status = "failed"
            state.error = traceback.format_exc()
        finally:
            # Clear current job ID BEFORE releasing the lock to avoid race
            # with another caller that might acquire the lock immediately.
            _current_job_id = None
            _lock.release()

    t = threading.Thread(target=_run, daemon=True, name=f"consolidation-{job_id[:8]}")
    t.start()
    return state
