"""Tests for POST /jobs/<id>/apply — persist dry-run drafts to review queue."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from flask.testing import FlaskClient

import better_memory.ui.jobs as jobs


def _seed_branch_observations(conn, project: str) -> None:
    """Insert 3 active observations in the same cluster (same project/component/theme)."""
    for i in range(3):
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, status, validated_true, outcome) "
            "VALUES (?, ?, ?, 'api', 'retry', 'active', 1, 'success')",
            (f"obs-{i}", f"content {i}", project),
        )
    conn.commit()


def _seed_sweep_observation(conn, project: str) -> str:
    """Insert 1 stale active observation suitable for sweep. Returns its id."""
    obs_id = "sweep-obs-1"
    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    conn.execute(
        "INSERT INTO observations "
        "(id, content, project, status, used_count, validated_true, last_retrieved) "
        "VALUES (?, ?, ?, 'active', 0, 0, ?)",
        (obs_id, "stale observation content", project, old_ts),
    )
    conn.commit()
    return obs_id


def _consolidate_and_join(client: FlaskClient) -> str:
    """POST /pipeline/consolidate, join the background thread, return job_id."""
    resp = client.post(
        "/pipeline/consolidate",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200
    match = re.search(rb'data-job-id="([a-f0-9]+)"', resp.data)
    assert match is not None, "No data-job-id in consolidate response"
    job_id = match.group(1).decode()

    for t in threading.enumerate():
        if t.name.startswith("consolidation-"):
            t.join(timeout=5.0)
            assert not t.is_alive(), "consolidation thread did not exit"

    return job_id


class TestApplyBranchCandidates:
    def test_apply_persists_branch_candidates(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name

        _seed_branch_observations(conn, project)

        fake = client.application.config["_fake_chat"]
        fake.responses.append("drafted insight text")

        job_id = _consolidate_and_join(client)

        # Before apply: no pending_review rows
        before = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE status = 'pending_review'"
        ).fetchone()[0]
        assert before == 0

        resp = client.post(
            f"/jobs/{job_id}/apply",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "job-complete"

        # After apply: 1 pending_review insight with the drafted content
        rows = conn.execute(
            "SELECT content, status FROM insights WHERE status = 'pending_review'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "drafted insight text"

        # Source observations are now consolidated
        obs_rows = conn.execute(
            "SELECT status FROM observations WHERE id LIKE 'obs-%'"
        ).fetchall()
        for row in obs_rows:
            assert row["status"] == "consolidated"


class TestApplyArchivesSweepCandidates:
    def test_apply_archives_sweep_candidates(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name

        obs_id = _seed_sweep_observation(conn, project)

        job_id = _consolidate_and_join(client)

        resp = client.post(
            f"/jobs/{job_id}/apply",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200

        row = conn.execute(
            "SELECT status FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        assert row["status"] == "archived"


class TestApplyIdempotent:
    def test_apply_is_idempotent(self, client: FlaskClient) -> None:
        conn = client.application.extensions["db_connection"]
        project = Path.cwd().name

        _seed_branch_observations(conn, project)
        fake = client.application.config["_fake_chat"]
        fake.responses.append("drafted insight text")

        job_id = _consolidate_and_join(client)

        # First apply
        resp1 = client.post(
            f"/jobs/{job_id}/apply",
            headers={"Origin": "http://localhost"},
        )
        assert resp1.status_code == 200

        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE status = 'pending_review'"
        ).fetchone()[0]

        # Second apply — must be a no-op
        resp2 = client.post(
            f"/jobs/{job_id}/apply",
            headers={"Origin": "http://localhost"},
        )
        assert resp2.status_code == 200

        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE status = 'pending_review'"
        ).fetchone()[0]

        assert count_after_second == count_after_first

        # job.applied must still be True
        state = jobs.get_job(job_id)
        assert state is not None
        assert state.applied is True


class TestApplyUnknownJob:
    def test_apply_unknown_job_returns_404(self, client: FlaskClient) -> None:
        resp = client.post(
            "/jobs/nonexistent/apply",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 404


class TestApplyIncompleteJob:
    def test_apply_rejects_incomplete_job(self, client: FlaskClient) -> None:
        job_id = uuid4().hex
        jobs._jobs[job_id] = jobs.JobState(id=job_id, status="running")
        try:
            resp = client.post(
                f"/jobs/{job_id}/apply",
                headers={"Origin": "http://localhost"},
            )
            assert resp.status_code == 400
            assert b"Cannot apply job" in resp.data
        finally:
            del jobs._jobs[job_id]
