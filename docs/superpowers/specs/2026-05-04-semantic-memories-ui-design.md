# Semantic memories UI tab — design

**Status:** Approved 2026-05-04
**Branch target:** new feature branch off `main` (e.g. `semantic-memories-ui`) after PR #34 is merged. PR will land as **#37** (issues #35 nav-redesign and #36 filter/search were filed first as deferred work).
**Predecessor:** PR #34 (semantic memories MCP feature) — adds `semantic_memories` table, `SemanticMemoryService` (`create`/`update_text`/`delete`/`list_for_project`), and 4 MCP tools. This PR puts that data on screen in the Archive UI.

## Goal

Add a **Semantic memories** tab to The Archive UI so users can read, create, edit, delete, and toggle the scope of their semantic memories visually — and promote useful observations into semantic memories directly from the observation drawer.

## Why now

PR #34 made semantic memories first-class data in better-memory but the only access path is the MCP tool surface (Claude has to do all the writes). Users editing or curating their own semantic memories need a UI affordance — the existing Archive UI is the natural home. The promote-observation action also closes a real gap: when a recorded observation turns out to be a durable rule, there's currently no way to elevate it without manual MCP-tool gymnastics.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Panel layout | **(A) Card list, mirrors Reflections** | Lowest implementation cost — reuse the same row-card-list pattern. Bucketing by scope (option C) was deferred; flat list is fine at v1 row counts. |
| Nav structure | Keep existing vertical nav-rail | Horizontal-tab redesign deferred to issue #35. This PR adds one more rail entry. |
| "Promote observation" action placement | **(II) Drawer only** (not row) | Promotion is a deliberate decision — two clicks (open drawer → promote) is fine. Keeps row UI clean. Row-level shortcut deferred. |
| Source observation status after promote | Reuse existing `consumed_without_reflection` (no new status value, no migration) | YAGNI. The semantic distinction (consumed-into-semantic vs consumed-without) isn't surfaced anywhere in the UI today. Can split later if filtering by promotion target becomes a need. |
| Promote atomicity | `SemanticMemoryService.create_from_observation` wraps both writes (insert + status flip) in a `SAVEPOINT promote_observation` envelope | Single failure mode — either both writes land or neither. Same envelope pattern as `synthesize_next` in PR #25. |
| Promote idempotence | `create_from_observation` raises `ValueError` if the observation isn't `status='active'` (i.e. already consumed) | Prevents accidental duplicate promotions. The drawer hides the button when status≠active so users only see it via the API path; the ValueError surfaces as a 400 to the route, which renders an error card. |
| Scope toggle | Inline button on each card (`make general` / `make project`) | Single-click change. No confirmation dialog — the change is reversible with the opposite-direction button. |
| Edit experience | Drawer with content textarea + scope select | Mirrors `reflection_drawer.html` + `reflection_edit_form.html`. |
| Search/filter | Out of scope — issue #36 | Flat list is usable at v1 row counts; filter form lands when growth surfaces the need. |
| Empty state | Plain text card: "No semantic memories yet. Use the form above to add one, or promote an observation from the Observations tab." | Matches existing `panel_reflections.html` empty-state pattern. |

## Approach

Single PR off `main`, three commits:

| # | Commit | Files |
|---|---|---|
| 1 | `feat(semantic): set_scope + create_from_observation service methods` | `better_memory/services/semantic.py`, `tests/services/test_semantic.py` |
| 2 | `feat(ui): /semantic panel with create/edit/scope-toggle/delete` | `better_memory/ui/app.py`, 6 new templates under `templates/`, `templates/base.html` (nav entry), `tests/ui/test_semantic.py` |
| 3 | `feat(ui): promote observation to semantic memory from the drawer` | `better_memory/ui/app.py` (one new route), `templates/fragments/observation_drawer.html`, `tests/ui/test_observations.py` |

Commits are dependency-ordered: service first, panel UI second, observation drawer integration third. Pyright + pytest green at every commit boundary.

## Commit 1 — Service additions

`better_memory/services/semantic.py`:

```python
def set_scope(self, *, id: str, scope: str) -> None:
    """Toggle a semantic memory's scope between 'project' and 'general'.

    Idempotent: setting scope to its current value is a no-op (UPDATE
    affects 0 rows but doesn't raise).
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"scope must be 'project' or 'general', got {scope!r}"
        )
    now = self._clock().isoformat()
    cur = self._conn.execute(
        "UPDATE semantic_memories SET scope = ?, updated_at = ? "
        "WHERE id = ?",
        (scope, now, id),
    )
    if cur.rowcount == 0:
        # No row updated — same rollback pattern as update_text per
        # PR #34's BugBot finding.
        self._conn.rollback()
        raise ValueError(f"semantic memory not found: {id}")
    self._conn.commit()

def create_from_observation(
    self, *, observation_id: str, scope: str = "project"
) -> str:
    """Promote an active observation into a new semantic memory.

    Atomically:
    1. Read the observation's content + project; raise if not found
       or not status='active'.
    2. Insert a new semantic_memories row with the same content,
       requested scope, and current timestamp.
    3. Mark the source observation status='consumed_without_reflection'
       and bump status_changed_at.

    All three writes wrapped in SAVEPOINT promote_observation —
    either all land or none.

    Raises ValueError if observation is missing or already consumed.
    Returns the new semantic memory id.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"scope must be 'project' or 'general', got {scope!r}"
        )
    row = self._conn.execute(
        "SELECT content, project, status FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"observation not found: {observation_id}")
    if row["status"] != "active":
        raise ValueError(
            f"observation {observation_id} is not active "
            f"(status={row['status']!r}); cannot promote"
        )

    memory_id = uuid4().hex
    now = self._clock().isoformat()
    self._conn.execute("SAVEPOINT promote_observation")
    try:
        self._conn.execute(
            """
            INSERT INTO semantic_memories
                (id, content, project, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, row["content"], row["project"], scope, now, now),
        )
        self._conn.execute(
            "UPDATE observations "
            "SET status = 'consumed_without_reflection', status_changed_at = ? "
            "WHERE id = ?",
            (now, observation_id),
        )
    except BaseException:
        self._conn.execute("ROLLBACK TO SAVEPOINT promote_observation")
        self._conn.execute("RELEASE SAVEPOINT promote_observation")
        raise
    else:
        self._conn.execute("RELEASE SAVEPOINT promote_observation")
    self._conn.commit()
    return memory_id
```

**Tests** (`tests/services/test_semantic.py` extension):

| Test | Behavior |
|---|---|
| `test_set_scope_changes_value` | project→general, general→project, both verified by re-read |
| `test_set_scope_is_idempotent_on_same_value` | scope='project' on a project row → no error, updated_at still bumps (sanity)... actually no: same-value update may or may not bump updated_at; pin behavior — test that the row is unchanged (updated_at bumped is acceptable) |
| `test_set_scope_rejects_invalid` | scope='invalid' → ValueError |
| `test_set_scope_raises_on_missing_id` + rolls back the implicit transaction (mirror the update_text regression test) |
| `test_create_from_observation_happy_path` | Insert observation; promote it; verify (a) new memory row with same content, (b) observation.status='consumed_without_reflection', (c) status_changed_at bumped |
| `test_create_from_observation_default_scope_is_project` | omit scope → project |
| `test_create_from_observation_with_general_scope` | scope='general' → general row |
| `test_create_from_observation_raises_on_missing_observation` | unknown id → ValueError |
| `test_create_from_observation_raises_on_already_consumed` | observation.status='consumed_into_reflection' → ValueError; observation row unchanged |
| `test_create_from_observation_atomic_on_failure` | force the second UPDATE to fail (e.g. monkeypatch); both writes rolled back — no orphaned semantic memory row |

## Commit 2 — `/semantic` panel UI

**Routes** added to `better_memory/ui/app.py`:

```python
@app.get("/semantic")
def semantic_page():
    return render_template("semantic.html", active_tab="semantic")

@app.get("/semantic/panel")
def semantic_panel():
    project = request.args.get("project") or project_name()
    conn = app.extensions["db_connection"]
    svc = SemanticMemoryService(conn)
    rows = svc.list_for_project(project=project)
    return render_template("fragments/panel_semantic.html", rows=rows, project=project)

@app.post("/semantic")
def semantic_create():
    project = project_name()
    conn = app.extensions["db_connection"]
    svc = SemanticMemoryService(conn)
    content = request.form.get("content", "").strip()
    scope = request.form.get("scope") or "project"
    try:
        svc.create(content=content, project=project, scope=scope)
    except ValueError as exc:
        return (f'<div class="card card-error">{escape(str(exc))}</div>', 400, {})
    return ("", 200, {"HX-Trigger": "semantic-changed"})

@app.get("/semantic/<id>/drawer")
def semantic_drawer(id: str):
    # Render edit form for one memory.
    ...

@app.post("/semantic/<id>/update")
def semantic_update(id: str):
    # Apply content edit; HX-Trigger semantic-changed.
    ...

@app.post("/semantic/<id>/scope")
def semantic_scope(id: str):
    # Toggle scope between project↔general.
    ...

@app.post("/semantic/<id>/delete")
def semantic_delete(id: str):
    # Hard delete; HX-Trigger semantic-changed.
    ...
```

All routes use `app.extensions["db_connection"]` (existing pattern). All non-GET routes return `HX-Trigger: semantic-changed` to reload the panel via the existing htmx pattern (see `panel_reflections` for the analog).

**Templates:**

| File | Mirror of |
|---|---|
| `templates/semantic.html` | `templates/reflections.html` — top-level page, includes the panel + listens for `semantic-changed` |
| `templates/fragments/panel_semantic.html` | `panel_reflections.html` — empty-state branch + iterates rows + includes the create form at top |
| `templates/fragments/semantic_row.html` | `reflection_row.html` — single card with content, scope badge, edit/scope-toggle/delete buttons |
| `templates/fragments/semantic_drawer.html` | `reflection_drawer.html` — expanded view with edit form |
| `templates/fragments/semantic_edit_form.html` | `reflection_edit_form.html` — textarea + scope select + Save/Cancel |
| `templates/fragments/semantic_create_form.html` | New (no direct mirror) — small textarea + scope select + Add button at top of panel |

**Nav-rail entry** in `templates/base.html`: add `Semantic` between `Reflections` and `Diagnostics`. Roman numeral `IV.` (Diagnostics shifts to `V.`).

**Scope-toggle button label rule** (in `semantic_row.html`):

```jinja
{% if row.scope == 'project' %}
  <button hx-post="/semantic/{{ row.id }}/scope"
          hx-vals='{"scope": "general"}'
          hx-target="closest .panel"
          hx-swap="outerHTML">make general</button>
{% else %}
  <button hx-post="/semantic/{{ row.id }}/scope"
          hx-vals='{"scope": "project"}'
          hx-target="closest .panel"
          hx-swap="outerHTML">make project</button>
{% endif %}
```

(Exact `hx-target` to be confirmed against the panel's container during implementation; mirror the existing reflection scope-toggle pattern if there's one.)

**Tests** (`tests/ui/test_semantic.py`):

| Test class | Tests |
|---|---|
| `TestSemanticPage` | GET `/semantic` returns 200 + nav-rail-active="semantic" |
| `TestSemanticPanel` | empty-state branch + populated state with 2 memories + ordering newest-first + project-+-general merging visible |
| `TestSemanticCreate` | POST `/semantic` with content + scope → row created + HX-Trigger; empty content → 400 with card-error |
| `TestSemanticUpdate` | POST `/semantic/<id>/update` changes content + bumps updated_at; missing id → error |
| `TestSemanticScope` | toggle project→general; toggle general→project; missing id → error |
| `TestSemanticDelete` | row removed; missing id → no error (idempotent) |
| `TestSemanticDrawer` | renders edit form with current values |

## Commit 3 — Promote-from-observation

**New route** in `better_memory/ui/app.py`:

```python
@app.post("/observations/<id>/promote-to-semantic")
def observation_promote_to_semantic(id: str):
    project = project_name()
    conn = app.extensions["db_connection"]
    svc = SemanticMemoryService(conn)
    scope = request.form.get("scope") or "project"
    try:
        memory_id = svc.create_from_observation(
            observation_id=id, scope=scope,
        )
    except ValueError as exc:
        return (f'<div class="card card-error">{escape(str(exc))}</div>', 400, {})
    # On success, swap the drawer with a brief confirmation card.
    return render_template(
        "fragments/observation_promoted_card.html",
        memory_id=memory_id, scope=scope,
    ), 200, {"HX-Trigger": "observations-changed semantic-changed"}
```

**`HX-Trigger: observations-changed semantic-changed`** — both the obs panel (to re-render the row with the consumed status) and the semantic panel (if open in another tab; reasonable to reload regardless).

**Template change** to `templates/fragments/observation_drawer.html`:

```jinja
{% if observation.status == 'active' %}
  <form hx-post="/observations/{{ observation.id }}/promote-to-semantic"
        hx-target="closest .observation-drawer"
        hx-swap="outerHTML"
        class="promote-form">
    <label>Promote to semantic memory:</label>
    <select name="scope">
      <option value="project">project</option>
      <option value="general">general</option>
    </select>
    <button type="submit">Promote</button>
  </form>
{% endif %}
```

**New template** `templates/fragments/observation_promoted_card.html`:

```jinja
<div class="card card-success observation-drawer">
  <p>Promoted as <strong>{{ scope }}</strong> semantic memory.
     <a href="/semantic">View in Semantic memories →</a></p>
</div>
```

**Tests** (`tests/ui/test_observations.py` extension):

| Test | Behavior |
|---|---|
| `test_drawer_shows_promote_form_when_active` | drawer template includes the form when observation.status='active' |
| `test_drawer_hides_promote_form_when_consumed` | drawer template omits the form when observation.status≠'active' |
| `test_promote_creates_semantic_memory_and_flips_status` | POST → new semantic_memories row exists with same content + observation.status flipped to consumed_without_reflection |
| `test_promote_already_consumed_observation_returns_400` | status='consumed_into_reflection' → 400 with card-error; row counts unchanged |

## Out-of-scope / future work

- **Filter / search** on the panel — issue #36.
- **Horizontal-tab nav redesign** — issue #35.
- **Bulk multi-select promote** from the observations panel.
- **Row-level promote shortcut** (vs drawer-only).
- **Audit log** of edits.
- **Re-promotion** (consumed → active again, or semantic-memory → re-fed-as-observation).
- **Markdown rendering** in semantic memory content.
- **Distinct `consumed_into_semantic_memory` status value** — split later if filtering by promotion-target becomes a need.

## Spec self-review

- **Placeholders:** none. Every commit specifies files, code, and test names. The two `...` ellipses in the routes section (`semantic_drawer`, `semantic_update`, etc.) are intentional — those routes mirror the existing reflections-route pattern verbatim and are detailed in the plan, not the spec.
- **Internal consistency:** `consumed_without_reflection` referenced identically across decisions log, service code, and tests. `SAVEPOINT promote_observation` envelope matches PR #25's `synthesize_next` pattern. The scope-toggle button label rule (project→make-general, general→make-project) is consistent with the inline button text in the Section 1 mockup.
- **Scope:** three commits, ~120 LOC service additions + ~250 LOC route+template+tests + ~80 LOC drawer integration. Tractable for a single subagent-driven cycle.
- **Ambiguity:** the only knob is the exact `hx-target` value for the scope-toggle and refresh patterns — pinned during implementation against the existing reflections-panel htmx wiring.
