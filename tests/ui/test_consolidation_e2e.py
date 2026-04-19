"""End-to-end Phase 2 smoke tests, executed against Phase 3's real
ConsolidationService. These were deferred in the Phase 2 plan because
Phase 2 had no way to produce real candidates; Phase 3 does.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from flask.testing import FlaskClient


def _seed_cluster(
    conn: sqlite3.Connection, project: str, component: str, n: int
) -> list[str]:
    ids = []
    for i in range(n):
        oid = f"{component}-{i}"
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, status, "
            "validated_true, outcome) "
            "VALUES (?, ?, ?, ?, ?, 'active', 1, 'success')",
            (oid, f"observation {i} of {component}", project, component, "core"),
        )
        ids.append(oid)
    conn.commit()
    return ids


def _run_consolidation(client: FlaskClient, draft_text: str) -> str:
    """Run /pipeline/consolidate, join the worker thread, apply the
    result so candidates land in the review queue, then return job_id."""
    fake = client.application.config["_fake_chat"]
    fake.responses.append(draft_text)

    post = client.post(
        "/pipeline/consolidate", headers={"Origin": "http://localhost"}
    )
    match = re.search(rb'data-job-id="([a-f0-9]+)"', post.data)
    assert match is not None
    job_id = match.group(1).decode()

    # Deterministic wait — join the consolidation thread by name.
    for t in threading.enumerate():
        if t.name.startswith("consolidation-"):
            t.join(timeout=5.0)
            assert not t.is_alive(), "consolidation thread did not exit"

    # Apply the dry-run result so approvable candidates exist.
    apply_resp = client.post(
        f"/jobs/{job_id}/apply", headers={"Origin": "http://localhost"}
    )
    assert apply_resp.status_code == 200, apply_resp.data
    return job_id


def test_approve_a_real_candidate(client: FlaskClient) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "api", 3)

    _run_consolidation(client, "Drafted insight content for approve.")

    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]

    resp = client.post(
        f"/candidates/{cand_id}/approve",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "confirmed"


def test_reject_a_real_candidate(client: FlaskClient) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "db", 3)

    _run_consolidation(client, "Drafted insight content for reject.")

    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]

    resp = client.post(
        f"/candidates/{cand_id}/reject",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "retired"


def test_retire_a_confirmed_insight_end_to_end(
    client: FlaskClient,
) -> None:
    conn = client.application.extensions["db_connection"]
    project = Path.cwd().name
    _seed_cluster(conn, project, "cache", 3)

    _run_consolidation(client, "Drafted insight content for retire.")

    cand_id = conn.execute(
        "SELECT id FROM insights WHERE project = ? AND status = 'pending_review'",
        (project,),
    ).fetchone()["id"]
    # Approve (→ confirmed) then Retire (→ retired)
    client.post(
        f"/candidates/{cand_id}/approve",
        headers={"Origin": "http://localhost"},
    )
    resp = client.post(
        f"/insights/{cand_id}/retire",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status FROM insights WHERE id = ?", (cand_id,)
    ).fetchone()
    assert row["status"] == "retired"
