"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

from flask import Flask, redirect, render_template, url_for
from werkzeug.wrappers import Response


def create_app() -> Flask:
    """Build and return a configured Flask app.

    A factory rather than a module-level ``app`` so tests can spin up
    fresh instances in isolation.
    """
    app = Flask(__name__)

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

    # Stubs — Task 4 gives each its own template.
    @app.get("/sweep")
    def sweep() -> str:
        return render_template("pipeline.html", active_tab="sweep")

    @app.get("/knowledge")
    def knowledge() -> str:
        return render_template("pipeline.html", active_tab="knowledge")

    @app.get("/audit")
    def audit() -> str:
        return render_template("pipeline.html", active_tab="audit")

    @app.get("/graph")
    def graph() -> str:
        return render_template("pipeline.html", active_tab="graph")

    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        # Real shutdown comes in Task 7.
        return "", 204

    return app
