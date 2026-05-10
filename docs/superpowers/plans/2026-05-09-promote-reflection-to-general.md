# Promote Reflection to General Scope — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Promote to general" button to the Reflections drawer so the user can flip a project-scoped reflection's `scope` column to `general`, making it visible from `memory.retrieve` in every project.

**Architecture:** Tiny vertical slice — new `ReflectionService.promote_to_general()` method, new `POST /reflections/<id>/promote` route, drawer template gains a `Scope` meta row and a gated promote button, `reflection_detail` query / `ReflectionFull` dataclass gain a `scope` field. No schema migration. Status guard (`pending_review` / `confirmed` only) enforced both server-side and in the template.

**Tech Stack:** Python 3.12, sqlite3, Flask, htmx, Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-05-09-promote-reflection-to-general-design.md`

---

## File Structure

| File | Role |
| --- | --- |
| `better_memory/services/reflection.py` | Add `ReflectionService.promote_to_general()` method, sibling of `confirm` / `retire` / `update_text`. |
| `better_memory/ui/queries.py` | Add `scope: str` to `ReflectionFull` dataclass; include `scope` in `reflection_detail` SELECT and constructor. |
| `better_memory/ui/app.py` | Add `POST /reflections/<id>/promote` route, modelled on `reflection_confirm`. |
| `better_memory/ui/templates/fragments/reflection_drawer.html` | Add `Scope` meta row; add gated `Promote to general` button inside the existing actions block. |
| `tests/services/test_reflection_writes.py` | Add `TestPromoteToGeneral` class (6 tests). |
| `tests/ui/test_queries_reflections.py` | Extend `_seed` helper with `scope` param; add scope-population assertion for `reflection_detail`. |
| `tests/ui/test_reflections.py` | Extend `_seed_reflection` helper with `scope` param; add `TestReflectionPromote` class (3 tests) + 2 drawer template tests. |

---

## Task 1: Read-model — add `scope` to `ReflectionFull` and `reflection_detail`

**Files:**
- Modify: `better_memory/ui/queries.py:298-314` (`ReflectionFull` dataclass), `better_memory/ui/queries.py:357-363` (SELECT), `better_memory/ui/queries.py:404-418` (constructor).
- Modify: `tests/ui/test_queries_reflections.py:31-59` (`_seed` helper); `tests/ui/test_queries_reflections.py` (existing `TestReflectionDetail` block — add new test).

The drawer template needs `detail.reflection.scope` to render the meta row and gate the button. Without this change, the template would crash on `AttributeError`. Do this task first so subsequent tasks can rely on the field.

- [ ] **Step 1: Extend the test seed helper to support `scope`**

In `tests/ui/test_queries_reflections.py`, modify `_seed`:

```python
def _seed(
    conn,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    created_at: str = "2026-04-25T10:00:00+00:00",
    updated_at: str = "2026-04-25T10:00:00+00:00",
    evidence_count: int = 0,
    scope: str = "project",
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        "confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, title or f"title-{rid}", project, tech, phase, polarity,
            use_cases, hints, confidence, status, evidence_count,
            created_at, updated_at, scope,
        ),
    )
    conn.commit()
```

- [ ] **Step 2: Write failing tests for the new `scope` field**

In `tests/ui/test_queries_reflections.py`, append to the `TestReflectionDetail` class:

```python
    def test_returns_default_project_scope_when_unspecified(self, conn):
        _seed(conn, rid="r-1")  # default scope='project'
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.scope == "project"

    def test_returns_general_scope_when_seeded_general(self, conn):
        _seed(conn, rid="r-1", scope="general")
        detail = reflection_detail(conn, reflection_id="r-1")
        assert detail is not None
        assert detail.reflection.scope == "general"
```

- [ ] **Step 3: Run the new tests; confirm they fail**

Run: `uv run pytest tests/ui/test_queries_reflections.py::TestReflectionDetail -v`

Expected: the two new tests fail with `AttributeError: 'ReflectionFull' object has no attribute 'scope'` (or similar).

- [ ] **Step 4: Add `scope` to `ReflectionFull`**

In `better_memory/ui/queries.py`, modify the `ReflectionFull` dataclass (currently lines 298-314). Add `scope: str` after `evidence_count`:

```python
@dataclass(frozen=True)
class ReflectionFull:
    """Full reflection row for the drawer."""

    id: str
    title: str
    project: str
    tech: str | None
    phase: str
    polarity: str
    confidence: float
    status: str
    use_cases: str
    hints: str
    evidence_count: int
    scope: str
    created_at: str
    updated_at: str
```

(Keep `created_at` / `updated_at` last — they were last before; `scope` slots in just before them.)

- [ ] **Step 5: Add `scope` to the `reflection_detail` SELECT and constructor**

In `better_memory/ui/queries.py`, modify the `r_row` SELECT inside `reflection_detail` (currently lines 357-363):

```python
    r_row = conn.execute(
        "SELECT id, title, project, tech, phase, polarity, "
        "confidence, status, use_cases, hints, evidence_count, scope, "
        "created_at, updated_at "
        "FROM reflections WHERE id = ?",
        (reflection_id,),
    ).fetchone()
```

And the `ReflectionFull(...)` constructor call (currently lines 404-418):

```python
    return ReflectionDetail(
        reflection=ReflectionFull(
            id=r_row["id"],
            title=r_row["title"],
            project=r_row["project"],
            tech=r_row["tech"],
            phase=r_row["phase"],
            polarity=r_row["polarity"],
            confidence=r_row["confidence"],
            status=r_row["status"],
            use_cases=r_row["use_cases"],
            hints=r_row["hints"],
            evidence_count=r_row["evidence_count"],
            scope=r_row["scope"],
            created_at=r_row["created_at"],
            updated_at=r_row["updated_at"],
        ),
        sources=sources,
    )
```

- [ ] **Step 6: Run the queries tests; confirm they pass**

Run: `uv run pytest tests/ui/test_queries_reflections.py -v`

Expected: all tests pass, including the two new scope tests.

- [ ] **Step 7: Run the broader test suite to catch any consumer that builds `ReflectionFull` positionally**

Run: `uv run pytest tests -x -q`

Expected: PASS. If any test fails because it constructed `ReflectionFull` with positional args, fix it to use keyword args (or update positional order). The dataclass is `frozen=True` and called by name in the existing code, so this should not be an issue — but verify.

- [ ] **Step 8: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_reflections.py
git commit -m "feat(reflections): expose scope field on reflection_detail read model"
```

---

## Task 2: Service method — `ReflectionService.promote_to_general`

**Files:**
- Modify: `better_memory/services/reflection.py` (the `ReflectionService` class — append the new method).
- Modify: `tests/services/test_reflection_writes.py:31-40` (`_seed_reflection` helper); append `TestPromoteToGeneral` class.

- [ ] **Step 1: Extend the test seed helper to support `scope`**

In `tests/services/test_reflection_writes.py`, modify `_seed_reflection`:

```python
def _seed_reflection(
    conn, reflection_id: str, status: str = "pending_review",
    *, scope: str = "project",
) -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, scope, created_at, updated_at) "
        "VALUES (?, ?, 'proj-a', 'general', 'do', 'old uc', 'old h', "
        "0.7, ?, ?, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')",
        (reflection_id, f"title-{reflection_id}", status, scope),
    )
    conn.commit()
```

- [ ] **Step 2: Write the failing service tests**

Append to `tests/services/test_reflection_writes.py`:

```python
class TestPromoteToGeneral:
    def test_promotes_pending_review_project_to_general(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review", scope="project")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.promote_to_general(reflection_id="r1")

        row = conn.execute(
            "SELECT scope, updated_at FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["scope"] == "general"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_promotes_confirmed_project_to_general(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed", scope="project")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.promote_to_general(reflection_id="r1")

        row = conn.execute(
            "SELECT scope FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["scope"] == "general"

    def test_promote_is_idempotent_on_already_general(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review", scope="general")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.promote_to_general(reflection_id="r1")

        row = conn.execute(
            "SELECT scope, updated_at FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["scope"] == "general"
        # No-op: updated_at NOT bumped (matches confirm/retire idempotency).
        assert row["updated_at"] == "2026-04-25T00:00:00+00:00"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.promote_to_general(reflection_id="nope")

    def test_raises_when_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired", scope="project")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot promote reflection in status 'retired'"):
            svc.promote_to_general(reflection_id="r1")

    def test_raises_when_superseded(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="superseded", scope="project")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot promote reflection in status 'superseded'"):
            svc.promote_to_general(reflection_id="r1")
```

- [ ] **Step 3: Run the new tests; confirm they fail**

Run: `uv run pytest tests/services/test_reflection_writes.py::TestPromoteToGeneral -v`

Expected: all six tests fail with `AttributeError: 'ReflectionService' object has no attribute 'promote_to_general'`.

- [ ] **Step 4: Implement `promote_to_general` on `ReflectionService`**

In `better_memory/services/reflection.py`, append a new method to the `ReflectionService` class (after `update_text`):

```python
    def promote_to_general(self, *, reflection_id: str) -> None:
        """project → general; idempotent on already-general; raise on retired/superseded.

        Mirrors the no-op-on-already-target semantics of ``confirm`` and
        ``retire``: when the reflection is already general we return
        without bumping ``updated_at`` so audit trails stay honest.

        Status guard matches the UI gate in the drawer template — the
        button is hidden on retired/superseded, but we enforce server
        side too in case of direct API calls.
        """
        row = self._conn.execute(
            "SELECT scope, status FROM reflections WHERE id = ?",
            (reflection_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Reflection not found: {reflection_id}")
        status = row["status"]
        if status not in ("pending_review", "confirmed"):
            raise ValueError(
                f"Cannot promote reflection in status {status!r}"
            )
        if row["scope"] == "general":
            return
        now = self._clock().isoformat()
        self._conn.execute(
            "UPDATE reflections SET scope = 'general', updated_at = ? "
            "WHERE id = ?",
            (now, reflection_id),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run the service tests; confirm they pass**

Run: `uv run pytest tests/services/test_reflection_writes.py -v`

Expected: all tests pass, including the six new `TestPromoteToGeneral` cases.

- [ ] **Step 6: Commit**

```bash
git add better_memory/services/reflection.py tests/services/test_reflection_writes.py
git commit -m "feat(reflections): add ReflectionService.promote_to_general"
```

---

## Task 3: Route — `POST /reflections/<id>/promote`

**Files:**
- Modify: `better_memory/ui/app.py` (the `/reflections` route block — append after `reflection_edit_save`).
- Modify: `tests/ui/test_reflections.py:13-43` (`_seed_reflection` helper); append `TestReflectionPromote` class.

- [ ] **Step 1: Extend the test seed helper to support `scope`**

In `tests/ui/test_reflections.py`, modify `_seed_reflection`:

```python
def _seed_reflection(
    db_path: Path,
    *,
    rid: str,
    project: str = "proj-a",
    tech: str | None = None,
    phase: str = "general",
    polarity: str = "do",
    confidence: float = 0.7,
    status: str = "confirmed",
    use_cases: str = "uc",
    hints: str = "h",
    title: str | None = None,
    evidence_count: int = 0,
    scope: str = "project",
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, title or f"title-{rid}", project, tech, phase, polarity,
                use_cases, hints, confidence, status, evidence_count, scope,
            ),
        )
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Write the failing route tests**

Append to `tests/ui/test_reflections.py`:

```python
class TestReflectionPromote:
    def test_promotes_project_pending(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="pending_review", scope="project")

        response = client.post(
            "/reflections/r-1/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "reflection-changed"

        conn = connect(tmp_db)
        try:
            row = conn.execute(
                "SELECT scope FROM reflections WHERE id = ?", ("r-1",)
            ).fetchone()
        finally:
            conn.close()
        assert row["scope"] == "general"

    def test_404_for_unknown(self, client: FlaskClient):
        response = client.post(
            "/reflections/does-not-exist/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 404

    def test_409_for_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="retired", scope="project")

        response = client.post(
            "/reflections/r-1/promote",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
        body = response.get_data(as_text=True)
        assert "card-error" in body
```

- [ ] **Step 3: Run the new tests; confirm they fail**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionPromote -v`

Expected: all three tests fail with 404 (route doesn't exist) — Flask returns 404 for unmapped paths.

- [ ] **Step 4: Add the `reflection_promote` route**

In `better_memory/ui/app.py`, immediately after the `reflection_edit_save` function (currently ends around line 367), insert:

```python
    @app.post("/reflections/<id>/promote")
    def reflection_promote(id: str) -> tuple[str, int, dict[str, str]]:
        conn = app.extensions["db_connection"]
        if queries.reflection_detail(conn, reflection_id=id) is None:
            abort(404)
        try:
            app.extensions["reflection_service"].promote_to_general(
                reflection_id=id,
            )
        except ValueError as exc:
            return (
                f'<div class="card card-error">'
                f"<p>{escape(str(exc))}</p>"
                "</div>"
            ), 409, {}
        detail = queries.reflection_detail(conn, reflection_id=id)
        rendered = render_template(
            "fragments/reflection_drawer.html", detail=detail
        )
        return rendered, 200, {"HX-Trigger": "reflection-changed"}
```

- [ ] **Step 5: Run the route tests; confirm they pass**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionPromote -v`

Expected: all three tests pass.

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/app.py tests/ui/test_reflections.py
git commit -m "feat(reflections): add POST /reflections/<id>/promote route"
```

---

## Task 4: Template — `Scope` meta row + gated `Promote to general` button

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html`.
- Modify: `tests/ui/test_reflections.py` (append two drawer-render tests in a new `TestReflectionDrawerScope` class).

- [ ] **Step 1: Write the failing drawer-render tests**

Append to `tests/ui/test_reflections.py`:

```python
class TestReflectionDrawerScope:
    def test_drawer_shows_scope_meta_and_promote_button_for_active_project(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="pending_review", scope="project",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row visible.
        assert "Scope" in body
        assert ">project<" in body
        # Promote button rendered.
        assert "Promote to general" in body
        assert "action-promote" in body
        assert "/reflections/r-1/promote" in body

    def test_drawer_hides_promote_button_when_already_general(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="pending_review", scope="general",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row still visible (now general).
        assert "Scope" in body
        assert ">general<" in body
        # No promote button.
        assert "Promote to general" not in body
        assert "action-promote" not in body

    def test_drawer_hides_promote_button_when_retired(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(
            tmp_db, rid="r-1", status="retired", scope="project",
        )
        response = client.get("/reflections/r-1/drawer")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Scope meta row still visible.
        assert "Scope" in body
        # No promote button (existing actions block already hidden on retired).
        assert "Promote to general" not in body
        assert "action-promote" not in body
```

- [ ] **Step 2: Run the new tests; confirm they fail**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionDrawerScope -v`

Expected: all three tests fail — first one fails because `Scope` / `Promote to general` are not in the body; the others may pass accidentally (no button rendered today). Either way, expect at least one failure.

- [ ] **Step 3: Add the `Scope` meta row to the drawer template**

In `better_memory/ui/templates/fragments/reflection_drawer.html`, find the existing `<dl class="drawer-meta">` block. Insert a new `<dt>/<dd>` pair right after the `Status` line so lifecycle metadata clusters together. The new block (lines 9-23 currently) should become:

```html
  <dl class="drawer-meta">
    <dt>Project</dt><dd>{{ detail.reflection.project }}</dd>
    {% if detail.reflection.tech %}
      <dt>Tech</dt><dd>{{ detail.reflection.tech }}</dd>
    {% endif %}
    <dt>Phase</dt>
    <dd><span class="phase-badge phase-{{ detail.reflection.phase }}">{{ detail.reflection.phase }}</span></dd>
    <dt>Polarity</dt>
    <dd><span class="polarity-badge polarity-{{ detail.reflection.polarity }}">{{ detail.reflection.polarity }}</span></dd>
    <dt>Confidence</dt>
    <dd>{{ '%.2f' | format(detail.reflection.confidence) }}</dd>
    <dt>Status</dt><dd>{{ detail.reflection.status }}</dd>
    <dt>Scope</dt><dd>{{ detail.reflection.scope }}</dd>
    <dt>Evidence</dt><dd>{{ detail.reflection.evidence_count }} observation{{ 's' if detail.reflection.evidence_count != 1 else '' }}</dd>
    <dt>Updated</dt><dd>{{ detail.reflection.updated_at }}</dd>
  </dl>
```

- [ ] **Step 4: Add the gated `Promote to general` button inside the actions block**

Still in `reflection_drawer.html`, find the existing `{% if detail.reflection.status in ('pending_review', 'confirmed') %}` actions block. Inside it, after the `Edit` button (lines 60-66 currently) and before the closing `</div>` of `drawer-actions`, insert:

```html
      {% if detail.reflection.scope == 'project' %}
        <button type="button"
                class="action-promote"
                hx-post="{{ url_for('reflection_promote', id=detail.reflection.id) }}"
                hx-target="#reflection-drawer"
                hx-swap="innerHTML">
          Promote to general
        </button>
      {% endif %}
```

The full updated actions block should now read:

```html
  {% if detail.reflection.status in ('pending_review', 'confirmed') %}
    <div class="drawer-actions">
      {% if detail.reflection.status == 'pending_review' %}
        <button type="button"
                class="action-confirm"
                hx-post="{{ url_for('reflection_confirm', id=detail.reflection.id) }}"
                hx-target="#reflection-drawer"
                hx-swap="innerHTML">
          Confirm
        </button>
      {% endif %}
      <button type="button"
              class="action-retire"
              hx-post="{{ url_for('reflection_retire', id=detail.reflection.id) }}"
              hx-target="#reflection-drawer"
              hx-swap="innerHTML">
        Retire
      </button>
      <button type="button"
              class="action-edit"
              hx-get="{{ url_for('reflection_edit_form', id=detail.reflection.id) }}"
              hx-target="#reflection-drawer"
              hx-swap="innerHTML">
        Edit
      </button>
      {% if detail.reflection.scope == 'project' %}
        <button type="button"
                class="action-promote"
                hx-post="{{ url_for('reflection_promote', id=detail.reflection.id) }}"
                hx-target="#reflection-drawer"
                hx-swap="innerHTML">
          Promote to general
        </button>
      {% endif %}
    </div>
  {% endif %}
```

- [ ] **Step 5: Run the drawer tests; confirm they pass**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionDrawerScope -v`

Expected: all three tests pass.

- [ ] **Step 6: Run the full reflections test surface**

Run: `uv run pytest tests/ui/test_reflections.py tests/services/test_reflection_writes.py tests/ui/test_queries_reflections.py -v`

Expected: PASS (all four feature tasks now landed).

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/fragments/reflection_drawer.html tests/ui/test_reflections.py
git commit -m "feat(reflections): drawer shows scope and gated promote button"
```

---

## Task 5: End-to-end smoke + full suite

**Files:**
- No new files. Verification only.

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest -x -q`

Expected: PASS, no regressions. If any unrelated test fails for environmental reasons (e.g. Ollama not running), note it and proceed; the new feature must not introduce new failures.

- [ ] **Step 2: Manual UI smoke**

Start (or re-use) the UI:

```bash
cd /c/Users/gethi/source/better-memory && uv run python -m better_memory.ui &
url=$(cat ~/.better-memory/ui.url) && start "$url"
```

In the browser:
1. Navigate to the Reflections tab.
2. Open a reflection drawer for any project-scoped row.
3. Confirm the `Scope: project` meta row is visible.
4. Confirm a `Promote to general` button is visible in the actions row alongside `Confirm` / `Retire` / `Edit`.
5. Click `Promote to general`. The drawer should re-render with `Scope: general` and the promote button gone.
6. Open a `general`-scoped row (or the same one again post-promotion). Confirm no promote button is shown.
7. Open a `retired` row. Confirm no actions block is rendered (the existing status guard still applies).

If any of these fail, fix the underlying issue (template gate, route, service) before continuing.

- [ ] **Step 3: Run pyright on the touched files**

Run: `uv run pyright better_memory/services/reflection.py better_memory/ui/queries.py better_memory/ui/app.py`

Expected: 0 errors, 0 warnings.

- [ ] **Step 4: Final commit (if any cleanup landed)**

If steps 2 or 3 surfaced fixes, commit them with a clear message. Otherwise this step is a no-op.

```bash
git status   # confirm clean
```

---

## Self-Review

**Spec coverage:**
- Service method (Spec → Service layer) → Task 2.
- Route (Spec → Route) → Task 3.
- Read-model `scope` field (Spec → Read model) → Task 1.
- Template `Scope` meta + gated promote button (Spec → Templates) → Task 4.
- Service tests (Spec → Service tests, 6 cases) → Task 2 step 2.
- Route tests (Spec → Route tests, 3 of 5 cases) → Task 3 step 2.
- Drawer template tests (Spec → Route tests, remaining 2 cases) → Task 4 step 1.
- Read-model test for scope population → Task 1 step 2.

**Placeholder scan:** none.

**Type / signature consistency:**
- `ReflectionFull.scope: str` — used the same field name in template, route handler, and tests.
- `ReflectionService.promote_to_general(*, reflection_id: str) -> None` — keyword-only arg matching `confirm` / `retire` / `update_text`.
- Route `reflection_promote(id: str)` — `id` matches the URL var name, matching sibling routes.
- HX-Trigger `"reflection-changed"` — matches the value used by `reflection_confirm` / `reflection_retire` / `reflection_edit_save`.
- Idempotency wording (`"already general"` no-op, no `updated_at` bump) matches `confirm` / `retire` semantics.

No gaps detected.
