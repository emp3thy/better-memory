"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import escape
from werkzeug.wrappers import Response

from better_memory.config import get_config, resolve_home
from better_memory.db.connection import connect
from better_memory.llm.ollama import OllamaChat
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import ReflectionService, ReflectionSynthesisService
from better_memory.ui import queries


def _project_name() -> str:
    """Return the current project — cwd name, matching service convention."""
    return Path.cwd().name


def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
) -> Flask:
    """Build and return a configured Flask app.

    Parameters
    ----------
    inactivity_timeout:
        Seconds without a non-``/healthz`` request before the server
        calls ``os._exit(0)``. Default 30 minutes.
    inactivity_poll_interval:
        Seconds between watchdog-thread liveness checks. Default 30 s.
    start_watchdog:
        If ``False``, skip starting the background watchdog thread.
        ``_check_idle`` is still registered so tests can drive it
        synchronously without spawning threads.
    """
    app = Flask(__name__)

    # Resolve DB path from arg or config.
    resolved_db = db_path if db_path is not None else resolve_home() / "memory.db"
    db_conn = connect(resolved_db)

    app.extensions["db_connection"] = db_conn
    app.extensions["episode_service"] = EpisodeService(conn=db_conn)
    app.extensions["reflection_service"] = ReflectionService(conn=db_conn)

    # Synthesis runs in a worker thread (sqlite3 connections aren't
    # thread-safe by default) with a fresh asyncio event loop per call
    # (httpx.AsyncClient pools are bound to the loop they were created
    # on; reusing a shared client across loops produces transport errors
    # after the first loop closes). So we store only the *config* here
    # — the route builds a fresh OllamaChat + DB connection +
    # ReflectionSynthesisService inside the worker each time.
    config = get_config()
    app.extensions["ollama_host"] = config.ollama_host
    app.extensions["consolidate_model"] = config.consolidate_model
    app.extensions["db_path"] = resolved_db

    @app.teardown_appcontext
    def _close_db_on_teardown(_exc: BaseException | None) -> None:
        # Flask calls this after every request in an app context. We keep
        # the connection open for the life of the app (shared single-request
        # model with threaded=False), so do nothing per-request. The
        # connection is closed when the process exits.
        return None

    def _cleanup_ui_url() -> None:
        try:
            (resolve_home() / "ui.url").unlink()
        except FileNotFoundError:
            pass

    def _host_of(url: str | None) -> str | None:
        if not url:
            return None
        try:
            return urlparse(url).netloc or None
        except ValueError:
            return None

    @app.before_request
    def _origin_check() -> None:
        if request.method in ("GET", "HEAD"):
            return
        expected_host = request.host  # e.g. "localhost" or "127.0.0.1:54321"
        origin_host = _host_of(request.headers.get("Origin"))
        referer_host = _host_of(request.headers.get("Referer"))
        if origin_host == expected_host or referer_host == expected_host:
            return
        abort(403)

    app.config["_last_activity"] = time.monotonic()

    import json as _json

    @app.template_filter("decode_hints")
    def _decode_hints(raw: str | None) -> list[str]:
        """Decode the hints column for template display.

        Hints are stored as ``json.dumps(list[str])`` by the synthesis
        service and (now) the UI edit handler. This filter decodes the
        JSON; if the column contains a plain-text legacy value (or any
        non-JSON), falls back to a single-element list so the UI
        renders something readable rather than crashing.
        """
        if not raw:
            return []
        try:
            value = _json.loads(raw)
        except (ValueError, TypeError):
            return [raw]
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    @app.before_request
    def _record_activity() -> None:
        if request.path != "/healthz":
            app.config["_last_activity"] = time.monotonic()

    def _check_idle() -> None:
        idle = time.monotonic() - app.config["_last_activity"]
        if idle > inactivity_timeout:
            _cleanup_ui_url()
            os._exit(0)

    app.config["_check_idle"] = _check_idle

    if start_watchdog:
        def _watchdog() -> None:
            while True:
                time.sleep(inactivity_poll_interval)
                _check_idle()

        t = threading.Thread(target=_watchdog, daemon=True, name="ui-watchdog")
        t.start()

    @app.get("/healthz")
    def healthz() -> tuple[str, int]:
        return "ok", 200

    @app.get("/")
    def root() -> Response:
        return redirect(url_for("episodes"))

    @app.get("/episodes")
    def episodes() -> str:
        return render_template("episodes.html", active_tab="episodes")

    @app.get("/episodes/panel")
    def episodes_panel() -> str:
        conn = app.extensions["db_connection"]
        rows = queries.episode_list_for_ui(conn, project=_project_name())
        # Group by ISO date prefix (YYYY-MM-DD) of started_at, preserving
        # newest-first ordering. itertools.groupby works because rows are
        # already sorted by started_at DESC.
        from itertools import groupby

        days = [
            (day, list(group))
            for day, group in groupby(
                rows, key=lambda r: r.started_at[:10]
            )
        ]
        return render_template(
            "fragments/panel_episodes.html", days=days
        )

    @app.get("/episodes/banner")
    def episodes_banner() -> str:
        conn = app.extensions["db_connection"]
        count = queries.unclosed_episode_count(
            conn, project=_project_name()
        )
        return render_template(
            "fragments/episode_banner.html", count=count
        )

    @app.get("/episodes/<id>/drawer")
    def episodes_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.episode_detail(conn, episode_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/episode_drawer.html", detail=detail
        )

    _DEFAULT_CLOSE_REASONS = {
        "success": "goal_complete",
        "partial": "plan_complete",
        "abandoned": "abandoned",
        "no_outcome": "session_end_reconciled",
    }

    @app.post("/episodes/<id>/close")
    def episode_close(id: str) -> tuple[str, int, dict[str, str]]:
        outcome = request.args.get("outcome", "")
        if outcome not in _DEFAULT_CLOSE_REASONS:
            return (
                f'<div class="card card-error">'
                f"<p>Invalid outcome: {escape(outcome)}</p>"
                "</div>"
            ), 400, {}
        conn = app.extensions["db_connection"]
        if queries.episode_detail(conn, episode_id=id) is None:
            abort(404)
        try:
            app.extensions["episode_service"].close_by_id(
                episode_id=id,
                outcome=outcome,
                close_reason=_DEFAULT_CLOSE_REASONS[outcome],
            )
        except ValueError as exc:
            # close_by_id raises for "already closed" or "not found".
            # We already checked existence, so this path is the
            # already-closed race — return 409 with an error card.
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        # Re-render the drawer (now showing the closed view) and fire
        # episode-closed so the timeline reloads.
        detail = queries.episode_detail(conn, episode_id=id)
        rendered = render_template(
            "fragments/episode_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "episode-closed"}

    @app.get("/reflections")
    def reflections() -> str:
        return render_template(
            "reflections.html",
            active_tab="reflections",
            # The filter-form initial state mirrors the no-filter
            # default — current project, status=active, no others.
            initial_filters={
                "project": _project_name(),
                "tech": "",
                "phase": "",
                "polarity": "",
                "status": "",
                "min_confidence": "",
            },
        )

    @app.get("/reflections/panel")
    def reflections_panel() -> str:
        conn = app.extensions["db_connection"]
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        project = _arg("project") or _project_name()
        tech = _arg("tech")
        phase = _arg("phase")
        polarity = _arg("polarity")
        status = _arg("status")

        min_conf_raw = _arg("min_confidence")
        try:
            min_confidence = float(min_conf_raw) if min_conf_raw else 0.0
        except ValueError:
            min_confidence = 0.0

        rows = queries.reflection_list_for_ui(
            conn,
            project=project,
            tech=tech,
            phase=phase,
            polarity=polarity,
            status=status,
            min_confidence=min_confidence,
        )
        return render_template(
            "fragments/panel_reflections.html", rows=rows
        )

    @app.get("/reflections/<id>/drawer")
    def reflections_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.reflection_detail(conn, reflection_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/reflection_drawer.html", detail=detail
        )

    @app.post("/reflections/<id>/confirm")
    def reflection_confirm(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        try:
            app.extensions["reflection_service"].confirm(reflection_id=id)
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.post("/reflections/<id>/retire")
    def reflection_retire(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        try:
            app.extensions["reflection_service"].retire(reflection_id=id)
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.get("/reflections/<id>/edit")
    def reflection_edit_form(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.reflection_detail(conn, reflection_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/reflection_edit_form.html", detail=detail
        )

    @app.post("/reflections/<id>/edit")
    def reflection_edit_save(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        use_cases = request.form.get("use_cases", "")
        hints = request.form.get("hints", "")
        # Validate empties at the route boundary (input-validation = 400)
        # so the service-layer ValueError can mean only "lifecycle block"
        # (= 409). Avoids fragile error-message string matching.
        if not use_cases.strip() or not hints.strip():
            return (
                '<div class="card card-error">'
                "<p>use_cases and hints must both be non-empty</p>"
                "</div>"
            ), 400, {}
        try:
            app.extensions["reflection_service"].update_text(
                reflection_id=id, use_cases=use_cases, hints=hints,
            )
        except ValueError as exc:
            # After the empty-check above, the only remaining ValueError
            # path is "Cannot edit reflection in status 'retired'/'superseded'".
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.get("/observations")
    def observations() -> str:
        return render_template(
            "observations.html", active_tab="observations"
        )

    @app.get("/observations/panel")
    def observations_panel() -> str:
        conn = app.extensions["db_connection"]
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        project = _arg("project") or _project_name()
        rows = queries.observation_list_for_ui(
            conn,
            project=project,
            status=_arg("status"),
            outcome=_arg("outcome"),
            component=_arg("component"),
        )
        from itertools import groupby

        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.created_at[:10])
        ]
        return render_template(
            "fragments/panel_observations.html", days=days
        )

    @app.get("/observations/<id>/drawer")
    def observation_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.observation_detail(conn, observation_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/observation_drawer.html", detail=detail
        )

    @app.post("/observations/synthesize")
    def observations_synthesize() -> tuple[str, int, dict[str, str]]:
        # synthesize() is async; the route is sync. We can't use
        # asyncio.run() because pyproject.toml sets pytest-asyncio's
        # asyncio_mode = "auto", so tests run inside an active event
        # loop and asyncio.run would raise. Dispatch to a fresh-loop
        # daemon thread instead — works in production (no loop) and
        # in tests (loop already running).
        #
        # The thread also opens its OWN sqlite3 connection (sqlite
        # enforces same-thread use) AND its own OllamaChat (httpx
        # AsyncClient pools are bound to the loop they were created
        # on; sharing across short-lived loops produces transport
        # errors after the first close).
        project = _project_name()
        ollama_host = app.extensions["ollama_host"]
        consolidate_model = app.extensions["consolidate_model"]
        db_path = app.extensions["db_path"]

        result: dict | None = None
        exc_holder: list[BaseException] = []

        def _run() -> None:
            nonlocal result
            try:
                local_conn = connect(db_path)
                try:
                    chat = OllamaChat(
                        host=ollama_host, model=consolidate_model
                    )
                    svc = ReflectionSynthesisService(local_conn, chat=chat)
                    loop = asyncio.new_event_loop()
                    try:
                        result = loop.run_until_complete(svc.synthesize(
                            goal="manual synthesis",
                            tech=None,
                            project=project,
                        ))
                    finally:
                        loop.close()
                finally:
                    local_conn.close()
            except BaseException as _exc:  # noqa: BLE001
                exc_holder.append(_exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join()

        if exc_holder:
            exc = exc_holder[0]
            return (
                f'<div class="card card-error"><p>{escape(str(exc))}</p></div>',
                500,
                {},
            )
        counts = {k: len(v) for k, v in (result or {}).items()}
        rendered = render_template(
            "fragments/observations_synth_banner.html", counts=counts,
        )
        return rendered, 200, {"HX-Trigger": "observations-synthesized"}

    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        def _exit() -> None:
            _cleanup_ui_url()
            os._exit(0)
        threading.Timer(0.1, _exit).start()
        return "", 204

    return app
