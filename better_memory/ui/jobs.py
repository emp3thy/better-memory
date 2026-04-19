"""Background-job registry for the Management UI.

Phase 3: ``start_consolidation_job`` spawns a ``threading.Thread`` that
runs ``ConsolidationService.dry_run()`` and stores the result. The UI
polls ``/jobs/<id>`` until ``state.status == 'complete'`` (or
``'failed'``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import traceback
from collections.abc import Callable
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
_apply_lock = threading.Lock()
_current_job_id: str | None = None
_jobs: dict[str, "JobState"] = {}

# TODO(phase4): cap _jobs dict size once long-running jobs become common.


def _run_sync_or_in_worker(work_fn: Callable[[], None]) -> None:
    """Run ``work_fn`` on the current thread, or on a fresh worker thread if
    an asyncio event loop is already active on the caller's thread.

    Why: Flask is sync and our service methods are async, so sync handlers
    call ``asyncio.run`` internally. ``asyncio.run`` raises ``RuntimeError``
    if invoked while a loop is already running (pytest-asyncio auto-mode,
    Jupyter, embedded asyncio contexts). Delegating to a fresh thread in
    those cases gives ``work_fn`` a clean thread with no running loop.

    ``work_fn`` is responsible for creating any thread-bound resources
    (e.g. ``sqlite3.Connection``) inside its own body so they are created
    on the thread that uses them.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        work_fn()
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(work_fn).result()


@dataclass
class JobState:
    id: str
    status: str  # "running" | "complete" | "failed"
    message: str = ""
    result: DryRunResult | None = None
    error: str | None = None
    applied: bool = False


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def apply_job(
    job_id: str,
    *,
    db_path: Path,
    chat: ChatCompleter,
) -> JobState:
    """Persist a completed job's drafts: create insights (pending_review)
    and archive swept observations. Idempotent: second call is a no-op.

    Returns the updated ``JobState``. Raises ``LookupError`` if the job
    is unknown, ``ValueError`` if it is not ``complete`` or has no result.
    """
    with _apply_lock:
        state = _jobs.get(job_id)
        if state is None:
            raise LookupError(job_id)
        if state.status != "complete" or state.result is None:
            raise ValueError(f"Cannot apply job in status {state.status!r}")
        if state.applied:
            return state

        result = state.result
        # Note: apply_branch commits per candidate. If a candidate raises mid-loop,
        # earlier candidates are already committed but state.applied stays False.
        # Retries may produce duplicate pending_review rows until apply_branch is
        # made fully transactional across all candidates.

        def _work() -> None:
            # Create the connection on whichever thread ends up running this —
            # SQLite connections are thread-bound, and ``_work`` may execute on
            # a worker thread when an asyncio loop is already active on the
            # caller's thread (pytest-asyncio auto-mode, embedded contexts).
            conn = connect(db_path)
            try:
                svc = ConsolidationService(conn=conn, chat=chat)

                async def _do_apply() -> None:
                    for c in result.branch:
                        await svc.apply_branch(c)
                    for s in result.sweep:
                        await svc.apply_sweep(s.observation_id)

                asyncio.run(_do_apply())
            finally:
                conn.close()

        _run_sync_or_in_worker(_work)

        state.applied = True
        state.message = (
            f"Applied {len(result.branch)} candidate(s) to review queue, "
            f"archived {len(result.sweep)} observation(s)."
        )
        return state


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
                # Suppress close errors so they can't overwrite a successful
                # state with "failed" via the outer except.
                try:
                    conn.close()
                except Exception:
                    pass
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
