"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import escape
from werkzeug.wrappers import Response

from better_memory.config import resolve_home
from better_memory.db.connection import connect
from better_memory.llm.ollama import ChatCompleter, OllamaChat
from better_memory.services.consolidation import ConsolidationService
from better_memory.services.episode import EpisodeService
from better_memory.services.insight import InsightService
from better_memory.services.reflection import ReflectionService
from better_memory.ui import jobs, queries


def _project_name() -> str:
    """Return the current project — cwd name, matching service convention."""
    return Path.cwd().name


def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
    chat: ChatCompleter | None = None,
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
    app.extensions["episode_service"] = EpisodeService(conn=db_conn)
    app.extensions["reflection_service"] = ReflectionService(conn=db_conn)
    app.extensions["_db_path"] = resolved_db
    resolved_chat: ChatCompleter = chat if chat is not None else OllamaChat()
    app.extensions["chat"] = resolved_chat

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
        "partial": "superseded",
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
        return ""

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
        service = app.extensions["insight_service"]
        c = service.get(id)
        if c is None or c.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/candidate_card_expanded.html", c=c
        )

    @app.post("/candidates/<id>/approve")
    def candidate_approve(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        service.update(id, status="confirmed")
        return ""

    @app.post("/candidates/<id>/reject")
    def candidate_reject(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        service.update(id, status="retired")
        return ""

    @app.get("/candidates/<id>/edit")
    def candidate_edit(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/insight_edit_form.html",
            row=existing,
            save_url=url_for("candidate_edit_save", id=id),
            cancel_url=url_for("candidate_compact_card", id=id),
        )

    @app.post("/candidates/<id>/edit")
    def candidate_edit_save(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        title = request.form.get("title", existing.title)
        content = request.form.get("content", existing.content)
        service.update(id, title=title, content=content)
        updated = service.get(id)
        return render_template(
            "fragments/candidate_card_compact.html", c=updated
        )

    @app.get("/candidates/<id>/compact")
    def candidate_compact_card(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "pending_review":
            abort(404)
        return render_template(
            "fragments/candidate_card_compact.html", c=existing
        )

    @app.get("/candidates/<id>/merge")
    def candidate_merge_picker(id: str) -> str:
        service = app.extensions["insight_service"]
        source = service.get(id)
        if source is None or source.status != "pending_review":
            abort(404)
        conn = app.extensions["db_connection"]
        project = _project_name()
        all_candidates = queries.list_candidates(conn, project=project)
        targets = [t for t in all_candidates if t.id != id]
        return render_template(
            "fragments/merge_picker.html", source=source, targets=targets
        )

    @app.post("/candidates/<id>/merge")
    def candidate_merge(id: str) -> tuple[str, int]:
        target_id = request.args.get("target", "")
        if not target_id:
            return (
                '<div class="card card-error">'
                "<p>Missing <code>target</code> query parameter.</p>"
                "</div>"
            ), 200
        db_path = app.extensions["_db_path"]
        chat = app.extensions["chat"]
        try:
            def _do_merge() -> None:
                import asyncio

                # Fresh connection on the worker thread (SQLite is thread-bound).
                conn = connect(db_path)
                try:
                    merge_svc = ConsolidationService(conn=conn, chat=chat)
                    asyncio.run(
                        merge_svc.merge(source_id=id, target_id=target_id)
                    )
                finally:
                    conn.close()

            jobs._run_sync_or_in_worker(_do_merge)
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(exc)}</p>"
                "</div>"
            ), 200
        return "", 200

    @app.get("/insights/<id>/card")
    def insight_card(id: str) -> str:
        service = app.extensions["insight_service"]
        i = service.get(id)
        if i is None or i.status not in ("confirmed", "promoted"):
            abort(404)
        return render_template(
            "fragments/insight_card_expanded.html", i=i
        )

    @app.get("/insights/<id>/promote")
    def insight_promote(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "confirmed":
            abort(404)
        return render_template("fragments/promotion_stub_modal.html")

    @app.post("/insights/<id>/retire")
    def insight_retire(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        service.update(id, status="retired")
        return ""

    @app.post("/insights/<id>/demote")
    def insight_demote(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status != "promoted":
            abort(404)
        service.update(id, status="confirmed")
        return ""

    @app.get("/insights/<id>/edit")
    def insight_edit(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        return render_template(
            "fragments/insight_edit_form.html",
            row=existing,
            save_url=url_for("insight_edit_save", id=id),
            cancel_url=url_for("insight_compact_card", id=id),
        )

    @app.post("/insights/<id>/edit")
    def insight_edit_save(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        title = request.form.get("title", existing.title)
        content = request.form.get("content", existing.content)
        service.update(id, title=title, content=content)
        updated = service.get(id)
        template = (
            "fragments/insight_card_compact.html"
            if updated.status == "confirmed"
            else "fragments/promoted_card_compact.html"
        )
        return render_template(template, i=updated, p=updated)

    @app.get("/insights/<id>/compact")
    def insight_compact_card(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None or existing.status not in ("confirmed", "promoted"):
            abort(404)
        template = (
            "fragments/insight_card_compact.html"
            if existing.status == "confirmed"
            else "fragments/promoted_card_compact.html"
        )
        return render_template(template, i=existing, p=existing)

    @app.get("/insights/<id>/sources")
    def insight_sources(id: str) -> str:
        service = app.extensions["insight_service"]
        existing = service.get(id)
        if existing is None:
            abort(404)
        conn = app.extensions["db_connection"]
        rows = queries.list_insight_sources(conn, insight_id=id)
        return render_template("fragments/insight_sources.html", rows=rows)

    @app.post("/pipeline/consolidate")
    def pipeline_consolidate() -> tuple[str, int, dict[str, str]]:
        db_path = app.extensions["_db_path"]
        chat = app.extensions["chat"]
        state = jobs.start_consolidation_job(
            db_path=db_path, chat=chat, project=_project_name()
        )
        rendered = render_template("fragments/consolidation_job.html", job=state)
        headers = {}
        if state.status == "complete":
            headers["HX-Trigger"] = "job-complete"
        return rendered, 200, headers

    @app.post("/jobs/<id>/apply")
    def jobs_apply(id: str) -> tuple[str, int, dict[str, str]]:
        db_path = app.extensions["_db_path"]
        chat = app.extensions["chat"]
        try:
            state = jobs.apply_job(id, db_path=db_path, chat=chat)
        except LookupError:
            abort(404)
        except ValueError as exc:
            return (
                f'<div class="card card-error"><p>{escape(exc)}</p></div>',
                200,
                {},
            )
        rendered = render_template("fragments/consolidation_job.html", job=state)
        return rendered, 200, {"HX-Trigger": "job-complete"}

    @app.get("/jobs/<id>")
    def jobs_get(id: str) -> tuple[str, int, dict[str, str]]:
        state = jobs.get_job(id)
        if state is None:
            abort(404)
        rendered = render_template("fragments/consolidation_job.html", job=state)
        headers = {}
        if state.status == "complete":
            headers["HX-Trigger"] = "job-complete"
        return rendered, 200, headers

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
