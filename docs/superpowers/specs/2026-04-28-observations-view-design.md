# Observations view — design

**Status:** draft · **Date:** 2026-04-28

---

## 1. Scope, goals, non-goals

### Scope

Add a fourth nav tab to the better-memory management UI, **Observations**, that lists raw observations for the current project with filters and a single button that triggers project-wide synthesis (LLM consolidation into reflections). The view mirrors the existing Reflections tab's shape: filter form at the top, panel of compact rows below, drawer for detail.

### Goals

1. The user can see the raw observation backlog for the current project — what `memory_observe` has written but synthesis hasn't yet consumed.
2. The user can trigger LLM synthesis manually from this view, so observations appear in the Reflections tab without needing to wait for whatever automatic trigger normally drives synthesis.
3. Each observation has a drawer surface for full content, audit timeline, and any reflections that already cite it.

### Non-goals

- Per-row checkbox or multi-select for partial synthesis. Synthesis is project-wide (matches the existing `ReflectionSynthesisService.synthesize` contract).
- Inline editing of observations. Observations are episodic facts from a moment in time — explicitly excluded by `2026-04-18-better-memory-ui-design.md` §1.
- Background synthesis or progress streaming. The Flask app runs `threaded=False`; the synthesis trigger is a blocking POST. UI freeze for ~10–60 s during the call is acceptable for a single-user local tool.
- Theme and date-range filters. Theme is thin in practice (~5 well-known values); date range is implied by the `status` filter (active observations are newest by definition).
- "Promote observation to knowledge" action. Tracked separately in `project_promote_to_knowledge_deferred.md`.

---

## 2. Background — what already exists

- `better_memory/services/observation.py::ObservationService` is the write path; observations land in the `observations` table with columns including `id`, `content`, `component`, `theme`, `outcome`, `tech`, `trigger_type`, `episode_id`, `project`, `status`, `reinforcement_score`, `created_at`.
- `better_memory/services/reflection.py::ReflectionSynthesisService.synthesize(*, goal, tech, project)` is the LLM consolidation entry point. It returns bucketed reflections (`do`/`dont`/`neutral`) and writes new/augment/merge actions to the database. Short-circuits when same `(project, tech, goal)` ran inside the last 10 min with no new observations.
- `better_memory/ui/app.py` exposes `/episodes` and `/reflections` tabs, both built on the same pattern: tab page → HTMX panel swap → click row to drawer. `app.extensions["episode_service"]` and `app.extensions["reflection_service"]` are wired in `create_app`. Origin-check middleware guards non-GET requests.
- `better_memory/ui/queries.py` is the read-only SELECT helper module. Adds rows here, not service-layer methods, for view-specific shapes.
- The base template `templates/base.html` renders three nav tabs today (`Episodes`, `Reflections` — plus the implicit `/` redirect). A fourth tab fits the existing layout.

---

## 3. Architecture

### File structure

```
better_memory/ui/
  templates/
    observations.html                        # tab page (filter form + empty panel target)
    fragments/
      panel_observations.html                # row list, grouped by ISO date prefix
      observation_row.html                   # one compact row
      observation_drawer.html                # detail (full content, metadata, audit, linked reflections, episode link)
      observation_filter_form.html           # status / outcome / component filters
      observations_synth_banner.html         # post-synthesis banner with bucket counts
better_memory/ui/queries.py                  # add ObservationRow, ObservationDetail + two helpers
better_memory/ui/app.py                      # add four routes; wire ReflectionSynthesisService into app.extensions
better_memory/ui/templates/base.html         # add fourth nav tab "Observations"
```

### Routes

| Path | Method | Purpose |
|---|---|---|
| `/observations` | GET | Tab page; renders filter form + empty panel target. |
| `/observations/panel` | GET | Row list. HTMX swap target. Reads filter args from query string. |
| `/observations/<id>/drawer` | GET | Detail drawer for one observation. 404 on unknown id. |
| `/observations/synthesize` | POST | Trigger project-wide synthesis. Blocking. Returns rendered banner with `HX-Trigger: observations-synthesized` so the panel auto-refreshes. |

`/observations/<id>` (no suffix) is not a route — drawers are HTMX fragments only, never standalone pages.

### Service wiring

`create_app` instantiates one `ReflectionSynthesisService` and stores it in `app.extensions["reflection_synthesis_service"]`. Construction needs the existing DB connection plus an `OllamaChat` client; mirror the pattern that `mcp/server.py` uses (config-driven, `_DEFAULT_CONSOLIDATE_MODEL`).

This is the only behavioural change to `create_app`. The existing `episode_service` and `reflection_service` wiring stays as-is.

---

## 4. Queries

Two new public functions in `better_memory/ui/queries.py`:

### `observation_list_for_ui`

```python
@dataclass(frozen=True)
class ObservationRow:
    id: str
    content: str               # full text — template truncates display
    component: str | None
    theme: str | None
    outcome: str               # "success" / "failure" / "neutral"
    status: str                # "active" / "archived" / "consumed_without_reflection"
    created_at: str
    episode_id: str | None


def observation_list_for_ui(
    conn: sqlite3.Connection,
    *,
    project: str,
    status: str | None = None,
    outcome: str | None = None,
    component: str | None = None,
    limit: int = 100,
) -> list[ObservationRow]:
    """Project-scoped observation list with optional filters. Newest first."""
```

SQL is a single SELECT with `WHERE project = ?` plus optional `AND status = ?` / `AND outcome = ?` / `AND component = ?` clauses, `ORDER BY created_at DESC, rowid DESC LIMIT ?`. None of the filters default to a value — the panel shows all observations on first load.

### `observation_detail`

```python
@dataclass(frozen=True)
class LinkedReflectionRow:
    id: str
    title: str
    polarity: str
    confidence: float
    status: str

@dataclass(frozen=True)
class ObservationAuditEntry:
    at: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None

@dataclass(frozen=True)
class ObservationDetail:
    observation: Observation     # full row from services/observation.py
    audit: list[ObservationAuditEntry]
    reflections: list[LinkedReflectionRow]


def observation_detail(
    conn: sqlite3.Connection, *, observation_id: str
) -> ObservationDetail | None:
    """Return one observation with audit + linked reflections, or None if not found."""
```

`Observation` is the existing dataclass in `services/observation.py`. `LinkedReflectionRow` mirrors the existing `EpisodeReflectionRow` shape. Audit reads from `audit_log` filtered to `entity_type = 'observation' AND entity_id = ?`. Linked reflections JOIN `reflection_sources` on `observation_id`.

Default sort: `created_at DESC, rowid DESC` for both list and audit.

---

## 5. Synthesis trigger flow

```python
@app.post("/observations/synthesize")
def observations_synthesize() -> tuple[str, int, dict[str, str]]:
    project = _project_name()
    svc = app.extensions["reflection_synthesis_service"]
    try:
        buckets = asyncio.run(svc.synthesize(
            goal="manual synthesis",
            tech=None,
            project=project,
        ))
    except Exception as exc:  # noqa: BLE001 — surface to user
        return (
            f'<div class="card card-error"><p>{escape(str(exc))}</p></div>',
            500,
            {},
        )
    counts = {k: len(v) for k, v in buckets.items()}
    rendered = render_template(
        "fragments/observations_synth_banner.html", counts=counts,
    )
    return rendered, 200, {"HX-Trigger": "observations-synthesized"}
```

### Decisions

- **Goal: `"manual synthesis"` constant.** Not user-supplied. Triggers the existing 10-minute same-goal short-circuit, so re-clicking the button within that window cheaply returns the current buckets without calling the LLM. Acceptable: nothing actionable happens in that window anyway.
- **Tech: `None`.** UI-triggered synthesis is project-wide, not tech-scoped. The synthesis service handles `tech=None` correctly (mirrors `mcp/server.py`'s call shape).
- **Blocking POST.** `asyncio.run` bridges the async `synthesize` to the sync Flask route. `threaded=False` means the whole UI freezes for the call duration; this is the documented tradeoff (Q3 in the brainstorm).
- **Error surface.** Any exception raised by `synthesize` (LLM unreachable, parse error, DB lock) is caught and rendered as a `card-error` fragment with HTTP 500, mirroring how `reflection_confirm` handles `ValueError`.
- **HX-Trigger.** `observations-synthesized` is the event the panel listens for to reload itself. Same pattern as `episode-closed` and `reflection-changed`.

### Banner template

`fragments/observations_synth_banner.html` renders the bucket counts as a single line: `Synthesised: 3 do · 1 dont · 0 neutral` with a link to `/reflections`. Plain HTML, no JS.

---

## 6. UI layout (read-only summary, mirrors Reflections)

- Header: existing nav adds **Observations** between Episodes and Reflections, so the order becomes Episodes · Observations · Reflections.
- Filter form (HTMX `hx-get="/observations/panel" hx-target="#observation-panel"` `hx-trigger="change from:select"`): three selects (status, outcome, component) plus a "Run synthesis" button styled as a primary action.
- Panel: rows grouped by ISO date prefix (matches the Episodes panel grouping pattern). Each row shows: outcome chip · component (or `—`) · created_at hh:mm · content snippet (one line, truncated). Click → drawer.
- Drawer: full content; metadata grid (`id`, `theme`, `tech`, `trigger_type`, `episode_id`, `reinforcement_score`); linked reflections list with `polarity` and `confidence`; audit timeline newest-first.
- "Run synthesis" button shows a spinner via HTMX's `hx-indicator` while the request is in flight. When the response lands, the banner replaces the button area; the panel auto-refreshes via the `HX-Trigger` event.

---

## 7. Error handling

| Failure mode | Behaviour |
|---|---|
| `/observations/<id>/drawer` for unknown id | `abort(404)` (matches `episodes_drawer`) |
| `/observations/panel` with invalid filter value (e.g. `outcome=garbage`) | Treat as no-op filter; show all matching the other filters. Keep parity with how `reflections_panel` handles its `min_confidence` parse — soft fallback. |
| `/observations/synthesize` raises | Render `card-error` fragment with the exception message; HTTP 500. The user can retry. |
| `asyncio.run` already in an event loop (e.g. someone wraps Flask in `asyncio`) | Should not happen — werkzeug's `make_server` is sync. If it does, the standard `RuntimeError("asyncio.run() cannot be called from a running event loop")` propagates and the user sees the generic 500 card. |

---

## 8. Testing

`tests/ui/test_observations.py` — Flask test-client level, mirrors `test_reflections.py`:

| Test | Assertion |
|---|---|
| `test_observations_panel_returns_rows` | seed 3 observations; GET `/observations/panel` returns three row fragments newest-first |
| `test_observations_panel_filters_by_outcome` | seed mixed outcomes; `?outcome=failure` returns only failures |
| `test_observations_panel_filters_by_status` | seed mixed statuses; `?status=active` excludes archived/consumed |
| `test_observations_panel_filters_by_component` | seed mixed components; `?component=ui_launcher` narrows correctly |
| `test_observation_drawer_renders_full_content` | drawer shows full content, linked reflections, audit timeline |
| `test_observation_drawer_404_for_missing_id` | unknown id → 404 |
| `test_observations_synthesize_triggers_service_and_returns_banner` | patch `synthesize` to return fixed buckets; assert `HX-Trigger: observations-synthesized` header + banner counts in body |
| `test_observations_synthesize_500_on_service_error` | patch `synthesize` to raise; assert `card-error` body + HTTP 500 |
| `test_observations_panel_filter_form_initial_state` | tab page renders the three select boxes with no preset values |

`tests/ui/test_browser_observations.py` — Playwright integration mirroring `test_browser_reflections.py`: open the tab; change a filter and assert the panel updates; click a row and assert the drawer opens; click "Run synthesis" with a stubbed LLM and assert the banner appears.

`tests/ui/test_queries_observations.py` — pure-query tests (no Flask) for `observation_list_for_ui` and `observation_detail`, mirroring `test_queries_reflections.py`.

---

## 9. Out of scope (will not build)

- Per-row checkbox / multi-select synthesis.
- Inline editing of observations.
- Background synthesis / progress streaming.
- Theme and date-range filters.
- "Promote observation to knowledge" action.
- Bulk archive / bulk delete observations.
- Pagination beyond the 100-row cap (matches reflections panel cap).

---

## 10. Open questions

None at the time of writing. All design questions resolved via the brainstorm.
