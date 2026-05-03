"""Unit tests for the /diagnostics page."""

from __future__ import annotations

from pathlib import Path


def _seed_hook_error(
    db_path: Path, *, error_id: str = "e-1",
    hook_name: str = "observer",
    created_at: str = "2026-05-03T10:00:00+00:00",
) -> None:
    from better_memory.db.connection import connect
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO hook_errors "
            "(id, created_at, hook_name, exception_type, "
            " exception_message, traceback, cwd) "
            "VALUES (?, ?, ?, 'RuntimeError', 'simulated', "
            " 'Traceback...', '/tmp/cwd')",
            (error_id, created_at, hook_name),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_retention_run(
    db_path: Path, *,
    run_at: str = "2026-05-03T08:00:00+00:00",
    triggered_by: str = "retrieve",
) -> None:
    from better_memory.db.connection import connect
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO retention_runs "
            "(run_at, archived_via_retired_reflection, "
            " archived_via_consumed_without_reflection, "
            " archived_via_no_outcome_episode, pruned, triggered_by) "
            "VALUES (?, 5, 2, 1, 0, ?)",
            (run_at, triggered_by),
        )
        conn.commit()
    finally:
        conn.close()


class TestDiagnosticsPage:
    def test_diagnostics_page_renders(self, client) -> None:
        resp = client.get("/diagnostics")
        assert resp.status_code == 200
        assert b"Hook errors" in resp.data
        assert b"Retention runs" in resp.data
        assert b"diagnostics" in resp.data  # active_tab

    def test_panel_hook_errors_renders_recent_errors(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        resp = client.get("/diagnostics/panel/hook-errors")
        assert resp.status_code == 200
        assert b"observer" in resp.data
        assert b"RuntimeError" in resp.data

    def test_panel_hook_errors_empty_state(self, client) -> None:
        resp = client.get("/diagnostics/panel/hook-errors")
        assert resp.status_code == 200
        assert b"No hook errors" in resp.data

    def test_panel_retention_runs_renders_recent_runs(
        self, client, tmp_db: Path
    ) -> None:
        _seed_retention_run(tmp_db)
        resp = client.get("/diagnostics/panel/retention-runs")
        assert resp.status_code == 200
        assert b"retrieve" in resp.data
        assert b"archived: 8" in resp.data  # 5+2+1

    def test_hook_error_drawer_renders_traceback(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        resp = client.get("/diagnostics/hook-errors/e-1/drawer")
        assert resp.status_code == 200
        assert b"Traceback..." in resp.data

    def test_hook_error_drawer_returns_404_when_missing(
        self, client
    ) -> None:
        resp = client.get("/diagnostics/hook-errors/missing/drawer")
        assert resp.status_code == 404

    def test_delete_hook_error_removes_row(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-del")
        resp = client.post(
            "/diagnostics/hook-errors/e-del/delete",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "hook-errors-changed"

        from better_memory.db.connection import connect
        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT id FROM hook_errors WHERE id = ?", ("e-del",)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_purge_all_removes_all_rows(
        self, client, tmp_db: Path
    ) -> None:
        _seed_hook_error(tmp_db, error_id="e-1")
        _seed_hook_error(tmp_db, error_id="e-2")
        resp = client.post(
            "/diagnostics/hook-errors/purge",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "hook-errors-changed"

        from better_memory.db.connection import connect
        conn = connect(tmp_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM hook_errors"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_delete_returns_403_without_origin(self, client) -> None:
        resp = client.post(
            "/diagnostics/hook-errors/x/delete"
        )
        assert resp.status_code == 403
