"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from better_memory.config import resolve_home
from better_memory.db.connection import connect
from better_memory.services.insight import InsightService
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
    app.extensions["insight_service"] = InsightService(conn=db_conn)

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
        return redirect(url_for("pipeline"))

    @app.get("/pipeline")
    def pipeline() -> str:
        counts = queries.kanban_counts(
            app.extensions["db_connection"], project=_project_name()
        )
        return render_template(
            "pipeline.html",
            active_tab="pipeline",
            active_stage="candidates",
            counts=counts,
        )

    @app.get("/pipeline/panel/<stage>")
    def pipeline_panel(stage: str) -> str:
        conn = app.extensions["db_connection"]
        project = _project_name()
        if stage == "observations":
            return render_template(
                "fragments/panel_observations.html",
                rows=queries.list_observations(conn, project=project),
            )
        if stage == "candidates":
            return render_template(
                "fragments/panel_candidates.html",
                rows=queries.list_candidates(conn, project=project),
            )
        if stage == "insights":
            return render_template(
                "fragments/panel_insights.html",
                rows=queries.list_insights(conn, project=project),
            )
        if stage == "promoted":
            return render_template(
                "fragments/panel_promoted.html",
                rows=queries.list_promoted(conn, project=project),
            )
        abort(404)

    @app.get("/candidates/<id>/card")
    def candidate_card(id: str) -> str:
        return ""  # Task 8 implements

    @app.post("/candidates/<id>/approve")
    def candidate_approve(id: str) -> str:
        return ""  # Task 10 implements

    @app.post("/candidates/<id>/reject")
    def candidate_reject(id: str) -> str:
        return ""  # Task 10 implements

    @app.get("/insights/<id>/card")
    def insight_card(id: str) -> str:
        return ""  # Task 8 implements

    @app.get("/insights/<id>/promote")
    def insight_promote(id: str) -> str:
        return ""  # Task 12 implements

    @app.post("/insights/<id>/retire")
    def insight_retire(id: str) -> str:
        return ""  # Task 11 implements

    @app.post("/insights/<id>/demote")
    def insight_demote(id: str) -> str:
        return ""  # Task 11 implements

    @app.post("/pipeline/consolidate")
    def pipeline_consolidate() -> str:
        # Task 13 implements this.
        return ""

    @app.get("/pipeline/badge")
    def pipeline_badge() -> str:
        counts = queries.kanban_counts(
            app.extensions["db_connection"], project=_project_name()
        )
        return render_template("fragments/badge.html", count=counts.candidates)

    @app.get("/sweep")
    def sweep() -> str:
        return render_template("sweep.html", active_tab="sweep")

    @app.get("/knowledge")
    def knowledge() -> str:
        return render_template("knowledge.html", active_tab="knowledge")

    @app.get("/audit")
    def audit() -> str:
        return render_template("audit.html", active_tab="audit")

    @app.get("/graph")
    def graph() -> str:
        return render_template("graph.html", active_tab="graph")

    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        def _exit() -> None:
            _cleanup_ui_url()
            os._exit(0)
        threading.Timer(0.1, _exit).start()
        return "", 204

    return app
