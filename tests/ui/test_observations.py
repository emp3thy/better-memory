"""Flask test-client tests for the Observations tab."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.llm.ollama import OllamaChat


def _seed_episode(
    db_path: Path, *, eid: str = "ep-1", project: str = "proj-a"
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at) "
            "VALUES (?, ?, '2026-04-26T10:00:00+00:00')",
            (eid, project),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_obs(
    db_path: Path,
    *,
    oid: str,
    project: str = "proj-a",
    component: str | None = "ui_launcher",
    theme: str | None = "bug",
    outcome: str = "neutral",
    status: str = "active",
    content: str = "test obs",
    episode_id: str = "ep-1",
    created_at: str = "2026-04-26T10:00:00+00:00",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, content, project, component, theme, outcome, status,
             episode_id, created_at),
        )
        conn.commit()
    finally:
        conn.close()


class TestObservationsPage:
    def test_returns_200(self, client: FlaskClient):
        response = client.get("/observations")
        assert response.status_code == 200

    def test_renders_filter_form(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert 'name="status"' in body
        assert 'name="outcome"' in body
        assert 'name="component"' in body

    def test_renders_run_synthesis_button(self, client: FlaskClient):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert "Run synthesis" in body

    def test_synthesis_5xx_responses_swap_via_global_htmx_config(
        self, client: FlaskClient
    ):
        """HTMX 2.x default responseHandling skips swaps on 4xx/5xx,
        so the card-error fragment returned by a failed
        POST /observations/synthesize never reaches the page — the
        user sees no feedback. base.html overrides
        htmx.config.responseHandling to swap all responses except 204.

        Earlier attempts used per-element hx-on listeners
        (`hx-on::before-swap`), but htmx:beforeSwap does not fire on
        non-2xx responses in HTMX 2.x — the event is never dispatched,
        so the listener never runs. Global responseHandling is the
        documented mechanism.
        """
        # The base layout renders on every page, so any tab page works.
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert "htmx.config.responseHandling" in body
        # The override must keep 204 No Content opted out of swap and
        # opt all other status codes in.
        assert '"204"' in body
        assert '".*"' in body
        assert "swap: true" in body
        assert "swap: false" in body


class TestServiceWiring:
    def test_synthesis_dependencies_are_in_app_extensions(
        self, client: FlaskClient
    ) -> None:
        app = client.application
        assert "ollama_host" in app.extensions
        assert "consolidate_model" in app.extensions
        assert "db_path" in app.extensions


class TestObservationsPanel:
    def test_empty_state_when_no_observations(self, client: FlaskClient):
        response = client.get("/observations/panel")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "No observations" in body

    def test_renders_seeded_rows(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="hello world")
        _seed_obs(tmp_db, oid="o-2", content="second")

        response = client.get("/observations/panel?project=proj-a")
        body = response.get_data(as_text=True)
        assert "hello world" in body
        assert "second" in body

    def test_filters_by_outcome(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-fail", outcome="failure", content="bad")
        _seed_obs(tmp_db, oid="o-ok", outcome="success", content="good")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=failure"
        )
        body = response.get_data(as_text=True)
        assert "bad" in body
        assert "good" not in body

    def test_filters_by_status(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-active", status="active", content="A")
        _seed_obs(tmp_db, oid="o-arch", status="archived", content="X")

        response = client.get(
            "/observations/panel?project=proj-a&status=active"
        )
        body = response.get_data(as_text=True)
        assert "A" in body
        assert "X" not in body

    def test_filters_by_component(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-ui", component="ui_launcher", content="ui")
        _seed_obs(tmp_db, oid="o-mcp", component="mcp", content="mcp")

        response = client.get(
            "/observations/panel?project=proj-a&component=ui_launcher"
        )
        body = response.get_data(as_text=True)
        assert "ui" in body
        assert "mcp" not in body

    def test_blank_filter_values_are_treated_as_unset(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", outcome="failure", content="A")
        _seed_obs(tmp_db, oid="o-2", outcome="success", content="B")

        response = client.get(
            "/observations/panel?project=proj-a&outcome=&status="
        )
        body = response.get_data(as_text=True)
        # Both should appear when filters are blank.
        assert "A" in body
        assert "B" in body


class TestObservationDrawer:
    def test_renders_full_content(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        _seed_obs(tmp_db, oid="o-1", content="full content for drawer")

        response = client.get("/observations/o-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "full content for drawer" in body

    def test_returns_404_for_unknown_id(self, client: FlaskClient):
        response = client.get("/observations/nope/drawer")
        assert response.status_code == 404

    def test_renders_metadata_grid(
        self, client: FlaskClient, tmp_db: Path,
    ):
        _seed_episode(tmp_db)
        conn = connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, component, theme, outcome, status, "
                " episode_id, tech, trigger_type, reinforcement_score, "
                " created_at) "
                "VALUES "
                "('o-1', 'x', 'proj-a', 'ui_launcher', 'bug', 'failure', "
                " 'active', 'ep-1', 'python', 'review', 1.5, "
                " '2026-04-26T10:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get("/observations/o-1/drawer")
        body = response.get_data(as_text=True)
        assert "ui_launcher" in body
        assert "bug" in body
        assert "python" in body
        assert "review" in body
        assert "ep-1" in body
        # reinforcement_score appears as text
        assert "1.5" in body


class TestNavTab:
    def test_observations_tab_appears_in_base_layout(
        self, client: FlaskClient
    ):
        response = client.get("/episodes")
        body = response.get_data(as_text=True)
        assert ">Observations<" in body
        assert "/observations" in body

    def test_observations_tab_marked_active_on_observations_page(
        self, client: FlaskClient
    ):
        response = client.get("/observations")
        body = response.get_data(as_text=True)
        assert 'class="tab active"' in body
        assert "Observations" in body


class TestObservationsSynthesize:
    def test_calls_service_and_returns_banner(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def fake_synthesize(self, *, goal, tech, project):
            assert goal == "manual synthesis"
            assert tech is None
            assert project == "proj-a"
            return {
                "do": [{"id": "r1"}, {"id": "r2"}],
                "dont": [{"id": "r3"}],
                "neutral": [],
            }

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", fake_synthesize
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == (
            "observations-synthesized"
        )
        body = response.get_data(as_text=True)
        # Banner mentions the bucket counts.
        assert "2" in body and "do" in body
        assert "1" in body and "dont" in body
        assert "0" in body and "neutral" in body

    def test_returns_500_card_error_on_service_failure(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def boom(self, *, goal, tech, project):
            raise RuntimeError("ollama unreachable")

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", boom
        )

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "card-error" in body
        assert "ollama unreachable" in body

    def test_synthesize_uses_worker_thread_connection_not_app_connection(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: the route must NOT reuse the app's db_connection.

        sqlite3 connections are not thread-safe by default. The route
        dispatches synthesize() to a worker thread, so it must open a
        fresh connection there. We verify by stubbing OllamaChat.complete
        (the lowest LLM-touching boundary) so the real synthesize body
        runs against a real per-thread connection.
        """
        from better_memory.llm.ollama import OllamaChat
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        # Return a parseable empty SynthesisResponse so the synthesize
        # body finishes without producing reflections — we don't care
        # about output, only that no ProgrammingError fires.
        async def fake_complete(self, prompt: str) -> str:
            return '{"new": [], "augment": [], "merge": [], "ignore": []}'

        monkeypatch.setattr(OllamaChat, "complete", fake_complete)

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        # Critically: not 500. Specifically not a ProgrammingError card.
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_data(as_text=True)
        assert "ProgrammingError" not in body
        assert "thread" not in body.lower()
        assert response.headers.get("HX-Trigger") == (
            "observations-synthesized"
        )

    def test_synthesize_succeeds_on_second_call_with_fresh_event_loop(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: each call must build a fresh OllamaChat.

        httpx.AsyncClient pools are bound to the loop they were created
        on. If the route shared a single OllamaChat across calls, the
        second call's loop would inherit dead transports from the first
        closed loop, producing transport errors. We verify by patching
        synthesize to count calls and asserting both succeed.
        """
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        call_count = [0]

        async def fake_synthesize(self, *, goal, tech, project):
            call_count[0] += 1
            return {"do": [], "dont": [], "neutral": []}

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", fake_synthesize
        )

        first = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        second = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )

        assert first.status_code == 200, first.get_data(as_text=True)
        assert second.status_code == 200, second.get_data(as_text=True)
        assert call_count[0] == 2

    def test_synthesize_surfaces_setup_errors_as_500(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: errors during worker setup (e.g. connect failure)
        must surface as 500, not silently produce a 200 with empty body.
        """
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        def boom(_path):
            raise RuntimeError("connect blew up")

        # Patch connect at the module level the route imports it from.
        monkeypatch.setattr(app_module, "connect", boom)

        response = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "card-error" in body
        assert "connect blew up" in body

    def test_synthesize_closes_httpx_client_per_call(
        self, client: FlaskClient, tmp_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: each synthesis call must aclose() its OllamaChat.

        The OllamaChat constructor builds an httpx.AsyncClient with a
        TCP pool. Without aclose(), every click leaks an unclosed
        client. We verify by counting aclose() calls after two synth
        requests.
        """
        from better_memory.llm.ollama import OllamaChat
        from better_memory.services.reflection import (
            ReflectionSynthesisService,
        )
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "_project_name", lambda: "proj-a")

        async def fake_synthesize(self, *, goal, tech, project):
            return {"do": [], "dont": [], "neutral": []}

        monkeypatch.setattr(
            ReflectionSynthesisService, "synthesize", fake_synthesize
        )

        aclose_count = [0]
        original_aclose = OllamaChat.aclose

        async def counting_aclose(self):
            aclose_count[0] += 1
            await original_aclose(self)

        monkeypatch.setattr(OllamaChat, "aclose", counting_aclose)

        for _ in range(2):
            response = client.post(
                "/observations/synthesize",
                headers={"Origin": "http://localhost"},
            )
            assert response.status_code == 200, response.get_data(as_text=True)

        assert aclose_count[0] == 2, (
            f"expected 2 aclose() calls, got {aclose_count[0]}"
        )

    def test_synthesize_returns_429_when_already_in_flight(self, client):
        """If a synthesis worker is already in-flight (busy flag set),
        a second concurrent request returns 429 immediately."""
        from better_memory.ui import app as _app_module

        _app_module._synth_busy = True
        try:
            resp = client.post(
                "/observations/synthesize",
                headers={"Origin": "http://localhost"},
            )
            assert resp.status_code == 429
            assert b"already in progress" in resp.data
        finally:
            _app_module._synth_busy = False

    def test_synthesize_returns_504_on_timeout(
        self, tmp_path, monkeypatch
    ):
        """If the worker exceeds the timeout, the route returns 504
        with a clear error card. The test creates an app with a tiny
        synth_timeout to avoid actually waiting 60s."""
        from better_memory.ui.app import create_app

        async def _slow_complete(self, prompt):
            await asyncio.sleep(2.0)
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'

        monkeypatch.setattr(OllamaChat, "complete", _slow_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path,
            start_watchdog=False,
            synth_timeout=0.5,
        )
        c = app.test_client()
        resp = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 504
        assert b"timed out" in resp.data

    def test_busy_flag_cleared_after_completion(
        self, tmp_path, monkeypatch
    ):
        """After a synthesis completes (success path), _synth_busy is
        False so the next call goes through."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        async def _ok_complete(self, prompt):
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'

        monkeypatch.setattr(OllamaChat, "complete", _ok_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )
        c = app.test_client()

        resp1 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp1.status_code == 200
        assert _app_module._synth_busy is False

        resp2 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp2.status_code == 200

    def test_busy_flag_cleared_after_exception(
        self, tmp_path, monkeypatch
    ):
        """If the synthesize coroutine raises, _synth_busy is still
        cleared (cleanup happens in the inner finally)."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        async def _bad_complete(self, prompt):
            raise RuntimeError("simulated synthesis failure")

        monkeypatch.setattr(OllamaChat, "complete", _bad_complete)

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )
        c = app.test_client()

        resp = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 500
        assert _app_module._synth_busy is False

    def test_busy_flag_cleared_when_connect_raises_during_setup(
        self, tmp_path, monkeypatch
    ):
        """Regression: if connect() raises during the route's setup,
        _synth_busy must still be released — otherwise the route is
        bricked for the lifetime of the process. The previous version
        of _build_coro raised before _run was created, so _run's
        finally (which clears the flag) was never reached."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )

        # Make connect() raise once it's called from inside _build_coro.
        # Patch the symbol used by app.py's route (not better_memory.db.connection
        # globally — the route imports `connect` at module top).
        original_connect = _app_module.connect
        call_count = [0]

        def _failing_connect(*args, **kwargs):
            call_count[0] += 1
            # Only fail the synthesize route's call, not setup-time
            # calls (e.g. the create_app path).
            if call_count[0] == 1:
                raise RuntimeError("simulated connect failure")
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(_app_module, "connect", _failing_connect)

        c = app.test_client()
        resp1 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp1.status_code == 500
        assert _app_module._synth_busy is False, (
            "busy flag must be released even when setup raises"
        )

        # Restore connect; second request must succeed (not 429).
        monkeypatch.setattr(_app_module, "connect", original_connect)

        async def _ok_complete(self, prompt):
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'
        monkeypatch.setattr(OllamaChat, "complete", _ok_complete)

        resp2 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp2.status_code == 200, (
            f"second request after setup-error should be 200, got "
            f"{resp2.status_code}: {resp2.data[:200]}"
        )

    def test_busy_flag_cleared_when_OllamaChat_raises_during_setup(
        self, tmp_path, monkeypatch
    ):
        """Regression: if OllamaChat() construction raises during the
        route's setup, _synth_busy must still be released."""
        from better_memory.ui import app as _app_module
        from better_memory.ui.app import create_app

        db_path = tmp_path / "memory.db"
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        with connect(db_path) as c:
            apply_migrations(c)

        app = create_app(
            db_path=db_path, start_watchdog=False
        )

        original_OllamaChat = _app_module.OllamaChat
        call_count = [0]

        def _failing_OllamaChat(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated OllamaChat failure")
            return original_OllamaChat(*args, **kwargs)

        monkeypatch.setattr(
            _app_module, "OllamaChat", _failing_OllamaChat
        )

        c = app.test_client()
        resp1 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp1.status_code == 500
        assert _app_module._synth_busy is False, (
            "busy flag must be released even when OllamaChat setup raises"
        )

        # Restore; second request succeeds (not 429).
        monkeypatch.setattr(
            _app_module, "OllamaChat", original_OllamaChat
        )

        async def _ok_complete(self, prompt):
            return '{"new":[],"augment":[],"merge":[],"ignore":[]}'
        monkeypatch.setattr(
            original_OllamaChat, "complete", _ok_complete
        )

        resp2 = c.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp2.status_code == 200, (
            f"second request after setup-error should be 200, got "
            f"{resp2.status_code}: {resp2.data[:200]}"
        )

    def test_release_synth_with_stale_token_does_not_clear_concurrent_flag(
        self
    ):
        """Regression for follow-up Bugbot finding on PR #18 (Low
        severity but real concurrency hazard): the route's
        except-BaseException handler calls _release_synth() AFTER
        _run's finally already called it. Without token-matching, a
        concurrent acquire between those two releases would have
        its busy state cleared by the second release.

        Verifies _release_synth(stale_token) is a no-op when a
        newer request has acquired with a fresh token.
        """
        from better_memory.ui import app as _app_module

        # Reset to known state.
        _app_module._synth_busy = False

        # Request A acquires.
        token_a = _app_module._try_acquire_synth()
        assert token_a is not None
        assert _app_module._synth_busy is True

        # Pretend A's _run finished and released.
        _app_module._release_synth(token_a)
        assert _app_module._synth_busy is False

        # A concurrent Request B acquires (gets a NEW token).
        token_b = _app_module._try_acquire_synth()
        assert token_b is not None
        assert token_b != token_a
        assert _app_module._synth_busy is True

        # A's route then tries to release again (the double-release
        # path that was previously a race). Stale token must not
        # clear B's busy flag.
        _app_module._release_synth(token_a)
        assert _app_module._synth_busy is True, (
            "stale-token release must not clear a concurrent "
            "request's busy flag"
        )

        # B's owner releases with its own token: succeeds.
        _app_module._release_synth(token_b)
        assert _app_module._synth_busy is False

    def test_busy_flag_cleared_when_helper_infrastructure_fails(
        self, client, monkeypatch
    ):
        """Regression for follow-up Bugbot finding on PR #18 (Low
        severity but real): if run_async_in_worker raises a
        non-WorkerTimeout exception (e.g. threading.Thread.start()
        exhaustion, asyncio.new_event_loop() failure inside the
        worker, or coro_factory() raising), the worker's _run
        coroutine may never execute — so _release_synth() inside
        _run's finally never fires. With worker_dispatched=True the
        outer finally also skips release, so the route's
        except-BaseException branch must release the flag itself.
        Idempotent on the typical case where _run did establish its
        finally."""
        from better_memory.ui import app as _app_module

        def _failing_helper(coro_factory, *, timeout=None):
            # Simulate infrastructure failure where _run never executes.
            # Don't call coro_factory() — mimics Thread.start() failure
            # before the worker body could invoke the factory.
            raise RuntimeError("simulated worker infrastructure failure")

        monkeypatch.setattr(
            _app_module, "run_async_in_worker", _failing_helper
        )

        resp = client.post(
            "/observations/synthesize",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 500
        assert b"simulated worker infrastructure failure" in resp.data
        assert _app_module._synth_busy is False, (
            "busy flag must be released when run_async_in_worker raises "
            "a non-WorkerTimeout exception (the worker's _run finally "
            "may never have fired)"
        )

    def test_busy_flag_cleared_when_pre_worker_setup_raises(
        self, client
    ):
        """Regression for Bugbot finding on PR #18 (Low severity but
        real): pre-worker setup (project name, app.extensions lookups
        on app.py:473-476) executes BETWEEN the busy flag acquire and
        the try/except block that wraps run_async_in_worker. If
        anything in that gap raises (e.g. a missing app.extensions
        key), the worker is never dispatched and its inner finally
        never fires — leaking the flag and bricking the route.

        Pops app.extensions['db_path'] to force a KeyError in the
        pre-worker setup path. In TESTING mode Flask propagates the
        exception; in production it returns 500. Either way the busy
        flag must be False after.
        """
        from better_memory.ui import app as _app_module

        original_db_path = client.application.extensions.pop("db_path")
        try:
            try:
                resp = client.post(
                    "/observations/synthesize",
                    headers={"Origin": "http://localhost"},
                )
                # Production path: Flask catches and returns 500.
                assert resp.status_code == 500
            except KeyError:
                # TESTING path: Flask propagates the exception. Expected.
                pass
            assert _app_module._synth_busy is False, (
                "busy flag must be released even when pre-worker "
                "setup raises before run_async_in_worker is called"
            )
        finally:
            client.application.extensions["db_path"] = original_db_path
