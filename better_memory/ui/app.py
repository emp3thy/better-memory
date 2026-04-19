"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    """Build and return a configured Flask app.

    A factory rather than a module-level ``app`` so tests can spin up
    fresh instances in isolation.
    """
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz() -> tuple[str, int]:
        return "ok", 200

    return app
