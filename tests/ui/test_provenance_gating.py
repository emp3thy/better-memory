"""Provenance gating: agentcore drawers omit provenance and take the row-only fetch."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from flask.testing import FlaskClient

from better_memory.db.connection import connect

# NOTE: ReflectionService (better_memory.services.reflection) has no
# `create` method -- it only exposes the UI lifecycle actions (confirm,
# retire, update_text, promote_to_general) plus ReflectionSynthesisService
# for synthesis writes. Seed directly via SQL, matching the pattern already
# used by tests/ui/test_reflections.py's `_seed_reflection` helper.


def _seed_reflection(db_path: Path) -> str:
    rid = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, useful_count, "
            "times_misled, times_overlooked, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-07-25T00:00:00+00:00', '2026-07-25T00:00:00+00:00')",
            (
                rid, "Prefer batched writes", "testproj", "sqlite",
                "implementation", "do", "When writing many records",
                '["batch them"]', 0.8, "confirmed", 0, "project", 0, 0, 0,
            ),
        )
        conn.commit()
        return rid
    finally:
        conn.close()


def test_sqlite_reflection_drawer_shows_provenance(
    client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(tmp_db)
    html = client.get(f"/reflections/{rid}/drawer").get_data(as_text=True)
    assert "Source observations" in html


def test_agentcore_reflection_drawer_omits_provenance_and_uses_row_only(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(tmp_db)
    fake = agentcore_client.application.extensions["backend"]

    def _row_only(*, reflection_id: str) -> dict[str, Any]:
        return {
            "id": reflection_id,
            "project": "testproj",
            "title": "Prefer batched writes",
            "tech": "sqlite",
            "phase": "implementation",
            "polarity": "do",
            "confidence": 0.8,
            "status": "active",
            "scope": "project",
            "evidence_count": 0,
            "updated_at": "2026-07-25T00:00:00Z",
            "use_cases": "When writing many records",
            "hints": '["batch them"]',
            "useful_count": 0,
            "last_useful_at": None,
            "times_overlooked": 0,
            "last_overlooked_at": None,
            "times_misled": 0,
            "last_misled_at": None,
        }

    # Spy: the provenance-join query must NOT be called on the row-only path.
    with patch(
        "better_memory.ui.app.queries.reflection_provenance"
    ) as prov_join, patch.object(fake, "reflection_get", side_effect=_row_only):
        resp = agentcore_client.get(f"/reflections/{rid}/drawer")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Source observations" not in html
    prov_join.assert_not_called()


def test_observation_drawer_provenance_conditional_present(tmp_path) -> None:
    # Observations tab is gated off in agentcore (G1), but the template
    # conditional must still guard the linked-reflections block. Assert the
    # sqlite render includes the block only when data + flag allow.
    from better_memory.ui.app import create_app

    app = create_app(start_watchdog=False, db_path=tmp_path / "memory.db")
    # Use a request context (not just app_context): the template's
    # `detail.observation.status == 'active'` promote-form block calls
    # `url_for(...)`, which requires an active request without
    # SERVER_NAME configured.
    with app.test_request_context():
        from flask import render_template

        class _Caps:
            supports_provenance = False

        detail = type("D", (), {
            "observation": type("O", (), {
                "outcome": "success", "created_at": "x", "content": "c",
                "id": "o1", "component": None, "theme": None, "tech": None,
                "trigger_type": None, "status": "active",
                "reinforcement_score": 0, "episode_id": None,
            })(),
            "reflections": [type("R", (), {
                "polarity": "do", "confidence": 0.9, "title": "t",
                "status": "active",
            })()],
            "audit": [],
        })()
        html = render_template(
            "fragments/observation_drawer.html", detail=detail, caps=_Caps(),
        )
    assert "Linked reflections" not in html
