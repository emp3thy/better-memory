"""Tests for diagnostics Recent ratings panel + session_id_missing counter."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_rated_exposure(
    conn, sid, kind, mid, classification="cited", evidence=None,
):
    """Insert an exposure that's already been rated."""
    conn.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source,
            rated_at, classification, evidence)
           VALUES (?, ?, ?, '2026-05-11T10:00:00+00:00', 'bootstrap',
                   '2026-05-11T11:00:00+00:00', ?, ?)""",
        (sid, kind, mid, classification, evidence),
    )
    conn.commit()


def _seed_reflection(conn, rid, title="Some title"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, title),
    )
    conn.commit()


class TestSessionIdMissingCounter:
    def test_retrieve_reflections_bumps_counter_when_env_missing(
        self, conn, monkeypatch,
    ):
        """When CLAUDE_SESSION_ID is unset, retrieve_reflections increments
        rating_diagnostics.session_id_missing."""
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p")  # default track_exposure=True

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value >= 1

    def test_semantic_list_bumps_counter_when_env_missing(
        self, conn, monkeypatch,
    ):
        from better_memory.services.semantic import SemanticMemoryService

        conn.execute(
            """INSERT INTO semantic_memories
               (id, content, project, scope, created_at, updated_at)
               VALUES ('s1', 'fact', 'p', 'project',
                       '2026-01-01', '2026-01-01')"""
        )
        conn.commit()
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = SemanticMemoryService(conn)
        svc.list_for_project(project="p")  # default track_exposure=True

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value >= 1

    def test_counter_not_bumped_when_track_exposure_false(
        self, conn, monkeypatch,
    ):
        """Bootstrap-style callers passing track_exposure=False must NOT
        bump the counter (no env-missing inflation from legitimate
        non-Claude integration tests)."""
        from better_memory.services.reflection import ReflectionSynthesisService

        _seed_reflection(conn, "r1")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", track_exposure=False)

        value = conn.execute(
            "SELECT value FROM rating_diagnostics WHERE metric='session_id_missing'"
        ).fetchone()["value"]
        assert value == 0


class TestDiagnosticsPanel:
    """Verify the Flask route exposes Recent ratings + diagnostics counter."""

    def test_recent_ratings_panel_lists_rated_exposures(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        _seed_reflection(conn, "r1", title="My Reflection Title")
        _seed_rated_exposure(conn, "S1", "reflection", "r1", "cited")

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app(start_watchdog=False)
        client = app.test_client()
        response = client.get("/diagnostics")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Recent ratings" in body
        assert "r1" in body
        assert "cited" in body
        assert "My Reflection Title" in body

    def test_recent_ratings_panel_shows_evidence_cell(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        _seed_reflection(conn, "r1", title="My Reflection Title")
        _seed_rated_exposure(
            conn, "S1", "reflection", "r1", "shaped",
            evidence="guided the retry fix",
        )

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app(start_watchdog=False)
        client = app.test_client()
        response = client.get("/diagnostics")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "guided the retry fix" in body

    def test_recent_ratings_panel_truncates_long_evidence(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        _seed_reflection(conn, "r1", title="My Reflection Title")
        long_evidence = "x" * 200
        _seed_rated_exposure(
            conn, "S1", "reflection", "r1", "shaped", evidence=long_evidence,
        )

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app(start_watchdog=False)
        client = app.test_client()
        body = client.get("/diagnostics").data.decode("utf-8")
        # Full text preserved in the title attribute for hover.
        assert f'title="{long_evidence}"' in body
        # Displayed cell text is truncated: the untruncated 200-char run
        # must not appear anywhere outside that title attribute.
        assert body.count(long_evidence) == 1

    def test_session_id_missing_counter_displayed(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        conn.execute(
            "UPDATE rating_diagnostics SET value=3 WHERE metric='session_id_missing'"
        )
        conn.commit()

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app(start_watchdog=False)
        client = app.test_client()
        response = client.get("/diagnostics")
        body = response.data.decode("utf-8")
        assert "session_id_missing" in body
        assert "3" in body

    def test_overlooked_total_displayed(
        self, conn, tmp_memory_db, monkeypatch,
    ):
        from better_memory.ui.app import create_app

        # One reflection overlooked twice, one semantic memory overlooked once.
        conn.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at, times_overlooked)
               VALUES ('r1', 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                       '2026-01-01', '2026-01-01', 2)"""
        )
        conn.execute(
            """INSERT INTO semantic_memories
               (id, content, project, scope, created_at, updated_at,
                times_overlooked)
               VALUES ('s1', 'fact', 'p', 'project',
                       '2026-01-01', '2026-01-01', 1)"""
        )
        conn.commit()

        monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_memory_db.parent))
        app = create_app(start_watchdog=False)
        client = app.test_client()
        body = client.get("/diagnostics").data.decode("utf-8")
        assert "overlooked (total)" in body
        assert "memories the agent dropped until the user intervened" in body
        # Anchor on the overlooked-total <dd> so an incidental "3"
        # elsewhere on the page cannot satisfy the assertion.
        import re
        m = re.search(r"overlooked \(total\)</dt>\s*<dd>\s*(\d+)", body)
        assert m is not None and m.group(1) == "3"  # 2 + 1


def _seed_rated(conn, sid, mid, source, classification, rated_at):
    conn.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification)
           VALUES (?, 'semantic', ?, ?, ?, ?, ?)""",
        (sid, mid, rated_at, source, rated_at, classification),
    )


def _seed_ledger(conn):
    """Three rated sessions plus one unrated row.

    s1 (oldest): bootstrap ignored, contextual shaped
    s2: trigger cited, trigger ignored, retrieve misled
    s3 (newest): bootstrap ignored, trigger shaped
    """
    _seed_rated(conn, "s1", "m1", "bootstrap", "ignored", "2026-10-01T10:00:00+00:00")
    _seed_rated(conn, "s1", "m2", "contextual", "shaped", "2026-10-01T10:00:00+00:00")
    _seed_rated(conn, "s2", "m3", "trigger", "cited", "2026-10-02T10:00:00+00:00")
    _seed_rated(conn, "s2", "m4", "trigger", "ignored", "2026-10-02T10:00:00+00:00")
    _seed_rated(conn, "s2", "m5", "retrieve", "misled", "2026-10-02T10:00:00+00:00")
    _seed_rated(conn, "s3", "m6", "bootstrap", "ignored", "2026-10-03T10:00:00+00:00")
    _seed_rated(conn, "s3", "m7", "trigger", "shaped", "2026-10-03T10:00:00+00:00")
    conn.execute(
        "INSERT INTO session_memory_exposure (session_id, memory_kind, memory_id, "
        "exposed_at, source) VALUES ('s4', 'semantic', 'm8', '2026-10-04', 'trigger')"
    )
    conn.commit()


class TestUsefulRateByChannel:
    def test_all_time_rows_total_and_guard(self, conn):
        from better_memory.ui.queries import useful_rate_by_channel
        _seed_ledger(conn)
        out = useful_rate_by_channel(conn, last_n_sessions=None)
        rows = {r["source"]: r for r in out["rows"]}
        assert rows["bootstrap"] == {
            "source": "bootstrap", "rated": 2, "useful": 0, "ignored": 2, "misled": 0,
            "rate": 0.0,
        }
        assert rows["trigger"] == {
            "source": "trigger", "rated": 3, "useful": 2, "ignored": 1, "misled": 0,
            "rate": pytest.approx(2 / 3),
        }
        assert rows["retrieve"]["misled"] == 1 and rows["retrieve"]["rate"] == 0.0
        assert out["total"] == {
            "source": "all", "rated": 7, "useful": 3, "ignored": 3, "misled": 1,
            "rate": pytest.approx(3 / 7),
        }
        assert out["rated_sessions"] == 3
        assert out["useful_per_session"] == pytest.approx(1.0)

    def test_last_n_sessions_window_drops_oldest(self, conn):
        from better_memory.ui.queries import useful_rate_by_channel
        _seed_ledger(conn)
        out = useful_rate_by_channel(conn, last_n_sessions=2)
        sources = {r["source"] for r in out["rows"]}
        assert "contextual" not in sources          # only in s1
        assert out["total"]["rated"] == 5
        assert out["rated_sessions"] == 2

    def test_empty_ledger(self, conn):
        from better_memory.ui.queries import useful_rate_by_channel
        out = useful_rate_by_channel(conn, last_n_sessions=15)
        assert out["rows"] == []
        assert out["total"]["rated"] == 0 and out["total"]["rate"] == 0.0
        assert out["rated_sessions"] == 0 and out["useful_per_session"] == 0.0

    def test_diagnostics_page_renders_panel(self, client, tmp_db):
        c = connect(tmp_db)
        try:
            _seed_ledger(c)
        finally:
            c.close()
        body = client.get("/diagnostics").get_data(as_text=True)
        assert "Useful rate by channel" in body
        assert "trigger" in body
        assert "Useful per rated session" in body
        assert "67%" in body
