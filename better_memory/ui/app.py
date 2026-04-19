"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

import os
import threading
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from werkzeug.wrappers import Response


def create_app() -> Flask:
    """Build and return a configured Flask app.

    A factory rather than a module-level ``app`` so tests can spin up
    fresh instances in isolation.
    """
    app = Flask(__name__)

    def _host_of(url: str | None) -> str | None:
        if not url:
            return None
        try:
            return urlparse(url).netloc or None
        except ValueError:
            return None

    @app.before_request
    def _origin_check() -> None:
        if request.method == "GET":
            return
        expected_host = request.host  # e.g. "localhost" or "127.0.0.1:54321"
        origin_host = _host_of(request.headers.get("Origin"))
        referer_host = _host_of(request.headers.get("Referer"))
        if origin_host == expected_host or referer_host == expected_host:
            return
        abort(403)

    @app.get("/healthz")
    def healthz() -> tuple[str, int]:
        return "ok", 200

    @app.get("/")
    def root() -> Response:
        return redirect(url_for("pipeline"))

    @app.get("/pipeline")
    def pipeline() -> str:
        return render_template("pipeline.html", active_tab="pipeline")

    @app.get("/pipeline/badge")
    def pipeline_badge() -> str:
        return ""

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
        threading.Timer(0.1, os._exit, (0,)).start()
        return "", 204

    return app
