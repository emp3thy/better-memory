"""Gating for retention-runs panel, Confirm action, and inline text-edit."""

from __future__ import annotations

import uuid
from pathlib import Path

from flask.testing import FlaskClient

from better_memory.db.connection import connect


def _seed_reflection(db_path: str) -> str:
    """Insert a fresh pending_review reflection directly (mirrors the
    raw-SQL seeding pattern in ``tests/ui/test_reflections.py``; there is
    no ``ReflectionService.create`` - that class only exposes the four
    lifecycle actions confirm/retire/update_text/promote_to_general)."""
    rid = str(uuid.uuid4())
    conn = connect(Path(db_path))
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, useful_count, "
            "times_misled, times_overlooked, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, "Prefer batched writes", "testproj", "sqlite",
                "implementation", "do", "When writing many records",
                '["batch them"]', 0.8, "pending_review", 0, "project", 0,
                0, 0,
            ),
        )
        conn.commit()
        return rid
    finally:
        conn.close()


def test_sqlite_diagnostics_shows_retention_and_ratings(
    client: FlaskClient,
) -> None:
    html = client.get("/diagnostics").get_data(as_text=True)
    assert "Retention runs" in html
    assert "Hook errors" in html
    assert "Recent ratings" in html
    assert "Rating diagnostics" in html
    assert client.get("/diagnostics/panel/retention-runs").status_code == 200


def test_agentcore_diagnostics_hides_retention_keeps_operational(
    agentcore_client: FlaskClient,
) -> None:
    html = agentcore_client.get("/diagnostics").get_data(as_text=True)
    assert "Retention runs" not in html
    # Operational surfaces stay visible in agentcore mode.
    assert "Hook errors" in html
    assert "Recent ratings" in html
    assert "Rating diagnostics" in html
    assert (
        agentcore_client.get("/diagnostics/panel/retention-runs").status_code
        == 404
    )
    assert agentcore_client.get("/diagnostics/panel/hook-errors").status_code == 200


def test_sqlite_reflection_drawer_shows_confirm_and_edit(
    client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    html = client.get(f"/reflections/{rid}/drawer").get_data(as_text=True)
    assert "action-confirm" in html
    assert "action-edit" in html


def test_agentcore_reflection_drawer_hides_confirm_and_edit(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    # Row-only detail with an agentcore-active status; Confirm/Edit gated off.
    fake = agentcore_client.application.extensions["backend"]

    def _row_only(*, reflection_id: str):
        return {
            "id": reflection_id, "project": "testproj", "title": "t",
            "tech": None, "phase": "implementation", "polarity": "do",
            "confidence": 0.8, "status": "active", "scope": "project",
            "evidence_count": 0, "updated_at": "x",
            "use_cases": "u", "hints": '["h"]',
            "useful_count": 0, "last_useful_at": None,
            "times_overlooked": 0, "last_overlooked_at": None,
            "times_misled": 0, "last_misled_at": None,
        }

    from unittest.mock import patch

    with patch.object(fake, "reflection_get", side_effect=_row_only):
        html = agentcore_client.get(
            f"/reflections/{rid}/drawer"
        ).get_data(as_text=True)
    assert "action-confirm" not in html
    assert "action-edit" not in html


def test_agentcore_confirm_and_edit_routes_404(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    origin = {"Origin": "http://localhost"}
    assert (
        agentcore_client.post(
            f"/reflections/{rid}/confirm", headers=origin
        ).status_code
        == 404
    )
    assert agentcore_client.get(f"/reflections/{rid}/edit").status_code == 404
    assert (
        agentcore_client.post(
            f"/reflections/{rid}/edit",
            data={"use_cases": "u", "hints": "h"},
            headers=origin,
        ).status_code
        == 404
    )
