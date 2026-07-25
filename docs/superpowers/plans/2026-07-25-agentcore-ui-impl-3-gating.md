# Phase G — PR 3: Gating + dropdown (hide non-agentcore surfaces)

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this phase task-by-task. Each task is one commit. Steps follow strict TDD: write failing test -> run (FAIL) -> minimal impl -> run (PASS) -> commit.

**Goal:** In agentcore mode the UI hides the surfaces AgentCore cannot back — the Episodes/Observations nav + routes, provenance sections + their fetch, the retention-runs panel, the reflection Confirm action, and inline reflection text-edit — and the Reflections project dropdown becomes AgentCore-native (`ListActors` UNION the migration ledger). sqlite mode is byte-for-byte unchanged.

**Depends on:** PR 1 + PR 2. Those phases already shipped, and this phase **consumes** (does not redefine):
- The six capability flags on `StorageBackend` (`@property -> bool`): `supports_episodes` (pre-existing), `supports_observations`, `supports_provenance`, `supports_retention_runs`, `supports_reflection_review`, `supports_reflection_text_edit`. SqliteBackend all `True`; AgentCoreBackend all `False`.
- `create_app` builds `app.extensions["backend"] = build_backend(config=get_config(), memory_conn=db_conn, sync_embedder=resolved_sync_embedder, session_id=None, project=project_name())`, keeps `app.extensions["db_connection"]` for OPERATIONAL state, and registers a `@app.context_processor` that returns `{"caps": <dict of the six flags read off app.extensions["backend"]>}`, rebuilt each render so swapping the extension flips every gate.
- Backend content methods already routed through `app.extensions["backend"]`: `reflection_list(...)`, `reflection_get(*, reflection_id) -> dict | None` (**row only, NO provenance**), `semantic_get(*, id)`, `semantic_observe/semantic_list/semantic_update_text/semantic_set_scope/semantic_delete`, `promote_reflection`, `retire_reflection`, `retrieve`. The `AgentCoreBackend.semantic_list(scope_filter=None)` project+general fan-out bug is **already fixed in PR 2** — not this phase.

**Tech stack:** Python 3.12, Flask/Jinja/htmx, sqlite, boto3 stubbed via `MagicMock` (tests/storage/test_agentcore_unit.py pattern), pytest, pyright.

**Authoritative design:** `docs/superpowers/specs/2026-07-25-agentcore-ui-design.md`. Reconciliation + dependency graph: `docs/superpowers/plans/2026-07-25-agentcore-ui-MASTER-plan.md`. Sub-plan mined (flag names TRANSLATED to canonical): `docs/superpowers/plans/2026-07-25-agentcore-ui-capability-gating.md` (T3-T8).

---

## Phase G guardrails (read before drafting code)

- **[[keep-docs-in-sync]]** *(0.95)* — the docs task (G5) updates `website/architecture.md` + `website/agentcore-setup.md` + the gated-route/protocol docstrings. `README.md`, `website/mcp-tools.md`, and `website/configuration.md` are **unaffected** by this phase (no new MCP tools, no new env keys, no config surface) — G5 states this explicitly.
- **[[server-boot-real-call]]** *(0.65)* — the agentcore nav/gating tests must drive a **real route** through the stubbed backend (e.g. `GET /reflections` returns 200 and renders 3 rail links), not merely assert the app constructs. Any leaked local-content read that the phase has not yet replaced must be visible/asserted, not silently tolerated.
- **[[guard-needs-triggering-test]]** *(0.8)* — every named edge-guard gets a test that seeds a value which actually triggers it: G4's namespace-parse rule (`projects/{p}/... -> p`, `general/... -> general`), G4's best-effort `ListActors` AWS-degrade (client raises -> ledger-only; both empty/failing -> `[]`).
- **[[brutalist-css-classes]]** *(0.75)* — gated template blocks **wrap existing markup** in `{% if caps.* %}`; no Bootstrap utility classes, no new class names. Reuse `.chip`, `.polarity-badge`, `.rating-badge`, `.drawer-section`, etc.
- **[[playwright-domtext]]** *(0.8)* — nav/gating tests assert element **presence/absence** (element count via a parser), never CSS-transformed label text.

## Global constraints

- Test command: `./.venv/Scripts/python.exe -m pytest <path> -v`. pyright: 0 errors. NO live AWS (MagicMock clients only).
- **sqlite behaviour byte-identical**: every flag is `True` on sqlite so no gate fires; the existing `tests/ui/*` suite is the standing preservation pin and passes unchanged.
- ASCII only; ruff line length 100; one commit per task; footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. No new env keys.
- All AWS reads best-effort: agentcore read methods degrade (ledger-only / empty), never raise into a route or the dropdown.

## Verified-against-source facts (do not re-derive)

| Fact | Where |
|---|---|
| Nav is 5 static rail links (Episodes/Observations/Reflections/Semantic/Diagnostics), hardcoded `<a class="rail-link">` | `better_memory/ui/templates/base.html:90-111` |
| Episodes routes: `/episodes`, `/episodes/panel`, `/episodes/banner`, `/episodes/<id>/drawer`, `/episodes/<id>/close` | `ui/app.py:205-288` |
| Observations routes: `/observations`, `/observations/panel`, `/observations/<id>/drawer`, `/observations/<id>/promote-to-semantic` | `ui/app.py:606-675` |
| Reflection drawer template renders "Source observations" from `detail.sources` (provenance) + optional "Rating evidence" from `rating_evidence` | `fragments/reflection_drawer.html:86-135` |
| Observation drawer template renders "Linked reflections" from `detail.reflections` (provenance) | `fragments/observation_drawer.html:25-39` |
| Reflection drawer Confirm button shown only when `status == 'pending_review'`; Edit button always in the `pending_review/confirmed` action block | `fragments/reflection_drawer.html:49-84` |
| Diagnostics mounts hook-errors panel (`hook_errors_panel`), retention-runs panel (`retention_runs_panel`), Recent ratings table, Rating diagnostics `<dl>` | `diagnostics.html:5-51` |
| Reflection drawer is re-rendered by `/reflections/<id>/drawer`, `/confirm`, `/retire`, `/edit` POST, `/promote` — each calls `queries.reflection_detail(...)` + `queries.fetch_rating_evidence(...)` at HEAD; **Content C6 already rewired `/drawer`, `/promote`, `/retire` to the `_reflection_drawer_detail(app, id)` helper**, so G2 extends that helper rather than the raw `reflection_detail` call | `ui/app.py:354-485` |
| Confirm route `/reflections/<id>/confirm` POST; edit routes `GET`+`POST /reflections/<id>/edit`; retention panel route `/diagnostics/panel/retention-runs` | `ui/app.py:368-460,727-738` |
| `/reflections` route builds the dropdown from `queries.reflection_distinct_projects(conn)` unioned with `project_name()`, sorted casefold | `ui/app.py:290-315` |
| `queries.reflection_distinct_projects` = `SELECT DISTINCT project FROM reflections WHERE project IS NOT NULL AND project != '' ORDER BY project COLLATE NOCASE` | `ui/queries.py:561-573` |
| `agentcore_migration` ledger has `namespace TEXT NOT NULL` (`projects/{p}/reflections/`, `general/semantic/`), NO `project` column; `ensure_ledger(conn)` creates it | `storage/agentcore_migrate.py:51-63,292-294` |
| AgentCore namespaces: `projects/{actor}/reflections/`, `.../semantic/`, `.../retired/`, `general/reflections/`, `general/semantic/`; parent does NOT roll up | `storage/session.py:42-60` |
| AgentCore reflection statuses: `active`(==sqlite `confirmed`)/`promoted`/`retired`; NO `pending_review` | `storage/agentcore_migrate.py:42-46`; design Grounding |
| `AgentCoreBackend.__init__(*, config, data_client, control_client, session_id, project, local_conn=None)`; NO `list_actors` wrapper exists yet | `storage/agentcore.py:73-97`; grep |
| `_ClientError` import guard + `_retry_on_transient_404` best-effort pattern available for reuse | `storage/agentcore.py:31-47,985-1010` |

---

## Task G0 (fixture prerequisite folded into G1)

The agentcore gating tests share one fixture. G1 Step 3 adds it to `tests/ui/conftest.py`; G2-G4 reuse it. It builds the normal sqlite app (so operational routes + the local conn still work) then swaps `app.extensions["backend"]` for a flags-all-`False` fake exposing the handful of content methods the still-reachable agentcore routes call. This isolates the gating logic from boto without a live factory build.

---

## Task G1: Gate the nav + Episodes/Observations routes

**Files:**
- Modify: `better_memory/ui/templates/base.html` (wrap Episodes + Observations rail links)
- Modify: `better_memory/ui/app.py` (guard every `/episodes*` and `/observations*` route with a capability check -> 404)
- Test: `tests/ui/conftest.py` (add shared `agentcore_client` fixture), `tests/ui/test_nav_gating.py` (new)

**Interfaces:**
- Consumes: `caps.supports_episodes`, `caps.supports_observations` (templates); `app.extensions["backend"].supports_episodes`, `.supports_observations` (route guards).
- Produces: `base.html` wraps the Episodes link in `{% if caps.supports_episodes %}` and the Observations link in `{% if caps.supports_observations %}`; the other three links always render. Each `/episodes...` and `/observations...` route body begins with a guard that `abort(404)`s when its flag is `False`. sqlite: flags `True`, guards inert, all five links + routes reachable exactly as today.

**Steps:**

- [ ] **Step 1 — Add the shared fixture.** Append to `tests/ui/conftest.py`:

```python
from typing import Any

from flask import Flask


class _FakeAgentCoreBackend:
    """Flags-all-False stand-in for AgentCoreBackend used by gating tests.

    Only the content methods the *still-reachable* agentcore routes call
    are stubbed; gated-off routes 404 before touching the backend.
    """

    supports_episodes = False
    supports_observations = False
    supports_provenance = False
    supports_retention_runs = False
    supports_reflection_review = False
    supports_reflection_text_edit = False

    def reflection_list(self, **_kwargs: Any) -> list[Any]:
        return []

    def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
        return None

    def semantic_list(self, **_kwargs: Any) -> list[Any]:
        return []

    def distinct_projects(self) -> list[str]:
        return []


@pytest.fixture
def agentcore_client(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FlaskClient]:
    """Flask client whose ``app.extensions['backend']`` is the all-False
    fake, so every ``caps.*`` gate reads False and every route guard fires.

    The context processor reads ``caps`` off ``app.extensions['backend']``
    at render time (PR 2 wiring), so swapping the extension flips the gates
    without a live boto/factory build.
    """
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    app.extensions["backend"] = _FakeAgentCoreBackend()
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c
```

- [ ] **Step 2 — Failing tests.** Create `tests/ui/test_nav_gating.py`:

```python
"""Nav + route gating: agentcore hides Episodes/Observations, sqlite shows all."""

from __future__ import annotations

from html.parser import HTMLParser

from flask.testing import FlaskClient


class _RailLinkCounter(HTMLParser):
    """Count <a class="rail-link ...">; assert presence/absence, not text."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        d = dict(attrs)
        cls = d.get("class") or ""
        if "rail-link" in cls.split():
            self.hrefs.append(d.get("href") or "")


def _rail_hrefs(html: str) -> list[str]:
    p = _RailLinkCounter()
    p.feed(html)
    return p.hrefs


def test_sqlite_shows_all_five_rail_links(client: FlaskClient) -> None:
    html = client.get("/reflections").get_data(as_text=True)
    hrefs = _rail_hrefs(html)
    assert len(hrefs) == 5
    assert any("/episodes" in h for h in hrefs)
    assert any("/observations" in h for h in hrefs)


def test_agentcore_hides_episodes_and_observations_links(
    agentcore_client: FlaskClient,
) -> None:
    # [[server-boot-real-call]] — drives a real route through the stubbed backend.
    resp = agentcore_client.get("/reflections")
    assert resp.status_code == 200
    hrefs = _rail_hrefs(resp.get_data(as_text=True))
    assert len(hrefs) == 3
    assert not any("/episodes" in h for h in hrefs)
    assert not any("/observations" in h for h in hrefs)


def test_agentcore_episodes_routes_404(agentcore_client: FlaskClient) -> None:
    assert agentcore_client.get("/episodes").status_code == 404
    assert agentcore_client.get("/episodes/panel").status_code == 404
    assert agentcore_client.get("/episodes/banner").status_code == 404


def test_agentcore_observations_routes_404(agentcore_client: FlaskClient) -> None:
    assert agentcore_client.get("/observations").status_code == 404
    assert agentcore_client.get("/observations/panel").status_code == 404


def test_sqlite_episodes_and_observations_reachable(client: FlaskClient) -> None:
    assert client.get("/episodes").status_code == 200
    assert client.get("/observations").status_code == 200
```

- [ ] **Step 3 — Run (FAIL).** `./.venv/Scripts/python.exe -m pytest tests/ui/test_nav_gating.py -v` — FAIL (links still render; routes still 200).

- [ ] **Step 4 — Implement.** In `base.html` wrap the two links:

```jinja
{% if caps.supports_episodes %}
<a class="rail-link {% if active_tab == 'episodes' %}active{% endif %}" href="{{ url_for('episodes') }}">
  <span class="rail-num">01</span>
  <span class="rail-label">Episodes</span>
</a>
{% endif %}
{% if caps.supports_observations %}
<a class="rail-link {% if active_tab == 'observations' %}active{% endif %}" href="{{ url_for('observations') }}">
  <span class="rail-num">02</span>
  <span class="rail-label">Observations</span>
</a>
{% endif %}
```

In `ui/app.py`, add a guard as the first statement of every `/episodes*` route body (`episodes`, `episodes_panel`, `episodes_banner`, `episodes_drawer`, `episode_close`):

```python
if not app.extensions["backend"].supports_episodes:
    abort(404)
```

and the analogous guard (`supports_observations`) as the first statement of every `/observations*` route body (`observations`, `observations_panel`, `observation_drawer`, `observation_promote_to_semantic`).

- [ ] **Step 5 — Run (PASS).** `./.venv/Scripts/python.exe -m pytest tests/ui/test_nav_gating.py -v` then `./.venv/Scripts/python.exe -m pytest tests/ui -q` — all pass (sqlite pins unchanged).

- [ ] **Step 6 — Commit.** `feat(ui): gate Episodes + Observations nav and routes by capability`.

**Sqlite preservation:** flags `True` -> both links render and every `/episodes*` / `/observations*` route reaches its unchanged body; existing `tests/ui/test_episodes.py` + `test_observations.py` pass unedited.

---

## Task G2: Gate provenance sections + provenance data-fetch

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html` (wrap "Source observations" section)
- Modify: `better_memory/ui/templates/fragments/observation_drawer.html` (wrap "Linked reflections" section)
- Modify: `better_memory/ui/app.py` (extend the `_reflection_drawer_detail` helper from Content C6 to gate its provenance fetch on `supports_provenance`)
- Test: `tests/ui/test_provenance_gating.py` (new)

**Interfaces:**
- Consumes: `caps.supports_provenance` (templates); `app.extensions["backend"].supports_provenance` + `app.extensions["backend"].reflection_get(reflection_id=...)` (row-only path from PR 2).
- Produces: both drawers wrap their provenance sections in `{% if caps.supports_provenance %}`. The `_reflection_drawer_detail(app, id)` helper (added in Content C6, already called by `/drawer`, `/retire`, `/promote`; `/confirm` + `/edit` are 404 in agentcore per G3) is modified to fetch provenance via `queries.reflection_provenance` ONLY when `supports_provenance`, else `detail.sources` is `[]` (gated-out section). sqlite: flag `True`, provenance fetched via `queries.reflection_provenance` and rendered exactly as C6 established.

**Steps:**

- [ ] **Step 1 — Failing tests.** Create `tests/ui/test_provenance_gating.py`:

```python
"""Provenance gating: agentcore drawers omit provenance and take the row-only fetch."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.services.reflection import ReflectionService


def _seed_reflection(db_path: str) -> str:
    conn = connect(db_path)
    try:
        svc = ReflectionService(conn=conn, sync_embedder=None)
        ref = svc.create(
            project="testproj",
            title="Prefer batched writes",
            use_cases="When writing many records",
            hints='["batch them"]',
            polarity="do",
            phase="implementation",
            tech="sqlite",
            confidence=0.8,
        )
        conn.commit()
        return ref.id
    finally:
        conn.close()


def test_sqlite_reflection_drawer_shows_provenance(
    client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    html = client.get(f"/reflections/{rid}/drawer").get_data(as_text=True)
    assert "Source observations" in html


def test_agentcore_reflection_drawer_omits_provenance_and_uses_row_only(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    fake = agentcore_client.application.extensions["backend"]

    def _row_only(*, reflection_id: str) -> dict[str, Any]:
        return {
            "id": reflection_id,
            "project": "testproj",
            "title": "Prefer batched writes",
            "tech": "sqlite",
            "phase": "implementation",
            "polarity": "do",
            "confidence": 0.8,
            "status": "active",
            "scope": "project",
            "evidence_count": 0,
            "updated_at": "2026-07-25T00:00:00Z",
            "use_cases": "When writing many records",
            "hints": '["batch them"]',
            "useful_count": 0,
            "last_useful_at": None,
            "times_overlooked": 0,
            "last_overlooked_at": None,
            "times_misled": 0,
            "last_misled_at": None,
        }

    # Spy: the provenance-join query must NOT be called on the row-only path.
    with patch(
        "better_memory.ui.app.queries.reflection_provenance"
    ) as prov_join, patch.object(fake, "reflection_get", side_effect=_row_only):
        resp = agentcore_client.get(f"/reflections/{rid}/drawer")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Source observations" not in html
    prov_join.assert_not_called()


def test_observation_drawer_provenance_conditional_present(tmp_path) -> None:
    # Observations tab is gated off in agentcore (G1), but the template
    # conditional must still guard the linked-reflections block. Assert the
    # sqlite render includes the block only when data + flag allow.
    from better_memory.ui.app import create_app

    app = create_app(start_watchdog=False, db_path=tmp_path / "memory.db")
    with app.app_context():
        from flask import render_template

        class _Caps:
            supports_provenance = False

        detail = type("D", (), {
            "observation": type("O", (), {
                "outcome": "success", "created_at": "x", "content": "c",
                "id": "o1", "component": None, "theme": None, "tech": None,
                "trigger_type": None, "status": "active",
                "reinforcement_score": 0, "episode_id": None,
            })(),
            "reflections": [type("R", (), {
                "polarity": "do", "confidence": 0.9, "title": "t",
                "status": "active",
            })()],
            "audit": [],
        })()
        html = render_template(
            "fragments/observation_drawer.html", detail=detail, caps=_Caps(),
        )
    assert "Linked reflections" not in html
```

> Note: match `ReflectionService.create(...)`'s real kwargs to the source before running; adjust the seed helper if the signature differs. The gating assertions (`prov_join.assert_not_called()`, `"Source observations" not in html`) are the load-bearing checks.

- [ ] **Step 2 — Run (FAIL).** `./.venv/Scripts/python.exe -m pytest tests/ui/test_provenance_gating.py -v` — FAIL (provenance still rendered; join still called).

- [ ] **Step 3 — Implement.** In `reflection_drawer.html`, wrap the whole "Source observations" `<section>` (lines ~86-120) in `{% if caps.supports_provenance %} ... {% endif %}`. In `observation_drawer.html`, wrap the "Linked reflections" block:

```jinja
{% if caps.supports_provenance and detail.reflections %}
  <section class="linked-reflections">
    ...existing markup unchanged...
  </section>
{% endif %}
```

In `ui/app.py`, **modify the `_reflection_drawer_detail(app, id)` helper added in Content Task C6** — do NOT add a new helper and do NOT replace any `queries.reflection_detail` fetch (C6 already routed `reflections_drawer` / `reflection_promote` / `reflection_retire` through `_reflection_drawer_detail`). Gate its provenance fetch on `supports_provenance`:

```python
def _reflection_drawer_detail(app, id):
    """Compose the drawer view model: row via backend.reflection_get; provenance
    via the local conn ONLY when the backend supports it (gated out in agentcore).
    Returns None when the reflection does not exist."""
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
```

The three routes (`reflections_drawer`, `reflection_promote`, `reflection_retire`) already call `_reflection_drawer_detail(app, id)` from C6, so no route-body edit is needed here — only the helper body changes. `queries.fetch_rating_evidence(conn, "reflection", id)` stays unchanged (operational/local). `reflection_confirm` + `reflection_edit_save` still call `queries.reflection_detail` and are gated to 404 in G3.

- [ ] **Step 4 — Run (PASS).** `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass (sqlite pins unchanged; on sqlite `supports_provenance` is `True`, so `_reflection_drawer_detail` fetches provenance via `queries.reflection_provenance` — the identical render C6 established).

- [ ] **Step 5 — Commit.** `feat(ui): gate reflection/observation provenance sections + row-only fetch`.

**Sqlite preservation:** flag `True` -> `_reflection_drawer_detail` fetches provenance via `queries.reflection_provenance` (the same rows C6 composes), and both templates render their provenance sections unchanged; existing `tests/ui/test_reflections.py` pins hold.

---

## Task G3: Gate retention-runs panel + Confirm action + inline text-edit

**Files:**
- Modify: `better_memory/ui/templates/diagnostics.html` (wrap the retention-runs panel mount)
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html` (wrap Confirm button in `supports_reflection_review`, Edit button in `supports_reflection_text_edit`)
- Modify: `better_memory/ui/app.py` (guard `/diagnostics/panel/retention-runs`, `/reflections/<id>/confirm`, `GET`+`POST /reflections/<id>/edit`)
- Test: `tests/ui/test_diagnostics_reflection_gating.py` (new)

**Interfaces:**
- Consumes: `caps.supports_retention_runs`, `caps.supports_reflection_review`, `caps.supports_reflection_text_edit` (templates); the same off `app.extensions["backend"]` (route guards).
- Produces: the retention-runs `<h2>` + panel `<div>` are wrapped in `{% if caps.supports_retention_runs %}`, and `retention_runs_panel` `abort(404)`s when off. The Confirm button is wrapped in `{% if caps.supports_reflection_review %}` and `reflection_confirm` guards. The Edit button + both edit routes (`reflection_edit_form` GET, `reflection_edit_save` POST) are wrapped/guarded by `supports_reflection_text_edit`. The hook-errors panel, Recent-ratings table, and Rating-diagnostics `<dl>` stay **ungated** (operational/local, visible in both modes). sqlite: all flags `True`, everything present and reachable as today.

**Steps:**

- [ ] **Step 1 — Failing tests.** Create `tests/ui/test_diagnostics_reflection_gating.py`:

```python
"""Gating for retention-runs panel, Confirm action, and inline text-edit."""

from __future__ import annotations

from flask.testing import FlaskClient

from better_memory.db.connection import connect
from better_memory.services.reflection import ReflectionService


def _seed_reflection(db_path: str) -> str:
    conn = connect(db_path)
    try:
        svc = ReflectionService(conn=conn, sync_embedder=None)
        ref = svc.create(
            project="testproj",
            title="Prefer batched writes",
            use_cases="When writing many records",
            hints='["batch them"]',
            polarity="do",
            phase="implementation",
            tech="sqlite",
            confidence=0.8,
        )
        conn.commit()
        return ref.id
    finally:
        conn.close()


def test_sqlite_diagnostics_shows_retention_and_ratings(
    client: FlaskClient,
) -> None:
    html = client.get("/diagnostics").get_data(as_text=True)
    assert "Retention runs" in html
    assert "Hook errors" in html
    assert "Recent ratings" in html
    assert "Rating diagnostics" in html
    assert client.get("/diagnostics/panel/retention-runs").status_code == 200


def test_agentcore_diagnostics_hides_retention_keeps_operational(
    agentcore_client: FlaskClient,
) -> None:
    html = agentcore_client.get("/diagnostics").get_data(as_text=True)
    assert "Retention runs" not in html
    # Operational surfaces stay visible in agentcore mode.
    assert "Hook errors" in html
    assert "Recent ratings" in html
    assert "Rating diagnostics" in html
    assert (
        agentcore_client.get("/diagnostics/panel/retention-runs").status_code
        == 404
    )
    assert agentcore_client.get("/diagnostics/panel/hook-errors").status_code == 200


def test_sqlite_reflection_drawer_shows_confirm_and_edit(
    client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    html = client.get(f"/reflections/{rid}/drawer").get_data(as_text=True)
    assert "action-confirm" in html
    assert "action-edit" in html


def test_agentcore_reflection_drawer_hides_confirm_and_edit(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    # Row-only detail with an agentcore-active status; Confirm/Edit gated off.
    fake = agentcore_client.application.extensions["backend"]

    def _row_only(*, reflection_id: str):
        return {
            "id": reflection_id, "project": "testproj", "title": "t",
            "tech": None, "phase": "implementation", "polarity": "do",
            "confidence": 0.8, "status": "active", "scope": "project",
            "evidence_count": 0, "updated_at": "x",
            "use_cases": "u", "hints": '["h"]',
            "useful_count": 0, "last_useful_at": None,
            "times_overlooked": 0, "last_overlooked_at": None,
            "times_misled": 0, "last_misled_at": None,
        }

    from unittest.mock import patch

    with patch.object(fake, "reflection_get", side_effect=_row_only):
        html = agentcore_client.get(
            f"/reflections/{rid}/drawer"
        ).get_data(as_text=True)
    assert "action-confirm" not in html
    assert "action-edit" not in html


def test_agentcore_confirm_and_edit_routes_404(
    agentcore_client: FlaskClient, tmp_db,
) -> None:
    rid = _seed_reflection(str(tmp_db))
    assert (
        agentcore_client.post(f"/reflections/{rid}/confirm").status_code == 404
    )
    assert agentcore_client.get(f"/reflections/{rid}/edit").status_code == 404
    assert (
        agentcore_client.post(
            f"/reflections/{rid}/edit",
            data={"use_cases": "u", "hints": "h"},
        ).status_code
        == 404
    )
```

- [ ] **Step 2 — Run (FAIL).** `./.venv/Scripts/python.exe -m pytest tests/ui/test_diagnostics_reflection_gating.py -v` — FAIL.

- [ ] **Step 3 — Implement.** In `diagnostics.html` wrap the retention panel:

```jinja
{% if caps.supports_retention_runs %}
  <h2>Retention runs</h2>
  <div id="retention-runs-panel"
       hx-get="{{ url_for('retention_runs_panel') }}"
       hx-trigger="load, every 60s"
       hx-swap="innerHTML">
  </div>
{% endif %}
```

In `reflection_drawer.html`, wrap the Confirm button (the inner `{% if detail.reflection.status == 'pending_review' %}` block) in an outer `{% if caps.supports_reflection_review %}`, and wrap the Edit button in `{% if caps.supports_reflection_text_edit %}`:

```jinja
{% if caps.supports_reflection_review and detail.reflection.status == 'pending_review' %}
  <button type="button" class="action-confirm" ...>Confirm</button>
{% endif %}
...
{% if caps.supports_reflection_text_edit %}
  <button type="button" class="action-edit" ...>Edit</button>
{% endif %}
```

In `ui/app.py`, add guards as the first statement of the route bodies:
- `retention_runs_panel`: `if not app.extensions["backend"].supports_retention_runs: abort(404)`
- `reflection_confirm`: `if not app.extensions["backend"].supports_reflection_review: abort(404)`
- `reflection_edit_form` and `reflection_edit_save`: `if not app.extensions["backend"].supports_reflection_text_edit: abort(404)`

(Text-edit-on-agentcore open decision is resolved as DISABLE, driven entirely by the flag — design §Open decisions.)

- [ ] **Step 4 — Run (PASS).** `./.venv/Scripts/python.exe -m pytest tests/ui -q` — pass; `tests/ui/test_diagnostics.py` (hook-errors + ratings) unchanged.

- [ ] **Step 5 — Commit.** `feat(ui): gate retention-runs panel, confirm, inline text-edit by capability`.

**Sqlite preservation:** flags `True` -> retention panel, Confirm, and Edit all render and their routes reach unchanged bodies; hook-errors + rating counters were never gated; existing `tests/ui/test_diagnostics.py` + reflection edit/confirm tests pass unedited.

---

## Task G4: `distinct_projects` backend method + dropdown replacement

**Files:**
- Modify: `better_memory/storage/protocol.py` (add `distinct_projects` to the Protocol)
- Modify: `better_memory/storage/sqlite.py` (implement `distinct_projects` = `SELECT DISTINCT project`)
- Modify: `better_memory/storage/agentcore.py` (add `list_actors` wrapper + implement `distinct_projects` = `ListActors` UNION ledger namespace-parse, best-effort degrade)
- Modify: `better_memory/ui/app.py` (`/reflections` route builds the dropdown from `backend.distinct_projects()`)
- Test: `tests/storage/test_sqlite_backend.py`, `tests/storage/test_agentcore_unit.py`, `tests/ui/test_reflections_dropdown.py` (new)

**Interfaces (exact canonical signatures):**

```python
# StorageBackend (protocol.py) + both backends
def distinct_projects(self) -> list[str]: ...

# AgentCoreBackend only (new best-effort wrapper)
def list_actors(self) -> list[str]: ...
```

- Produces:
  - `SqliteBackend.distinct_projects()` runs `SELECT DISTINCT project FROM reflections WHERE project IS NOT NULL AND project != '' ORDER BY project COLLATE NOCASE` — the identical result set to today's `queries.reflection_distinct_projects`.
  - `AgentCoreBackend.list_actors()` calls `self._data.list_actors(memoryId=self._cfg.episodic.memory_id)`, parses `actorSummaries[].actorId`, best-effort (any `_ClientError`/`Exception` -> `[]`).
  - `AgentCoreBackend.distinct_projects()` returns the sorted-casefold union of `list_actors()` and the project set parsed from the local `agentcore_migration.namespace` column (`projects/{p}/... -> p`; `general/... -> general`). Best-effort: `ListActors` failure degrades to ledger-only; both empty/failing -> `[]`.
  - `/reflections` route keeps `sorted({project_name(), *backend.distinct_projects()}, key=lambda s: s.casefold())`; only the data source changes (`queries.reflection_distinct_projects` -> `backend.distinct_projects`). The Observations dropdown (`observation_distinct_projects`) is untouched.

**Steps:**

- [ ] **Step 1 — Failing tests.**

`tests/storage/test_sqlite_backend.py` (add):

```python
def test_distinct_projects_matches_reflection_query(sqlite_backend, seeded_conn) -> None:
    """Identity pin: SqliteBackend.distinct_projects == reflection_distinct_projects."""
    from better_memory.ui import queries

    expected = queries.reflection_distinct_projects(seeded_conn)
    assert sqlite_backend.distinct_projects() == expected
    # Must be non-empty for the identity pin to be meaningful.
    assert expected
```

> Match `sqlite_backend` / `seeded_conn` to the file's existing fixtures; seed >=2 distinct projects if none exist so the pin is meaningful.

`tests/storage/test_agentcore_unit.py` (add — these trigger the named guards for [[guard-needs-triggering-test]]):

```python
def _ac_backend_with_ledger(ac_config, mock_data_client, mock_control_client, conn):
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="s",
        project="testproj",
        local_conn=conn,
    )


def test_list_actors_parses_actor_summaries(
    ac_config, mock_data_client, mock_control_client,
) -> None:
    mock_data_client.list_actors.return_value = {
        "actorSummaries": [{"actorId": "alpha"}, {"actorId": "beta"}],
    }
    be = AgentCoreBackend(
        config=ac_config, data_client=mock_data_client,
        control_client=mock_control_client, session_id="s", project="p",
    )
    assert sorted(be.list_actors()) == ["alpha", "beta"]
    mock_data_client.list_actors.assert_called_once_with(
        memoryId=ac_config.episodic.memory_id
    )


def test_distinct_projects_unions_actors_and_ledger_namespaces(
    ac_config, mock_data_client, mock_control_client,
) -> None:
    conn = connect(":memory:")
    apply_migrations(conn)
    from better_memory.storage.agentcore_migrate import ensure_ledger

    ensure_ledger(conn)
    # namespace-parse guard: projects/{p}/... -> p ; general/... -> general
    conn.executemany(
        "INSERT INTO agentcore_migration "
        "(source_kind, source_id, namespace, content_hash, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("reflection", "r1", "projects/gamma/reflections/", "h1", "active"),
            ("semantic", "s1", "general/semantic/", "h2", "active"),
        ],
    )
    conn.commit()
    mock_data_client.list_actors.return_value = {
        "actorSummaries": [{"actorId": "alpha"}, {"actorId": "beta"}],
    }
    be = _ac_backend_with_ledger(
        ac_config, mock_data_client, mock_control_client, conn,
    )
    assert be.distinct_projects() == ["alpha", "beta", "gamma", "general"]


def test_distinct_projects_degrades_to_ledger_when_listactors_raises(
    ac_config, mock_data_client, mock_control_client,
) -> None:
    conn = connect(":memory:")
    apply_migrations(conn)
    from better_memory.storage.agentcore_migrate import ensure_ledger

    ensure_ledger(conn)
    conn.execute(
        "INSERT INTO agentcore_migration "
        "(source_kind, source_id, namespace, content_hash, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("reflection", "r1", "projects/gamma/reflections/", "h1", "active"),
    )
    conn.commit()
    # AWS-degrade guard: ListActors raises -> ledger-only result.
    mock_data_client.list_actors.side_effect = RuntimeError("throttled")
    be = _ac_backend_with_ledger(
        ac_config, mock_data_client, mock_control_client, conn,
    )
    assert be.distinct_projects() == ["gamma"]


def test_distinct_projects_empty_when_both_fail(
    ac_config, mock_data_client, mock_control_client,
) -> None:
    mock_data_client.list_actors.side_effect = RuntimeError("throttled")
    be = AgentCoreBackend(
        config=ac_config, data_client=mock_data_client,
        control_client=mock_control_client, session_id="s", project="p",
        local_conn=None,
    )
    assert be.distinct_projects() == []
```

`tests/ui/test_reflections_dropdown.py` (new):

```python
"""The Reflections project dropdown sources from backend.distinct_projects()."""

from __future__ import annotations

from html.parser import HTMLParser
from unittest.mock import patch

from flask.testing import FlaskClient


class _ProjectOptions(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_project_select = False
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        d = dict(attrs)
        if tag == "select" and d.get("name") == "project":
            self._in_project_select = True
        elif tag == "option" and self._in_project_select:
            self.values.append(d.get("value") or "")

    def handle_endtag(self, tag) -> None:
        if tag == "select":
            self._in_project_select = False


def _project_options(html: str) -> list[str]:
    p = _ProjectOptions()
    p.feed(html)
    return p.values


def test_dropdown_sources_from_backend_distinct_projects(
    agentcore_client: FlaskClient,
) -> None:
    fake = agentcore_client.application.extensions["backend"]
    with patch.object(
        fake, "distinct_projects", return_value=["zeta", "alpha"],
    ):
        html = agentcore_client.get("/reflections").get_data(as_text=True)
    opts = _project_options(html)
    # project_name() is unioned in, and the set is sorted casefold.
    assert "alpha" in opts
    assert "zeta" in opts
    assert opts == sorted(opts, key=lambda s: s.casefold())
```

- [ ] **Step 2 — Run (FAIL).** `./.venv/Scripts/python.exe -m pytest tests/storage/test_sqlite_backend.py tests/storage/test_agentcore_unit.py tests/ui/test_reflections_dropdown.py -v` — FAIL (`distinct_projects` / `list_actors` missing).

- [ ] **Step 3 — Implement.**

`protocol.py` — add to the `StorageBackend` Protocol:

```python
def distinct_projects(self) -> list[str]:
    """Distinct project names for the Reflections project dropdown.

    sqlite: SELECT DISTINCT project FROM reflections. agentcore:
    ListActors UNION the migration-ledger namespaces (best-effort)."""
    ...
```

`sqlite.py`:

```python
def distinct_projects(self) -> list[str]:
    return [
        r["project"]
        for r in self._conn.execute(
            "SELECT DISTINCT project FROM reflections "
            "WHERE project IS NOT NULL AND project != '' "
            "ORDER BY project COLLATE NOCASE"
        ).fetchall()
    ]
```

(Use the file's actual connection attribute name — match `sqlite.py`'s existing methods.)

`agentcore.py`:

```python
def list_actors(self) -> list[str]:
    """Actor ids for the episodic memory, best-effort (empty on any error)."""
    try:
        resp = self._data.list_actors(
            memoryId=self._cfg.episodic.memory_id
        )
    except Exception:  # noqa: BLE001 - best-effort; empty signals "lookup failed"
        return []
    return [
        s.get("actorId", "")
        for s in resp.get("actorSummaries", [])
        if s.get("actorId")
    ]

def _ledger_projects(self) -> set[str]:
    """Projects parsed from the local agentcore_migration.namespace column.

    projects/{p}/... -> {p}; general/... -> 'general'. Parent namespaces
    do NOT roll up. Best-effort: no local conn / query error -> empty set."""
    if self._local_conn is None:
        return set()
    try:
        rows = self._local_conn.execute(
            "SELECT DISTINCT namespace FROM agentcore_migration"
        ).fetchall()
    except Exception:  # noqa: BLE001 - ledger missing/unreadable -> empty
        return set()
    out: set[str] = set()
    for row in rows:
        ns = (row[0] or "").lstrip("/")
        if ns.startswith("projects/"):
            rest = ns[len("projects/"):]
            head = rest.split("/", 1)[0]
            if head:
                out.add(head)
        elif ns.startswith("general/"):
            out.add("general")
    return out

def distinct_projects(self) -> list[str]:
    projects = set(self.list_actors()) | self._ledger_projects()
    return sorted(projects, key=lambda s: s.casefold())
```

`ui/app.py` — in the `reflections` route, replace the dropdown source:

```python
backend = app.extensions["backend"]
db_projects = backend.distinct_projects()
projects = sorted({current, *db_projects}, key=lambda s: s.casefold())
```

- [ ] **Step 4 — Run (PASS).** `./.venv/Scripts/python.exe -m pytest tests/storage tests/ui -q` — pass.

- [ ] **Step 5 — Commit.** `feat(ui): distinct_projects via ListActors UNION migration ledger`.

**Sqlite preservation:** `SqliteBackend.distinct_projects()` returns the identical DISTINCT set to `queries.reflection_distinct_projects`; the `/reflections` union+sort is unchanged, so the sqlite dropdown is byte-identical. The identity test pins this.

---

## Task G5: Docs, pyright, full suite, live-smoke checklist

**Files:** `website/agentcore-setup.md`, `website/architecture.md`, gated-route/protocol docstrings.

- [ ] **Step 1 — Docs.** Update `website/agentcore-setup.md`: add/extend a **capability table** naming the UI surfaces hidden in agentcore mode — Episodes tab + routes, Observations tab + routes, reflection/observation provenance sections, retention-runs Diagnostics panel, reflection Confirm action, inline reflection text-edit — and note the Reflections project dropdown is now sourced from `ListActors` UNION the `agentcore_migration` ledger. Update `website/architecture.md` UI/storage prose: the UI gates surfaces by `StorageBackend` capability flags via a `caps` context processor; CONTENT reads route through `app.extensions["backend"]` while OPERATIONAL state (hook errors, session_memory_exposure / rating counters, retention_runs, audit_log) stays on the local `db_connection` in BOTH modes. Add one-line docstrings to each new route guard and to `distinct_projects` / `list_actors`.
  - **[[keep-docs-in-sync]]**: State explicitly in the commit body that `README.md`, `website/mcp-tools.md`, and `website/configuration.md` are **unaffected** (no new MCP tool, no env key, no config surface — this phase only gates existing UI surfaces and reroutes one dropdown). Grep-sweep synonyms to be sure: `Observations tab`, `retention runs`, `Confirm`, `SELECT DISTINCT`, `supports_episodes`.
- [ ] **Step 2 — pyright.** `./.venv/Scripts/python.exe -m pyright` -> 0 errors. Note the `_FakeAgentCoreBackend` in tests is duck-typed; assign it via `app.extensions["backend"] = ...` (the extensions dict is `Any`-valued) so no protocol-conformance error arises.
- [ ] **Step 3 — Full suite.** `./.venv/Scripts/python.exe -m pytest tests -q` -> green; fix stragglers minimally; re-run.
- [ ] **Step 4 — Commit.** `docs: agentcore UI capability-gating tables + prose`.

**Sqlite preservation:** docs-only + verification; no runtime change. The full `tests/ui/*` suite green is the standing sqlite pin.

---

## Task G6: PR, babysit, merge

- [ ] **Step 1 — PR.** Push; `gh pr create`. Body includes: the design-spec link; the four moves (nav/route gating, provenance gating + row-only fetch, retention/confirm/text-edit gating, dropdown via `distinct_projects`); the sqlite-unchanged proof (existing `tests/ui/*` suite green, unedited); honest boundaries (observation reads + Diagnostics content aggregates deferred to later phases); and a **manual live-smoke checklist**:
  - `BETTER_MEMORY_STORAGE_BACKEND=agentcore` session -> nav hides Episodes + Observations (3 rail links).
  - Reflections + Semantic tabs show real AgentCore records.
  - Reflections project dropdown lists real projects (`ListActors` + ledger).
  - Reflection drawer omits provenance ("Source observations"), Confirm, and inline Edit.
  - Diagnostics omits the Retention-runs panel but still shows Hook errors + Recent ratings + Rating diagnostics.
  - Footer: `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] **Step 2 — Babysit.** Watch to green + zero open review threads -> squash-merge, checkout main, pull.
- [ ] **Step 3 — Memory sweep (CLAUDE.md mandatory trigger).** Before finishing, record any review-driven fix or non-obvious agentcore gotcha (namespace-parse edge cases, `ListActors` degradation, the row-only drawer-render branch) as a `failure`/`neutral` observation with `component=agentcore`/`ui`, `theme=bug`/`gotcha`.
- [ ] **Step 4 — Deploy note.** No env changes; restart picks up UI code. Live smoke is manual (needs AWS creds) — present the checklist, do not run unprompted.

---

## Self-review notes

- Scope maps: nav/route gating -> G1; provenance sections + row-only fetch -> G2; retention/confirm/text-edit gating -> G3; dropdown -> G4; docs/pyright/suite -> G5; PR -> G6.
- Every task carries a sqlite-preservation clause; flags are all `True` on sqlite so no gate fires, and every rerouted read (`_reflection_drawer_detail`, `distinct_projects`) resolves to the exact existing query. The `tests/ui/*` suite is the standing pin.
- Canonical flag names used throughout: `supports_observations`, `supports_provenance`, `supports_retention_runs`, `supports_reflection_review`, `supports_reflection_text_edit` (+ pre-existing `supports_episodes`). No superseded `*_for_ui` / `supports_reflection_confirm` / `_retention` / `_mutation` names appear.
- [[guard-needs-triggering-test]] satisfied in G4: namespace-parse (`projects/gamma/... -> gamma`, `general/... -> general`) and `ListActors`-raises-degrade each have a dedicated seeding test.
- [[server-boot-real-call]] satisfied in G1: `test_agentcore_hides_episodes_and_observations_links` drives a real `GET /reflections` through the stubbed backend.
