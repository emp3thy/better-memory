# Management UI — Phase 1: Web App Skeleton

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a Flask + HTMX web application that serves the five nav-tab views (Pipeline, Sweep, Knowledge, Audit, Graph) as empty shells, with health, shutdown, Origin-check middleware, inactivity timeout, and an entry point that binds to `127.0.0.1:0` and writes its URL to `$BETTER_MEMORY_HOME/ui.url`.

**Architecture:** Single Flask app factory. `threaded=False` dev server. Routes live in one `app.py`. Templates in `templates/`. Static (HTMX + CSS) served from `static/`. Entry point `__main__.py` uses `werkzeug.serving.make_server` so we can bind to port 0, capture the assigned port, write `ui.url` before accepting traffic, and call `serve_forever()` ourselves.

**Tech Stack:** Python 3.12, Flask, Jinja2 (bundled with Flask), Werkzeug (bundled), HTMX 2.0.8 (vendored JS), plain hand-written CSS, pytest + Flask test client for integration tests.

**Scope (Phase 1 only):**
- Builds everything from spec §2 (Technology and process model) and §3 (Information architecture and routes) _layout shell + `/healthz` + empty views_.
- Does NOT wire up `ObservationService` / `InsightService` / `KnowledgeService`. They come in when a view actually needs them (Phase 2 onwards).
- Does NOT include the `memory.start_ui()` MCP tool (that's Phase 10). This plan builds the child side only; the parent-side spawn logic comes later.
- Candidate-count badge returns `0` for now — the real count lives in Phase 2.

---

## File Structure

### Create

```
better_memory/ui/
  __init__.py                    # empty package marker
  __main__.py                    # python -m better_memory.ui — bind, write ui.url, serve
  app.py                         # Flask app factory, routes, middleware, config
  static/
    htmx.min.js                  # vendored HTMX 2.0.8
    app.css                      # plain dark-theme CSS for the shell
  templates/
    base.html                    # layout shell (header, nav tabs, <main>)
    pipeline.html                # empty view extending base
    sweep.html                   # empty view extending base
    knowledge.html               # empty view extending base
    audit.html                   # empty view extending base
    graph.html                   # empty view extending base
    fragments/
      badge.html                 # candidate-count badge (returns 0)
tests/ui/
  __init__.py                    # empty package marker
  conftest.py                    # Flask test client fixture
  test_app.py                    # route / middleware / timeout unit tests
  test_entrypoint_integration.py # subprocess spawn + ui.url + /healthz
```

### Modify

- `pyproject.toml` — add `flask>=3` to `dependencies`.

---

## Task 1: Add `flask` dependency and create empty UI package

**Files:**
- Modify: `pyproject.toml`
- Create: `better_memory/ui/__init__.py`
- Create: `tests/ui/__init__.py`

- [ ] **Step 1: Edit `pyproject.toml` to add Flask**

Replace the dependencies block with:

```toml
dependencies = [
    "mcp",
    "sqlite-vec",
    "httpx",
    "pydantic>=2",
    "flask>=3",
]
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: Installs Flask and Werkzeug; no other changes.

- [ ] **Step 3: Create empty package markers**

Create `better_memory/ui/__init__.py` with a single line:

```python
"""Management UI — web app spawned by memory.start_ui()."""
```

Create `tests/ui/__init__.py` empty:

```python
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock better_memory/ui/__init__.py tests/ui/__init__.py
git commit -m "UI Phase 1: add flask dependency and ui package scaffold"
```

---

## Task 2: Flask app factory with `/healthz`

**Files:**
- Create: `better_memory/ui/app.py`
- Create: `tests/ui/conftest.py`
- Create: `tests/ui/test_app.py`

- [ ] **Step 1: Write the failing test**

Create `tests/ui/conftest.py`:

```python
"""Shared fixtures for UI tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient

from better_memory.ui.app import create_app


@pytest.fixture
def client() -> Iterator[FlaskClient]:
    """Yield a Flask test client for a freshly created app."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
```

Create `tests/ui/test_app.py`:

```python
"""Unit tests for the UI Flask app."""

from __future__ import annotations

from flask.testing import FlaskClient


class TestHealthz:
    def test_returns_200_with_ok_body(self, client: FlaskClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.data == b"ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'better_memory.ui.app'`

- [ ] **Step 3: Implement the app factory**

Create `better_memory/ui/app.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/conftest.py tests/ui/test_app.py
git commit -m "UI Phase 1: Flask app factory with /healthz"
```

---

## Task 3: Layout shell template and `/` redirect

**Files:**
- Create: `better_memory/ui/templates/base.html`
- Create: `better_memory/ui/templates/pipeline.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_app.py`:

```python
class TestRootRedirect:
    def test_redirects_to_pipeline(self, client: FlaskClient) -> None:
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/pipeline")


class TestLayoutShell:
    def test_pipeline_renders_base_layout(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        assert response.status_code == 200
        body = response.data.decode()
        # All five nav tabs appear in the header
        assert "Pipeline" in body
        assert "Sweep" in body
        assert "Knowledge" in body
        assert "Audit" in body
        assert "Graph" in body
        # Close UI button is rendered
        assert "Close UI" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: Two new tests FAIL — `/` returns 404, `/pipeline` returns 404.

- [ ] **Step 3: Create base template**

Create `better_memory/ui/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{% block title %}better-memory{% endblock %}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
  <script src="{{ url_for('static', filename='htmx.min.js') }}" defer></script>
</head>
<body>
  <header class="app-header">
    <div class="brand">better-memory</div>
    <nav class="tabs">
      <a class="tab {% if active_tab == 'pipeline' %}active{% endif %}" href="{{ url_for('pipeline') }}">
        Pipeline
        <span id="badge"
              hx-get="{{ url_for('pipeline_badge') }}"
              hx-trigger="load, every 10s"
              hx-swap="innerHTML"></span>
      </a>
      <a class="tab {% if active_tab == 'sweep' %}active{% endif %}" href="{{ url_for('sweep') }}">Sweep</a>
      <a class="tab {% if active_tab == 'knowledge' %}active{% endif %}" href="{{ url_for('knowledge') }}">Knowledge</a>
      <a class="tab {% if active_tab == 'audit' %}active{% endif %}" href="{{ url_for('audit') }}">Audit</a>
      <a class="tab {% if active_tab == 'graph' %}active{% endif %}" href="{{ url_for('graph') }}">Graph</a>
    </nav>
    <button class="close-ui"
            hx-post="{{ url_for('shutdown') }}"
            hx-confirm="Shut down the UI?"
            hx-swap="none">Close UI</button>
  </header>
  <main>
    {% block main %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Create pipeline template**

Create `better_memory/ui/templates/pipeline.html`:

```html
{% extends "base.html" %}
{% block title %}Pipeline — better-memory{% endblock %}
{% block main %}
  <section class="view-placeholder">
    <h1>Pipeline</h1>
    <p class="muted">Kanban view arrives in Phase 2.</p>
  </section>
{% endblock %}
```

- [ ] **Step 5: Add routes**

The `base.html` template uses `url_for('sweep')`, `url_for('knowledge')`, `url_for('audit')`, and `url_for('graph')`, so those endpoints must exist before the pipeline page can render. Register stub routes for all four alongside the pipeline route — Task 4 will give each stub its own template.

Replace `better_memory/ui/app.py` entirely with:

```python
"""Flask app factory for the better-memory management UI."""

from __future__ import annotations

from flask import Flask, redirect, render_template, url_for


def create_app() -> Flask:
    """Build and return a configured Flask app."""
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz() -> tuple[str, int]:
        return "ok", 200

    @app.get("/")
    def root():
        return redirect(url_for("pipeline"))

    @app.get("/pipeline")
    def pipeline() -> str:
        return render_template("pipeline.html", active_tab="pipeline")

    @app.get("/pipeline/badge")
    def pipeline_badge() -> str:
        return "0"

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
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/app.py better_memory/ui/templates/ tests/ui/test_app.py
git commit -m "UI Phase 1: base layout shell, / redirect, view stubs"
```

---

## Task 4: Empty view templates for Sweep, Knowledge, Audit, Graph

**Files:**
- Create: `better_memory/ui/templates/sweep.html`
- Create: `better_memory/ui/templates/knowledge.html`
- Create: `better_memory/ui/templates/audit.html`
- Create: `better_memory/ui/templates/graph.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_app.py`:

```python
class TestEmptyViews:
    def test_sweep_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/sweep")
        assert response.status_code == 200
        assert b"Sweep Review" in response.data

    def test_knowledge_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/knowledge")
        assert response.status_code == 200
        assert b"Knowledge Base" in response.data

    def test_audit_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/audit")
        assert response.status_code == 200
        assert b"Audit Timeline" in response.data

    def test_graph_renders_own_placeholder(self, client: FlaskClient) -> None:
        response = client.get("/graph")
        assert response.status_code == 200
        assert b"Graph" in response.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: Four new tests FAIL (all four routes still render `pipeline.html`).

- [ ] **Step 3: Create per-view templates**

Create `better_memory/ui/templates/sweep.html`:

```html
{% extends "base.html" %}
{% block title %}Sweep — better-memory{% endblock %}
{% block main %}
  <section class="view-placeholder">
    <h1>Sweep Review</h1>
    <p class="muted">Sweep queue arrives in Phase 5.</p>
  </section>
{% endblock %}
```

Create `better_memory/ui/templates/knowledge.html`:

```html
{% extends "base.html" %}
{% block title %}Knowledge — better-memory{% endblock %}
{% block main %}
  <section class="view-placeholder">
    <h1>Knowledge Base</h1>
    <p class="muted">KB editor arrives in Phase 6.</p>
  </section>
{% endblock %}
```

Create `better_memory/ui/templates/audit.html`:

```html
{% extends "base.html" %}
{% block title %}Audit — better-memory{% endblock %}
{% block main %}
  <section class="view-placeholder">
    <h1>Audit Timeline</h1>
    <p class="muted">Timeline arrives in Phase 8.</p>
  </section>
{% endblock %}
```

Create `better_memory/ui/templates/graph.html`:

```html
{% extends "base.html" %}
{% block title %}Graph — better-memory{% endblock %}
{% block main %}
  <section class="view-placeholder">
    <h1>Graph</h1>
    <p class="muted">Graph view arrives in Phase 9.</p>
  </section>
{% endblock %}
```

- [ ] **Step 4: Wire the routes to their templates**

Edit `better_memory/ui/app.py` — replace each of the four stub routes (`sweep`, `knowledge`, `audit`, `graph`) to render its own template:

```python
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
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/ better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 1: empty view templates for sweep, knowledge, audit, graph"
```

---

## Task 5: Origin-check middleware

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

**Context:** The UI binds `127.0.0.1:<random>` and does not use auth. Adding an `Origin` / `Referer` check on non-GET requests prevents a malicious page in another browser tab from POSTing to our mutating endpoints cross-origin. GET requests are allowed through because they are safe (no state transition) and because top-nav navigations do not always send `Origin`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_app.py`:

```python
class TestOriginCheck:
    def test_post_without_origin_or_referer_is_rejected(
        self, client: FlaskClient
    ) -> None:
        response = client.post("/shutdown")
        assert response.status_code == 403

    def test_post_with_matching_origin_is_accepted(
        self, client: FlaskClient
    ) -> None:
        # Flask test client "serves" on http://localhost (no port) —
        # SERVER_NAME is localhost by default.
        response = client.post(
            "/shutdown",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 204

    def test_post_with_matching_referer_is_accepted(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/shutdown",
            headers={"Referer": "http://localhost/pipeline"},
        )
        assert response.status_code == 204

    def test_post_with_foreign_origin_is_rejected(
        self, client: FlaskClient
    ) -> None:
        response = client.post(
            "/shutdown",
            headers={"Origin": "http://evil.example.com"},
        )
        assert response.status_code == 403

    def test_get_without_origin_is_allowed(self, client: FlaskClient) -> None:
        response = client.get("/pipeline")
        assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py::TestOriginCheck -v`
Expected: Tests that expect 403 will FAIL (currently returns 204).

- [ ] **Step 3: Add the before_request hook**

Edit `better_memory/ui/app.py`. Add this import at top:

```python
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
```

Inside `create_app()`, before the route definitions, add:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 1: Origin/Referer check on non-GET requests"
```

---

## Task 6: Static assets — HTMX + CSS

**Files:**
- Create: `better_memory/ui/static/htmx.min.js`
- Create: `better_memory/ui/static/app.css`
- Modify: `tests/ui/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_app.py`:

```python
class TestStaticAssets:
    def test_htmx_js_is_served(self, client: FlaskClient) -> None:
        response = client.get("/static/htmx.min.js")
        assert response.status_code == 200
        assert response.content_type.startswith("application/javascript") or \
               response.content_type.startswith("text/javascript")
        # HTMX's minified bundle begins with a standard UMD-ish header;
        # assert something from the real file rather than an exact hash.
        assert b"htmx" in response.data.lower()

    def test_app_css_is_served(self, client: FlaskClient) -> None:
        response = client.get("/static/app.css")
        assert response.status_code == 200
        assert response.content_type.startswith("text/css")
        assert b".app-header" in response.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py::TestStaticAssets -v`
Expected: Both FAIL — 404.

- [ ] **Step 3: Vendor HTMX 2.0.8**

Pin a specific version. Download the minified bundle once and commit it:

```bash
curl -fsSL -o better_memory/ui/static/htmx.min.js \
  https://unpkg.com/htmx.org@2.0.8/dist/htmx.min.js
```

Verify:
- File exists and is ~50 KB.
- `head -c 60 better_memory/ui/static/htmx.min.js` shows HTMX header text.

- [ ] **Step 4: Write `app.css`**

Create `better_memory/ui/static/app.css`:

```css
/* better-memory management UI — dark theme shell */

* {
  box-sizing: border-box;
}

html, body {
  margin: 0;
  padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen,
    Ubuntu, Cantarell, sans-serif;
  font-size: 14px;
  background: #0f0f0f;
  color: #e0e0e0;
  line-height: 1.5;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 18px;
  background: #151515;
  border-bottom: 1px solid #2a2a2a;
}

.app-header .brand {
  font-weight: 600;
  color: #f0f0f0;
}

.app-header .tabs {
  display: flex;
  gap: 4px;
  flex: 1;
}

.app-header .tab {
  padding: 6px 12px;
  border-radius: 4px;
  color: #bbb;
  text-decoration: none;
  font-size: 13px;
}

.app-header .tab:hover {
  background: #1f1f1f;
  color: #f0f0f0;
}

.app-header .tab.active {
  background: #2a2a2a;
  color: #f0f0f0;
}

.app-header .tab #badge {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 10px;
  background: #3a2a1a;
  color: #e0a060;
  font-size: 11px;
  font-weight: 600;
  min-width: 18px;
  display: inline-block;
  text-align: center;
}

.app-header .tab #badge:empty,
.app-header .tab #badge[data-count="0"] {
  display: none;
}

.app-header .close-ui {
  padding: 6px 12px;
  background: #2a2a2a;
  border: 1px solid #3a3a3a;
  border-radius: 4px;
  color: #e0e0e0;
  cursor: pointer;
  font-size: 13px;
}

.app-header .close-ui:hover {
  background: #3a3a3a;
}

main {
  padding: 18px;
}

.view-placeholder {
  max-width: 720px;
  margin: 40px auto;
  text-align: center;
  color: #999;
}

.view-placeholder h1 {
  color: #e0e0e0;
  margin-bottom: 8px;
}

.muted {
  color: #888;
}
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/static/htmx.min.js better_memory/ui/static/app.css tests/ui/test_app.py
git commit -m "UI Phase 1: vendor HTMX 2.0.8 and write shell CSS"
```

---

## Task 7: `/shutdown` endpoint defers `os._exit` via Timer

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

**Context:** `os._exit(0)` kills the process immediately. Calling it from inside a request handler aborts Flask before the response is flushed — the browser sees a broken connection instead of a 204. Spec §2 prescribes `threading.Timer(0.1, os._exit, (0,)).start()` so the handler returns first.

- [ ] **Step 1: Write the failing test**

Append to `tests/ui/test_app.py`:

```python
from unittest.mock import patch


class TestShutdown:
    def test_shutdown_schedules_exit_via_timer(
        self, client: FlaskClient
    ) -> None:
        with patch("better_memory.ui.app.threading.Timer") as mock_timer:
            response = client.post(
                "/shutdown", headers={"Origin": "http://localhost"}
            )
            assert response.status_code == 204
            mock_timer.assert_called_once()
            args, _ = mock_timer.call_args
            assert args[0] == 0.1
            import os as _os
            assert args[1] is _os._exit
            assert args[2] == (0,)
            mock_timer.return_value.start.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_app.py::TestShutdown -v`
Expected: FAIL — `threading` isn't imported in `app.py`, and the stub `shutdown` doesn't schedule anything.

- [ ] **Step 3: Replace the shutdown stub**

Edit `better_memory/ui/app.py`. Add imports at top:

```python
import os
import threading
```

Replace the `shutdown` view inside `create_app()`:

```python
    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        threading.Timer(0.1, os._exit, (0,)).start()
        return "", 204
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 1: /shutdown defers os._exit via threading.Timer"
```

---

## Task 8: Inactivity timeout

**Files:**
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

**Context:** Spec §2 — "30-minute inactivity timeout (reset on every non-`/healthz` request). On expiry, server calls `os._exit(0)`." Every request updates `last_activity` except `/healthz` (so external liveness probes don't keep the UI alive forever).

Implementation design for testability: the idle-check logic lives in a helper that tests can invoke **synchronously** via `app.config["_check_idle"]()`. The production path is a daemon thread that calls the helper in a loop. No real-time sleeps in tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ui/test_app.py`:

```python
import time as _time

from better_memory.ui.app import create_app


class TestInactivityTimeout:
    def test_request_resets_last_activity(self) -> None:
        app = create_app()
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0  # pretend ancient
            c.get("/pipeline")
            # After the request, _last_activity should be ~now.
            assert _time.monotonic() - app.config["_last_activity"] < 0.1

    def test_healthz_does_not_reset_last_activity(self) -> None:
        app = create_app()
        with app.test_client() as c:
            app.config["_last_activity"] = 0.0
            c.get("/healthz")
            # /healthz must not update _last_activity
            assert app.config["_last_activity"] == 0.0

    def test_check_idle_exits_when_over_threshold(self) -> None:
        app = create_app(inactivity_timeout=60.0)
        app.config["_last_activity"] = _time.monotonic() - 120.0  # 2 min idle
        with patch("better_memory.ui.app.os._exit") as mock_exit:
            app.config["_check_idle"]()
            mock_exit.assert_called_once_with(0)

    def test_check_idle_noop_when_under_threshold(self) -> None:
        app = create_app(inactivity_timeout=60.0)
        app.config["_last_activity"] = _time.monotonic()  # just now
        with patch("better_memory.ui.app.os._exit") as mock_exit:
            app.config["_check_idle"]()
            mock_exit.assert_not_called()

    def test_watchdog_thread_started_by_default(self) -> None:
        app = create_app()
        # Name is set in the factory; look for it in the thread roster.
        names = [t.name for t in threading.enumerate()]
        assert "ui-watchdog" in names

    def test_watchdog_thread_skipped_when_disabled(self) -> None:
        # Tests that don't want the thread can pass start_watchdog=False.
        app = create_app(start_watchdog=False)
        assert app.config["_check_idle"]  # helper still registered
```

(Add `import threading` at the top of the test file if not already present.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ui/test_app.py::TestInactivityTimeout -v`
Expected: FAIL — `create_app()` does not accept `inactivity_timeout` / `start_watchdog`, and the helpers don't exist yet.

- [ ] **Step 3: Update the factory**

Edit `better_memory/ui/app.py`. Add imports at top if missing:

```python
import time
```

Change the factory signature:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
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
```

Inside `create_app()`, **after** `_origin_check` (which was added in Task 5) and **before** the route definitions, add:

```python
    app.config["_last_activity"] = time.monotonic()

    @app.before_request
    def _record_activity() -> None:
        if request.path != "/healthz":
            app.config["_last_activity"] = time.monotonic()

    def _check_idle() -> None:
        idle = time.monotonic() - app.config["_last_activity"]
        if idle > inactivity_timeout:
            os._exit(0)

    app.config["_check_idle"] = _check_idle

    if start_watchdog:
        def _watchdog() -> None:
            while True:
                time.sleep(inactivity_poll_interval)
                _check_idle()

        t = threading.Thread(target=_watchdog, daemon=True, name="ui-watchdog")
        t.start()
```

Registration order matters: `_origin_check` must be registered before `_record_activity` so that a 403 short-circuits before we record activity for a would-be rejected request.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_app.py -v`
Expected: All PASS. No real-time sleeps in the assertions, so no flakiness.

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 1: inactivity watchdog with synchronously-testable check"
```

---

## Task 9: `__main__.py` — bind, write `ui.url`, serve

**Files:**
- Create: `better_memory/ui/__main__.py`
- Create: `tests/ui/test_entrypoint_integration.py`

**Context:** This is the actual entry point the (future) `memory.start_ui()` MCP tool will spawn. It must:

1. Build the Flask app.
2. Bind to `127.0.0.1:0` via `werkzeug.serving.make_server` so we can capture the assigned port *before* serving any requests.
3. Atomically write `<BETTER_MEMORY_HOME>/ui.url` containing the full URL.
4. Call `server.serve_forever()`.
5. On exit (normal or via `os._exit` from shutdown/inactivity), best-effort delete `ui.url`.

`BETTER_MEMORY_HOME` resolution reuses `better_memory.config._resolve_home()` (private but safe to call — it's `os.environ.get(...)` with an expanduser). Tests set `BETTER_MEMORY_HOME` to a tmpdir.

- [ ] **Step 1: Write the failing integration test**

Create `tests/ui/test_entrypoint_integration.py`:

```python
"""Integration tests for the UI entry point (`python -m better_memory.ui`).

Launches the real module as a subprocess, waits for ``ui.url`` to appear,
hits ``/healthz`` on the reported URL, then kills the subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def spawn_ui(tmp_path: Path):
    """Spawn the UI subprocess with an isolated BETTER_MEMORY_HOME.

    Yields (process, ui_url_path). Caller is responsible for asserting
    on the URL file; teardown terminates the process.
    """
    proc: subprocess.Popen | None = None

    def _spawn() -> tuple[subprocess.Popen, Path]:
        nonlocal proc
        env = {**os.environ, "BETTER_MEMORY_HOME": str(tmp_path)}
        # Discard child stdout/stderr — werkzeug prints one line per
        # request, and we don't want to risk pipe-buffer deadlocks on
        # platforms with small pipe sizes (notably Windows).
        proc = subprocess.Popen(
            [sys.executable, "-m", "better_memory.ui"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc, tmp_path / "ui.url"

    yield _spawn

    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{path} did not appear within {timeout}s")


class TestEntrypoint:
    def test_writes_ui_url_and_serves_healthz(self, spawn_ui) -> None:
        proc, url_path = spawn_ui()
        _wait_for_file(url_path, timeout=5.0)
        url = url_path.read_text().strip()
        assert url.startswith("http://127.0.0.1:")

        # The server is up by the time ui.url is written.
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as resp:
            assert resp.status == 200
            assert resp.read() == b"ok"

    def test_ui_url_deleted_on_clean_shutdown(self, spawn_ui) -> None:
        proc, url_path = spawn_ui()
        _wait_for_file(url_path, timeout=5.0)
        url = url_path.read_text().strip()

        # Hit /shutdown with a valid Origin — UI schedules os._exit.
        req = urllib.request.Request(
            f"{url}/shutdown",
            method="POST",
            headers={"Origin": url},
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            # os._exit races the response flush; connection reset is OK.
            pass

        # Subprocess should exit shortly.
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "UI did not exit after /shutdown"

        # ui.url should be gone (best-effort cleanup in __main__).
        # Allow a brief moment for the atexit hook to run.
        for _ in range(20):
            if not url_path.exists():
                break
            time.sleep(0.05)
        assert not url_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ui/test_entrypoint_integration.py -v`
Expected: FAIL — `No module named better_memory.ui.__main__`.

- [ ] **Step 3: Write `__main__.py`**

Create `better_memory/ui/__main__.py`:

```python
"""Entry point for the management UI.

Spawned by ``memory.start_ui()`` (Phase 10). Binds to 127.0.0.1:0, writes
the resulting URL atomically to ``$BETTER_MEMORY_HOME/ui.url`` so the
parent process can discover it, then serves forever until killed by
``/shutdown`` or the inactivity watchdog.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from werkzeug.serving import make_server

from better_memory.config import _resolve_home
from better_memory.ui.app import create_app


def _ui_url_path() -> Path:
    return _resolve_home() / "ui.url"


def _write_url_atomically(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(url)
    os.replace(tmp, dest)


def _delete_url(dest: Path) -> None:
    try:
        dest.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    app = create_app()
    server = make_server(host="127.0.0.1", port=0, app=app, threaded=False)
    port = server.port  # werkzeug BaseWSGIServer.port is the bound port
    url = f"http://127.0.0.1:{port}"

    dest = _ui_url_path()
    _write_url_atomically(url, dest)
    atexit.register(_delete_url, dest)

    try:
        server.serve_forever()
    finally:
        _delete_url(dest)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ui/test_entrypoint_integration.py -v`
Expected: Both PASS. The second test (clean shutdown deletes `ui.url`) may be flaky if the process is killed before `atexit` hooks run. `os._exit` does NOT run atexit hooks — the `finally` in `main()` is the only reliable path, but `os._exit` bypasses `finally` too. To make this robust, `/shutdown` and the inactivity watchdog both call `_delete_url` explicitly before `os._exit`.

To fix: the ui.url deletion must happen at **the call site of `os._exit`**, not in `main()`. See Step 5.

- [ ] **Step 5: Move URL cleanup into the `os._exit` call sites**

Edit `better_memory/ui/app.py`. Add these imports at top:

```python
from pathlib import Path

from better_memory.config import _resolve_home
```

Define a helper inside `create_app()` (before the routes):

```python
    def _cleanup_ui_url() -> None:
        try:
            (_resolve_home() / "ui.url").unlink()
        except FileNotFoundError:
            pass
```

Update the `shutdown` view:

```python
    @app.post("/shutdown")
    def shutdown() -> tuple[str, int]:
        def _exit() -> None:
            _cleanup_ui_url()
            os._exit(0)
        threading.Timer(0.1, _exit).start()
        return "", 204
```

Update `_check_idle` to clean up before exiting:

```python
    def _check_idle() -> None:
        idle = time.monotonic() - app.config["_last_activity"]
        if idle > inactivity_timeout:
            _cleanup_ui_url()
            os._exit(0)
```

The shutdown test in `test_app.py` (Task 7) asserts `threading.Timer` is called with `os._exit`. Update that test to assert the Timer is called with a callable and position-0 delay of 0.1:

```python
class TestShutdown:
    def test_shutdown_schedules_exit_via_timer(
        self, client: FlaskClient
    ) -> None:
        with patch("better_memory.ui.app.threading.Timer") as mock_timer:
            response = client.post(
                "/shutdown", headers={"Origin": "http://localhost"}
            )
            assert response.status_code == 204
            mock_timer.assert_called_once()
            args, _ = mock_timer.call_args
            assert args[0] == 0.1
            assert callable(args[1])
            mock_timer.return_value.start.assert_called_once()
```

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest tests/ui/ -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/__main__.py better_memory/ui/app.py tests/ui/test_entrypoint_integration.py tests/ui/test_app.py
git commit -m "UI Phase 1: __main__ entry point + ui.url lifecycle"
```

---

## Task 10: Fragment template for the candidate-count badge

**Files:**
- Create: `better_memory/ui/templates/fragments/badge.html`
- Modify: `better_memory/ui/app.py`
- Modify: `tests/ui/test_app.py`

**Context:** The badge route currently returns a bare `"0"` string. The CSS hides the badge when its content is empty, so for Phase 1 (where the count is always zero) we want the fragment to render an empty string. Phase 2 will plug in real candidate counts and the template will emit the number.

- [ ] **Step 1: Replace the existing badge assertion**

In `tests/ui/test_app.py` find the test `TestLayoutShell.test_pipeline_renders_base_layout` — leave it as-is — and append a new class below it:

```python
class TestBadgeFragment:
    def test_badge_empty_when_zero(self, client: FlaskClient) -> None:
        response = client.get("/pipeline/badge")
        assert response.status_code == 200
        assert response.content_type.startswith("text/html")
        # Phase 1: always zero ⇒ CSS hides the badge ⇒ fragment is empty.
        assert response.data.strip() == b""

    def test_badge_template_renders_number_when_positive(
        self, client: FlaskClient
    ) -> None:
        # Render the template directly with a non-zero count, proving
        # the Phase-2-ready code path works without needing to stub the
        # view or mock the DB.
        from flask import render_template

        with client.application.app_context():
            out = render_template("fragments/badge.html", count=7)
            assert out == "7"
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `uv run pytest tests/ui/test_app.py::TestBadgeFragment -v`
Expected: `test_badge_empty_when_zero` FAILS (current route returns `b"0"`, not `b""`).

- [ ] **Step 3: Create the fragment template**

Create `better_memory/ui/templates/fragments/badge.html`:

```html
{%- if count and count > 0 -%}{{ count }}{%- endif -%}
```

The `{%- -%}` whitespace-control markers ensure no leading/trailing whitespace in the output. Empty string when `count == 0`; the number otherwise.

- [ ] **Step 4: Update the route**

Replace the `pipeline_badge` view in `better_memory/ui/app.py`:

```python
    @app.get("/pipeline/badge")
    def pipeline_badge() -> str:
        return render_template("fragments/badge.html", count=0)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ui/ -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/ better_memory/ui/app.py tests/ui/test_app.py
git commit -m "UI Phase 1: badge fragment renders empty when count is zero"
```

---

## Task 11: Smoke-run end-to-end and document

**Files:**
- Modify: `README.md` (append a short "Management UI" section)

- [ ] **Step 1: Manually smoke-run the server**

Run in one terminal:

```bash
BETTER_MEMORY_HOME=/tmp/bm-smoke uv run python -m better_memory.ui
```

In another terminal:

```bash
cat /tmp/bm-smoke/ui.url   # should print http://127.0.0.1:<port>
curl $(cat /tmp/bm-smoke/ui.url)/healthz
# expected: ok
curl -sI $(cat /tmp/bm-smoke/ui.url)/pipeline | head -1
# expected: HTTP/1.1 200 OK
```

Open the URL in a browser. Verify:
- Header renders with all five nav tabs.
- Pipeline, Sweep, Knowledge, Audit, Graph pages each show their placeholder message.
- Nav "Pipeline" tab does not show a visible badge (the `:empty` CSS hides it when HTMX has not yet loaded content, and `data-count="0"` hides it after load).
- Clicking "Close UI" (after confirming the browser prompt) shuts the server down; `/tmp/bm-smoke/ui.url` is deleted.

- [ ] **Step 2: Append a short README section**

Append the following block to `README.md` (note: the outer fence uses tildes so the inner triple-backtick fence nests cleanly):

~~~markdown
## Management UI

Spawn the UI on demand (Phase 10 of Plan 2 will expose this as the
`memory.start_ui()` MCP tool). Until then, start it manually:

```bash
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
cat ~/.better-memory/ui.url   # print the bound URL
```

The UI exits after 30 minutes of inactivity, or when you click
**Close UI** in the header.
~~~

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "UI Phase 1: document manual UI launch in README"
```

---

## Self-Review Checklist

Before handing off, walk through the spec §2 and §3 requirements and confirm every one is covered:

| Spec item | Task |
|---|---|
| Flask + Jinja2 | 2 |
| HTMX vendored | 6 |
| Hand-written CSS, no framework | 6 |
| Dark theme only | 6 |
| `flask>=3` added to pyproject | 1 |
| Bind `127.0.0.1:0` via werkzeug make_server | 9 |
| Child writes `ui.url`, deletes on exit | 9, 5 (shutdown path) |
| `GET /healthz` returns 200 ok | 2 |
| 30-min inactivity timeout + `os._exit` | 8 |
| `POST /shutdown` deferred via Timer | 7 |
| Origin / Referer check on non-GET | 5 |
| `sqlite3.Connection` at startup | **NOT in Phase 1** — added in Phase 2 when a view first needs DB access |
| `threaded=False` Flask/Werkzeug | 9 (`make_server(..., threaded=False)`) |
| Five-tab layout shell with Close UI | 3, 4 |
| Pipeline badge fragment polling every 10s | 3, 10 |
| Empty placeholder views | 3, 4 |
| Directory layout (`__main__`, `app`, `static`, `templates`, `fragments/`) | 1, 3, 6, 9, 10 |

If any row is unchecked at the end of execution, add a follow-up task before declaring Phase 1 done.

Not in this plan — intentionally deferred to the phase that first needs them:
- `queries.py` (Phase 2 uses it for kanban counts)
- `jobs.py` (Phase 2 uses it for consolidation jobs)
- Service wiring (`ObservationService` etc.) (Phase 2)
- `single-instance guard` using `ui.pid` (Phase 10 — the guard is parent-side logic in `memory.start_ui()`)
