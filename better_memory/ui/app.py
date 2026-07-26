"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import escape
from werkzeug.wrappers import Response

from better_memory.config import get_config, project_name, resolve_home
from better_memory.db.connection import connect
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.services.episode import EpisodeService
from better_memory.services.reflection import ReflectionService
from better_memory.storage.factory import build_backend
from better_memory.ui import queries


class _Unset:
    """Sentinel type distinguishing "caller did not pass sync_embedder"
    (auto-detect from config) from an explicit ``sync_embedder=None``
    (force-disable, e.g. in tests that don't want any embedding side
    effects). A dedicated class (rather than a bare ``object()``) lets
    pyright narrow the parameter type via ``isinstance`` in create_app.
    """

    __slots__ = ()


_UNSET = _Unset()


def _build_sync_embedder() -> SyncEmbedder | None:
    """Build the shared SyncEmbedder used by every write-path service.

    Only wired for the ollama embeddings backend — sqlite (FTS5) indexes
    via DB triggers instead of a Python embedder (mirrors the same gate
    in better_memory/mcp/server.py and better_memory/storage/sqlite.py).
    """
    if get_config().embeddings_backend != "ollama":
        return None
    return SyncEmbedder(lambda: OllamaEmbedder(timeout=5.0, max_retries=1))


def _reflection_drawer_detail(app: Flask, id: str) -> SimpleNamespace | None:
    """Compose the drawer view model: row via backend.reflection_get; provenance
    via the local conn ONLY when the backend supports it (gated out in
    agentcore). Returns None when the reflection does not exist."""
    backend = app.extensions["backend"]
    row = backend.reflection_get(reflection_id=id)
    if row is None:
        return None
    sources = (
        queries.reflection_provenance(app.extensions["db_connection"], reflection_id=id)
        if backend.supports_provenance
        else []
    )
    return SimpleNamespace(reflection=SimpleNamespace(**row), sources=sources)


def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
    sync_embedder: SyncEmbedder | None | _Unset = _UNSET,
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
    sync_embedder:
        Shared :class:`SyncEmbedder` passed to every UI write-path
        service (``ReflectionService``, ``SemanticMemoryService``) so
        UI-driven writes embed exactly like MCP-driven writes. Defaults
        to auto-detecting from ``get_config().embeddings_backend`` via
        :func:`_build_sync_embedder`. Pass explicitly (including
        ``None``) to override — tests use this to inject a fake
        embedder or to force-disable embedding.
    """
    app = Flask(__name__)

    # Resolve DB path from arg or config.
    resolved_db = db_path if db_path is not None else resolve_home() / "memory.db"
    db_conn = connect(resolved_db)

    resolved_sync_embedder: SyncEmbedder | None = (
        _build_sync_embedder()
        if isinstance(sync_embedder, _Unset)
        else sync_embedder
    )

    app.extensions["db_connection"] = db_conn
    app.extensions["episode_service"] = EpisodeService(conn=db_conn)
    app.extensions["reflection_service"] = ReflectionService(
        conn=db_conn, sync_embedder=resolved_sync_embedder,
    )
    app.extensions["sync_embedder"] = resolved_sync_embedder
    app.extensions["db_path"] = resolved_db
    app.extensions["backend"] = build_backend(
        config=get_config(),
        memory_conn=db_conn,
        sync_embedder=resolved_sync_embedder,
        session_id=None,
        project=project_name(),
    )

    @app.context_processor
    def _inject_caps() -> dict[str, object]:
        b = app.extensions["backend"]
        return {"caps": {
            "supports_episodes": b.supports_episodes,
            "supports_observations": b.supports_observations,
            "supports_provenance": b.supports_provenance,
            "supports_retention_runs": b.supports_retention_runs,
            "supports_reflection_review": b.supports_reflection_review,
            "supports_reflection_text_edit": b.supports_reflection_text_edit,
        }}

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

    _RATING_CHIP_CLASS = {
        "cited": "outcome-success",
        "shaped": "outcome-success",
        "misled": "outcome-failure",
        "overlooked": "outcome-partial",
        "ignored": "outcome-no_outcome",
    }

    @app.template_filter("rating_chip_class")
    def _rating_chip_class(classification: str) -> str:
        """Map a rating classification to its `.chip`/`.outcome-badge` class.

        cited/shaped -> outcome-success, misled -> outcome-failure,
        overlooked -> outcome-partial, ignored (and anything unknown) ->
        outcome-no_outcome.
        """
        return _RATING_CHIP_CLASS.get(classification, "outcome-no_outcome")

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
        """Gated on supports_episodes: 404 in agentcore mode (episode
        grouping is internal to AgentCore's sessionId, no local table)."""
        if not app.extensions["backend"].supports_episodes:
            abort(404)
        return render_template("episodes.html", active_tab="episodes")

    @app.get("/episodes/panel")
    def episodes_panel() -> str:
        """Gated on supports_episodes, same as episodes()."""
        if not app.extensions["backend"].supports_episodes:
            abort(404)
        conn = app.extensions["db_connection"]
        rows = queries.episode_list_for_ui(conn, project=project_name())
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
        """Gated on supports_episodes, same as episodes()."""
        if not app.extensions["backend"].supports_episodes:
            abort(404)
        conn = app.extensions["db_connection"]
        count = queries.unclosed_episode_count(
            conn, project=project_name()
        )
        return render_template(
            "fragments/episode_banner.html", count=count
        )

    @app.get("/episodes/<id>/drawer")
    def episodes_drawer(id: str) -> str:
        """Gated on supports_episodes, same as episodes()."""
        if not app.extensions["backend"].supports_episodes:
            abort(404)
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
        """Gated on supports_episodes, same as episodes()."""
        if not app.extensions["backend"].supports_episodes:
            abort(404)
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
        backend = app.extensions["backend"]
        current = project_name()
        db_projects = backend.distinct_projects()
        # Union + sort so the current project is always selectable, even
        # when no reflections exist for it yet.
        projects = sorted(
            {current, *db_projects}, key=lambda s: s.casefold()
        )
        return render_template(
            "reflections.html",
            active_tab="reflections",
            projects=projects,
            # The filter-form initial state mirrors the no-filter
            # default — current project, status=active, no others.
            initial_filters={
                "project": current,
                "tech": "",
                "phase": "",
                "polarity": "",
                "status": "",
                "min_confidence": "",
                "useful_only": False,
            },
        )

    @app.get("/reflections/panel")
    def reflections_panel() -> str:
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        project = _arg("project") or project_name()
        tech = _arg("tech")
        phase = _arg("phase")
        polarity = _arg("polarity")
        status = _arg("status")

        min_conf_raw = _arg("min_confidence")
        try:
            min_confidence = float(min_conf_raw) if min_conf_raw else 0.0
        except ValueError:
            min_confidence = 0.0

        useful_only = args.get("useful_only") == "1"

        rows = app.extensions["backend"].reflection_list(
            project=project,
            tech=tech,
            phase=phase,
            polarity=polarity,
            status=status,
            min_confidence=min_confidence,
            useful_only=useful_only,
            limit=100,
        )
        return render_template(
            "fragments/panel_reflections.html", rows=rows
        )

    @app.get("/reflections/<id>/drawer")
    def reflections_drawer(id: str) -> str:
        detail = _reflection_drawer_detail(app, id)
        if detail is None:
            abort(404)
        rating_evidence = queries.fetch_rating_evidence(
            app.extensions["db_connection"], "reflection", id
        )
        return render_template(
            "fragments/reflection_drawer.html",
            detail=detail, rating_evidence=rating_evidence,
        )

    @app.post("/reflections/<id>/confirm")
    def reflection_confirm(id: str) -> tuple[str, int, dict[str, str]]:
        """Gated on supports_reflection_review: 404 in agentcore mode (no
        pending_review status to confirm out of)."""
        if not app.extensions["backend"].supports_reflection_review:
            abort(404)
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
        rating_evidence = queries.fetch_rating_evidence(
            conn, "reflection", id
        )
        rendered = render_template(
            "fragments/reflection_drawer.html",
            detail=detail, rating_evidence=rating_evidence,
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.post("/reflections/<id>/retire")
    def reflection_retire(id: str) -> tuple[str, int, dict[str, str]]:
        if _reflection_drawer_detail(app, id) is None:
            abort(404)
        try:
            app.extensions["backend"].retire_reflection(reflection_id=id)
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = _reflection_drawer_detail(app, id)
        rating_evidence = queries.fetch_rating_evidence(
            app.extensions["db_connection"], "reflection", id
        )
        rendered = render_template(
            "fragments/reflection_drawer.html",
            detail=detail, rating_evidence=rating_evidence,
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.get("/reflections/<id>/edit")
    def reflection_edit_form(id: str) -> str:
        """Gated on supports_reflection_text_edit: 404 in agentcore mode
        (agentcore reflection content is not locally editable)."""
        if not app.extensions["backend"].supports_reflection_text_edit:
            abort(404)
        conn = app.extensions["db_connection"]
        detail = queries.reflection_detail(conn, reflection_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/reflection_edit_form.html", detail=detail
        )

    @app.post("/reflections/<id>/edit")
    def reflection_edit_save(id: str) -> tuple[str, int, dict[str, str]]:
        """Gated on supports_reflection_text_edit, same as
        reflection_edit_form()."""
        if not app.extensions["backend"].supports_reflection_text_edit:
            abort(404)
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
        rating_evidence = queries.fetch_rating_evidence(
            conn, "reflection", id
        )
        rendered = render_template(
            "fragments/reflection_drawer.html",
            detail=detail, rating_evidence=rating_evidence,
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.post("/reflections/<id>/promote")
    def reflection_promote(id: str) -> tuple[str, int, dict[str, str]]:
        if _reflection_drawer_detail(app, id) is None:
            abort(404)
        try:
            app.extensions["backend"].promote_reflection(reflection_id=id)
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = _reflection_drawer_detail(app, id)
        rating_evidence = queries.fetch_rating_evidence(
            app.extensions["db_connection"], "reflection", id
        )
        rendered = render_template(
            "fragments/reflection_drawer.html",
            detail=detail, rating_evidence=rating_evidence,
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}

    @app.get("/semantic")
    def semantic() -> str:
        return render_template(
            "semantic.html",
            active_tab="semantic",
            initial_filters={"scope_filter": "", "search": ""},
        )

    @app.get("/semantic/panel")
    def semantic_panel() -> str:
        project = request.args.get("project") or project_name()
        scope_filter = (request.args.get("scope_filter") or "").strip() or None
        if scope_filter not in ("project", "general", None):
            scope_filter = None
        search = (request.args.get("search") or "").strip() or None
        backend = app.extensions["backend"]
        rows = backend.semantic_list(
            project=project, scope_filter=scope_filter, search=search,
        )
        return render_template(
            "fragments/panel_semantic.html", rows=rows, project=project,
        )

    @app.post("/semantic")
    def semantic_create() -> tuple[str, int, dict[str, str]]:
        project = project_name()
        content = request.form.get("content", "").strip()
        scope = request.form.get("scope") or "project"
        try:
            app.extensions["backend"].semantic_observe(
                content=content, project=project, scope=scope,
            )
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})

    @app.post("/semantic/<id>/scope")
    def semantic_scope(id: str) -> tuple[str, int, dict[str, str]]:
        scope = request.form.get("scope") or "project"
        try:
            app.extensions["backend"].semantic_set_scope(id=id, scope=scope)
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})

    @app.post("/semantic/<id>/delete")
    def semantic_delete(id: str) -> tuple[str, int, dict[str, str]]:
        try:
            app.extensions["backend"].semantic_delete(id=id)  # sqlite: idempotent
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})

    @app.get("/semantic/<id>/drawer")
    def semantic_drawer(id: str):
        conn = app.extensions["db_connection"]
        memory = app.extensions["backend"].semantic_get(id=id)
        if memory is None:
            abort(404)
        rating_evidence = queries.fetch_rating_evidence(conn, "semantic", id)
        return render_template(
            "fragments/semantic_drawer.html",
            memory=memory, rating_evidence=rating_evidence,
        )

    @app.post("/semantic/<id>/update")
    def semantic_update(id: str) -> tuple[str, int, dict[str, str]]:
        content = request.form.get("content", "").strip()
        try:
            app.extensions["backend"].semantic_update_text(id=id, content=content)
        except (ValueError, RuntimeError) as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        return ("", 200, {"HX-Trigger": "semantic-changed"})

    @app.get("/observations")
    def observations() -> str:
        """Gated on supports_observations: 404 in agentcore mode (no local
        observation table to browse)."""
        if not app.extensions["backend"].supports_observations:
            abort(404)
        conn = app.extensions["db_connection"]
        return render_template(
            "observations.html",
            active_tab="observations",
            projects=queries.observation_distinct_projects(conn),
        )

    @app.get("/observations/panel")
    def observations_panel() -> str:
        """Gated on supports_observations, same as observations()."""
        if not app.extensions["backend"].supports_observations:
            abort(404)
        conn = app.extensions["db_connection"]
        args = request.args

        def _arg(name: str) -> str | None:
            v = args.get(name, "").strip()
            return v or None

        rows = queries.observation_list_for_ui(
            conn,
            project=_arg("project"),
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
        """Gated on supports_observations, same as observations()."""
        if not app.extensions["backend"].supports_observations:
            abort(404)
        conn = app.extensions["db_connection"]
        detail = queries.observation_detail(conn, observation_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/observation_drawer.html", detail=detail
        )

    @app.post("/observations/<id>/promote-to-semantic")
    def observation_promote_to_semantic(
        id: str,
    ) -> tuple[str, int, dict[str, str]]:
        """Gated on supports_observations, same as observations()."""
        if not app.extensions["backend"].supports_observations:
            abort(404)
        from better_memory.services.semantic import SemanticMemoryService
        from markupsafe import escape
        conn = app.extensions["db_connection"]
        scope = request.form.get("scope") or "project"
        svc = SemanticMemoryService(
            conn, sync_embedder=app.extensions["sync_embedder"],
        )
        try:
            svc.create_from_observation(observation_id=id, scope=scope)
        except ValueError as exc:
            return (
                f'<div class="card card-error">{escape(str(exc))}</div>',
                400, {},
            )
        rendered = render_template(
            "fragments/observation_promoted_card.html", scope=scope,
        )
        return (
            rendered, 200,
            {"HX-Trigger": "observations-changed semantic-changed"},
        )

    @app.get("/diagnostics")
    def diagnostics() -> str:
        conn = app.extensions["db_connection"]
        recent_ratings = conn.execute(
            """
            SELECT e.rated_at, e.memory_kind, e.memory_id, e.classification,
                   e.evidence,
                   COALESCE(r.title, s.content) AS display
              FROM session_memory_exposure e
              LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                            AND e.memory_id = r.id
              LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                            AND e.memory_id = s.id
             WHERE e.rated_at IS NOT NULL
             ORDER BY e.rated_at DESC
             LIMIT 20
            """
        ).fetchall()
        diag_rows = conn.execute(
            "SELECT metric, value FROM rating_diagnostics"
        ).fetchall()
        rating_diagnostics = {r["metric"]: r["value"] for r in diag_rows}
        overlooked_total = conn.execute(
            "SELECT "
            "(SELECT COALESCE(SUM(times_overlooked), 0) FROM reflections) "
            "+ "
            "(SELECT COALESCE(SUM(times_overlooked), 0) FROM semantic_memories) "
            "AS total"
        ).fetchone()["total"]
        return render_template(
            "diagnostics.html",
            active_tab="diagnostics",
            recent_ratings=recent_ratings,
            rating_diagnostics=rating_diagnostics,
            overlooked_total=overlooked_total,
        )

    @app.get("/diagnostics/panel/hook-errors")
    def hook_errors_panel() -> str:
        conn = app.extensions["db_connection"]
        rows = queries.hook_errors_list_for_ui(conn)
        from itertools import groupby
        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.created_at[:10])
        ]
        return render_template(
            "fragments/panel_hook_errors.html", days=days
        )

    @app.get("/diagnostics/panel/retention-runs")
    def retention_runs_panel() -> str:
        """Gated on supports_retention_runs: 404 in agentcore mode (no local
        retention_runs table; AgentCore expiry is managed internally)."""
        if not app.extensions["backend"].supports_retention_runs:
            abort(404)
        conn = app.extensions["db_connection"]
        rows = queries.retention_runs_list_for_ui(conn)
        from itertools import groupby
        days = [
            (day, list(group))
            for day, group in groupby(rows, key=lambda r: r.run_at[:10])
        ]
        return render_template(
            "fragments/panel_retention_runs.html", days=days
        )

    @app.get("/diagnostics/hook-errors/<id>/drawer")
    def hook_error_drawer(id: str) -> str:
        conn = app.extensions["db_connection"]
        detail = queries.hook_error_detail(conn, error_id=id)
        if detail is None:
            abort(404)
        return render_template(
            "fragments/hook_error_drawer.html", detail=detail
        )

    @app.post("/diagnostics/hook-errors/<id>/delete")
    def hook_error_delete(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        conn.execute(
            "DELETE FROM hook_errors WHERE id = ?", (id,)
        )
        conn.commit()
        return "", 200, {"HX-Trigger": "hook-errors-changed"}

    @app.post("/diagnostics/hook-errors/purge")
    def hook_errors_purge() -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        conn.execute("DELETE FROM hook_errors")
        conn.commit()
        return "", 200, {"HX-Trigger": "hook-errors-changed"}

    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        def _exit() -> None:
            _cleanup_ui_url()
            os._exit(0)
        threading.Timer(0.1, _exit).start()
        return "", 204

    return app
