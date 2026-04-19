"""Background-job registry for the Management UI.

Phase 2 provides the plumbing — a lock, a current-job-id, a record of
job state. Phase 3 replaces the job body with ``ConsolidationService``
calls. The public surface is deliberately minimal so Phase 3's
implementation can slot in without churn.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

_lock = threading.Lock()
_current_job_id: str | None = None
_jobs: dict[str, "JobState"] = {}

# TODO(phase3): cap _jobs dict size when Phase 3 makes the stub a real
# long-running job. Simple LRU eviction of ~100 entries is sufficient.


@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str


def current_job_id() -> str | None:
    """Return the currently-running job id, if any."""
    return _current_job_id


def start_phase3_stub_job() -> JobState:
    """Start a placeholder job that records a Phase-3-not-ready message.

    Phase 3 replaces this function with one that spawns a real
    threading.Thread running ConsolidationService.dry_run().
    """
    global _current_job_id
    if not _lock.acquire(blocking=False):
        # Another job is active — return the existing state.
        existing_id = _current_job_id
        if existing_id is not None and existing_id in _jobs:
            return _jobs[existing_id]
        # Lock held but no job recorded — shouldn't happen, but return an
        # error state rather than falling through to try/finally (which
        # would crash on release-without-acquire).
        return JobState(
            id="unknown",
            status="failed",
            message="Consolidation is busy but no job is recorded. Retry in a moment.",
        )
    try:
        job_id = uuid4().hex
        state = JobState(
            id=job_id,
            status="complete",
            message="ConsolidationService ships in Phase 3. This button will run the real dry-run then.",
        )
        _jobs[job_id] = state
        _current_job_id = job_id
        # Phase 2: the "job" is synchronous and complete immediately.
        # Phase 3 will make this async and clear _current_job_id on thread exit.
        return state
    finally:
        _lock.release()
        _current_job_id = None  # Phase 2: job is done by the time we return.


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)
