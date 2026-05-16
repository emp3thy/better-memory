# Inline Rating Indicators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface each memory's value signal inline on its list row across the Observations, Reflections, and Semantic pages.

**Architecture:** Mostly Jinja template work. One read-model change adds `reinforcement_score` to the observation row model in `queries.py`. A shared `_rating_stat.html` partial renders the useful/misled badge pair for reflection and semantic rows. CSS uses the existing brutalist palette — ink (useful), amber (misled), muted grey (zero / default). No schema migration: every column read here already exists.

**Tech Stack:** Python 3.12, Flask, Jinja2, SQLite, HTMX, pytest. Tests are Flask test-client render tests (the established pattern in `tests/ui/test_observations.py` etc.) — they assert the rendered HTML fragment contains the expected text and class names, which is exactly what this feature changes.

**Spec:** `docs/superpowers/specs/2026-05-15-inline-memory-rating-indicators-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `better_memory/ui/queries.py` | UI read-models | Add `reinforcement_score` to `ObservationRow` + its query |
| `better_memory/ui/templates/fragments/_rating_stat.html` | Useful/misled badge pair | **New** — shared partial |
| `better_memory/ui/templates/fragments/reflection_row.html` | Reflection list row | Use the partial |
| `better_memory/ui/templates/fragments/semantic_row.html` | Semantic list row | Use the partial |
| `better_memory/ui/templates/fragments/observation_row.html` | Observation list row | Add reinforcement indicator |
| `better_memory/ui/templates/fragments/reflection_drawer.html` | Reflection drawer | Always render the Misled line |
| `better_memory/ui/templates/fragments/semantic_drawer.html` | Semantic drawer | Always render the Misled line |
| `better_memory/ui/static/app.css` | UI styles | Rating-badge, reinforcement, `.text-danger` classes |

---

## Task 1: `reinforcement_score` on the observation read-model

**Confidence: 98%**

The drawer model `ObservationFull` already carries `reinforcement_score`; the list-row model `ObservationRow` does not. Add it. This is the only Python change in the feature.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Test: `tests/ui/test_queries_observations.py`

- [ ] **Step 1: Write the failing test**

Add this method to the `TestObservationListForUi` class in `tests/ui/test_queries_observations.py`:

```python
    def test_row_carries_reinforcement_score(self, conn):
        _seed_episode(conn)
        conn.execute(
            "INSERT INTO observations "
            "(id, content, project, component, theme, outcome, status, "
            " episode_id, reinforcement_score, created_at) "
            "VALUES ('o-r', 'x', 'proj-a', 'ui_launcher', 'bug', "
            " 'neutral', 'active', 'ep-1', 2.5, "
            " '2026-04-26T10:00:00+00:00')"
        )
        conn.commit()

        [row] = observation_list_for_ui(conn, project="proj-a")
        assert row.reinforcement_score == 2.5
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/ui/test_queries_observations.py::TestObservationListForUi::test_row_carries_reinforcement_score -v`
Expected: FAIL — `AttributeError: 'ObservationRow' object has no attribute 'reinforcement_score'`.

- [ ] **Step 3: Add the field to the `ObservationRow` dataclass**

In `better_memory/ui/queries.py`, the `ObservationRow` dataclass currently ends with `episode_id: str | None`. Add a field after it:

```python
@dataclass(frozen=True)
class ObservationRow:
    id: str
    content: str
    project: str
    component: str | None
    theme: str | None
    outcome: str
    status: str
    created_at: str
    episode_id: str | None
    reinforcement_score: float
```

- [ ] **Step 4: Add the column to the query and constructor**

In `observation_list_for_ui`, change the SELECT column list and the `ObservationRow(...)` constructor:

```python
    sql = (
        "SELECT id, content, project, component, theme, outcome, status, "
        "       created_at, episode_id, reinforcement_score "
        "FROM observations "
        f"{where} "
        "ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?"
    )
    params.append(limit)
    return [
        ObservationRow(
            id=r["id"],
            content=r["content"],
            project=r["project"],
            component=r["component"],
            theme=r["theme"],
            outcome=r["outcome"],
            status=r["status"],
            created_at=r["created_at"],
            episode_id=r["episode_id"],
            reinforcement_score=r["reinforcement_score"],
        )
        for r in conn.execute(sql, params).fetchall()
    ]
```

- [ ] **Step 5: Run the test, verify it passes**

Run: `uv run pytest tests/ui/test_queries_observations.py -v`
Expected: PASS (all tests in the file, including the new one).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries_observations.py
git commit -m "feat(ui): carry reinforcement_score on the observation row model"
```

---

## Task 2: `_rating_stat.html` partial + reflection row + CSS

**Confidence: 95%**

A shared partial renders the `useful N` / `misled N` badge pair. Reflection rows use it. `ReflectionListRow` already carries `useful_count` and `times_misled`, so no query change.

**Files:**
- Create: `better_memory/ui/templates/fragments/_rating_stat.html`
- Modify: `better_memory/ui/templates/fragments/reflection_row.html`
- Modify: `better_memory/ui/static/app.css`
- Test: `tests/ui/test_reflections.py`

- [ ] **Step 1: Extend the `_seed_reflection` test helper**

In `tests/ui/test_reflections.py`, the `_seed_reflection` helper does not set the rating counters. Add two keyword params and two columns. Replace the helper's signature and `conn.execute` call with:

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
    useful_count: int = 0,
    times_misled: int = 0,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reflections "
            "(id, title, project, tech, phase, polarity, use_cases, hints, "
            "confidence, status, evidence_count, scope, useful_count, "
            "times_misled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'2026-04-26T10:00:00+00:00', '2026-04-26T10:00:00+00:00')",
            (
                rid, title or f"title-{rid}", project, tech, phase, polarity,
                use_cases, hints, confidence, status, evidence_count, scope,
                useful_count, times_misled,
            ),
        )
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Write the failing tests**

Add this class to `tests/ui/test_reflections.py`:

```python
class TestReflectionRowRatingStat:
    def test_row_shows_both_badges_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Zero rated")

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "useful 0" in body
        assert "misled 0" in body
        # Both badges grey (default-value) at zero.
        assert body.count("rating-zero") >= 2

    def test_useful_badge_inked_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Useful one", useful_count=3)

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "useful 3" in body
        assert "rating-useful" in body

    def test_misled_badge_ambered_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", title="Misled one", times_misled=2)

        body = client.get(
            "/reflections/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "misled 2" in body
        assert "rating-misled" in body
```

- [ ] **Step 3: Run the tests, verify they fail**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionRowRatingStat -v`
Expected: FAIL — the current `reflection_row.html` renders the `★ useful` badge only when `useful_count > 0`, so `"useful 0"` / `"misled 0"` / `rating-*` strings are absent.

- [ ] **Step 4: Create the shared partial**

Create `better_memory/ui/templates/fragments/_rating_stat.html`:

```html
{# Useful / misled rating pair for a memory list row. Expects
   `rating_useful` and `rating_misled` (ints) in context — each row
   supplies them via {% with %}. Each badge is classed by its own
   count: rating-useful / rating-misled when > 0, else rating-zero
   (grey, default-value). #}
<span class="rating-stat">
  <span class="rating-badge rating-{{ 'useful' if rating_useful else 'zero' }}">useful {{ rating_useful or 0 }}</span>
  <span class="rating-badge rating-{{ 'misled' if rating_misled else 'zero' }}">misled {{ rating_misled or 0 }}</span>
</span>
```

- [ ] **Step 5: Use the partial in `reflection_row.html`**

In `better_memory/ui/templates/fragments/reflection_row.html`, replace this block:

```html
      {% if row.useful_count > 0 %}
      <span class="badge bg-success" title="Times this reflection was useful">
        ★ useful: {{ row.useful_count }}
      </span>
      {% endif %}
```

with:

```html
      {% with rating_useful = row.useful_count, rating_misled = row.times_misled %}
        {% include "fragments/_rating_stat.html" %}
      {% endwith %}
```

- [ ] **Step 6: Add the rating-badge CSS**

Append to `better_memory/ui/static/app.css`:

```css
/* ============================================================
   Rating indicators — useful / misled badge pair
   ============================================================ */
.rating-stat { display: inline-flex; gap: 6px; }

.rating-badge {
  font-family: var(--brut-mono);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 2px 7px;
  white-space: nowrap;
  border: 1.5px solid var(--brut-rule);
  background: var(--brut-paper);
  color: var(--brut-muted);
}
.rating-badge.rating-useful {
  border-color: var(--brut-ink);
  background: var(--brut-ink);
  color: var(--brut-paper);
}
.rating-badge.rating-misled {
  border-color: var(--brut-ink);
  background: var(--brut-amber);
  color: var(--brut-amber-ink);
}
/* .rating-zero keeps the muted base style — no override needed. */
```

- [ ] **Step 7: Run the tests, verify they pass**

Run: `uv run pytest tests/ui/test_reflections.py -v`
Expected: PASS (whole file — confirms the row change did not break existing reflection tests).

- [ ] **Step 8: Commit**

```bash
git add better_memory/ui/templates/fragments/_rating_stat.html better_memory/ui/templates/fragments/reflection_row.html better_memory/ui/static/app.css tests/ui/test_reflections.py
git commit -m "feat(ui): inline useful/misled badge pair on reflection rows"
```

---

## Task 3: Semantic row uses the partial

**Confidence: 96%**

`SemanticMemory` already carries `useful_count` and `times_misled` (verified in `better_memory/services/semantic.py`), so no service change is needed — only the row template.

**Files:**
- Modify: `better_memory/ui/templates/fragments/semantic_row.html`
- Test: `tests/ui/test_semantic.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/ui/test_semantic.py`:

```python
class TestSemanticRowRatingStat:
    def test_row_shows_both_badges_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "useful 0" in body
        assert "misled 0" in body
        assert body.count("rating-zero") >= 2

    def test_badges_coloured_when_positive(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, useful_count, times_misled, "
                " created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project', 4, 1,"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/panel").get_data(as_text=True)
        assert "useful 4" in body
        assert "rating-useful" in body
        assert "misled 1" in body
        assert "rating-misled" in body
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/ui/test_semantic.py::TestSemanticRowRatingStat -v`
Expected: FAIL — `semantic_row.html` currently shows the `★ useful` badge only when `useful_count > 0`.

- [ ] **Step 3: Use the partial in `semantic_row.html`**

In `better_memory/ui/templates/fragments/semantic_row.html`, replace this block:

```html
    {% if row.useful_count > 0 %}
    <span class="badge bg-success" title="Times this memory was useful">
      ★ useful: {{ row.useful_count }}
    </span>
    {% endif %}
```

with:

```html
    {% with rating_useful = row.useful_count, rating_misled = row.times_misled %}
      {% include "fragments/_rating_stat.html" %}
    {% endwith %}
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/ui/test_semantic.py -v`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/templates/fragments/semantic_row.html tests/ui/test_semantic.py
git commit -m "feat(ui): inline useful/misled badge pair on semantic rows"
```

---

## Task 4: Observation row reinforcement indicator

**Confidence: 95%**

Add a reinforcement-score indicator to the observation row. Depends on Task 1 (the row model must carry `reinforcement_score`).

**Files:**
- Modify: `better_memory/ui/templates/fragments/observation_row.html`
- Modify: `better_memory/ui/static/app.css`
- Test: `tests/ui/test_observations.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/ui/test_observations.py` (the file already imports `connect` and defines a module-level `_seed_episode` with `project="proj-a"`):

```python
class TestObservationRowReinforcement:
    def _seed_obs_score(self, db_path: Path, *, oid: str, score: float) -> None:
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO observations "
                "(id, content, project, component, theme, outcome, status, "
                " episode_id, reinforcement_score, created_at) VALUES "
                "(?, 'obs body', 'proj-a', 'ui_launcher', 'bug', 'neutral', "
                " 'active', 'ep-1', ?, '2026-04-26T10:00:00+00:00')",
                (oid, score),
            )
            conn.commit()
        finally:
            conn.close()

    def test_positive_score_takes_pos_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=2.5)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf 2.5" in body
        assert "reinf-pos" in body

    def test_negative_score_takes_neg_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=-1.5)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf -1.5" in body
        assert "reinf-neg" in body

    def test_zero_score_takes_zero_class(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_episode(tmp_db)
        self._seed_obs_score(tmp_db, oid="o-1", score=0.0)
        body = client.get(
            "/observations/panel?project=proj-a"
        ).get_data(as_text=True)
        assert "reinf 0.0" in body
        assert "reinf-zero" in body
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/ui/test_observations.py::TestObservationRowReinforcement -v`
Expected: FAIL — `observation_row.html` renders nothing about reinforcement.

- [ ] **Step 3: Add the reinforcement indicator to `observation_row.html`**

In `better_memory/ui/templates/fragments/observation_row.html`, insert the reinforcement span between the `.time` span and the `.content` span:

```html
  <span class="time">{{ row.created_at[11:16] }}</span>
  {% set rscore = row.reinforcement_score | round(1) %}
  <span class="reinforcement-stat reinf-{{ 'pos' if rscore > 0 else 'neg' if rscore < 0 else 'zero' }}"
        title="Reinforcement score">reinf {{ '%.1f' | format(rscore or 0.0) }}</span>
  <span class="content">{{ row.content }}</span>
```

The `rscore or 0.0` guards the `-0.0` display case — a tiny negative score that rounds to zero renders as `0.0` and takes the `reinf-zero` class.

- [ ] **Step 4: Add the reinforcement CSS**

Append to `better_memory/ui/static/app.css`:

```css
/* Reinforcement score indicator — observation rows */
.reinforcement-stat {
  font-family: var(--brut-mono);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--brut-muted);
  margin-right: 10px;
}
.reinforcement-stat.reinf-pos { color: var(--brut-ink); }
.reinforcement-stat.reinf-neg { color: var(--brut-amber); }
/* .reinf-zero keeps the muted base colour. */
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `uv run pytest tests/ui/test_observations.py -v`
Expected: PASS (whole file).

- [ ] **Step 6: Commit**

```bash
git add better_memory/ui/templates/fragments/observation_row.html better_memory/ui/static/app.css tests/ui/test_observations.py
git commit -m "feat(ui): inline reinforcement-score indicator on observation rows"
```

---

## Task 5: Drawers always show the Misled line

**Confidence: 97%**

The reflection and semantic drawers render the `Misled` line only when `times_misled > 0`. Drop the guard so it always renders — consistent with the always-shown rows. Also add a `.text-danger` rule (referenced by both drawers but currently undefined).

**Files:**
- Modify: `better_memory/ui/templates/fragments/reflection_drawer.html`
- Modify: `better_memory/ui/templates/fragments/semantic_drawer.html`
- Modify: `better_memory/ui/static/app.css`
- Test: `tests/ui/test_reflections.py`, `tests/ui/test_semantic.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/ui/test_reflections.py`:

```python
class TestReflectionDrawerMisledAlwaysShown:
    def test_drawer_shows_misled_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        _seed_reflection(tmp_db, rid="r-1", status="confirmed")
        body = client.get("/reflections/r-1/drawer").get_data(as_text=True)
        assert "Misled" in body
```

Add to `tests/ui/test_semantic.py`:

```python
class TestSemanticDrawerMisledAlwaysShown:
    def test_drawer_shows_misled_line_at_zero(
        self, client: FlaskClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sqlite3
        from better_memory.ui import app as app_module

        monkeypatch.setattr(app_module, "project_name", lambda: "proj-a")
        with sqlite3.connect(tmp_db) as c:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) VALUES "
                "('m1','rule','proj-a','project',"
                " '2026-05-01T10:00:00+00:00','2026-05-01T10:00:00+00:00')"
            )
            c.commit()
        body = client.get("/semantic/m1/drawer").get_data(as_text=True)
        assert "Misled" in body
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/ui/test_reflections.py::TestReflectionDrawerMisledAlwaysShown tests/ui/test_semantic.py::TestSemanticDrawerMisledAlwaysShown -v`
Expected: FAIL — both drawers omit the `Misled` line when `times_misled == 0`.

- [ ] **Step 3: Remove the guard in `reflection_drawer.html`**

In `better_memory/ui/templates/fragments/reflection_drawer.html`, replace:

```html
    <dt>Useful</dt>
    <dd>{{ detail.reflection.useful_count }}{% if detail.reflection.last_useful_at %} (last: {{ detail.reflection.last_useful_at }}){% endif %}</dd>
    {% if detail.reflection.times_misled > 0 %}
      <dt>Misled</dt>
      <dd class="text-danger">{{ detail.reflection.times_misled }}{% if detail.reflection.last_misled_at %} (last: {{ detail.reflection.last_misled_at }}){% endif %}</dd>
    {% endif %}
```

with:

```html
    <dt>Useful</dt>
    <dd>{{ detail.reflection.useful_count }}{% if detail.reflection.last_useful_at %} (last: {{ detail.reflection.last_useful_at }}){% endif %}</dd>
    <dt>Misled</dt>
    <dd class="text-danger">{{ detail.reflection.times_misled }}{% if detail.reflection.last_misled_at %} (last: {{ detail.reflection.last_misled_at }}){% endif %}</dd>
```

- [ ] **Step 4: Remove the guard in `semantic_drawer.html`**

In `better_memory/ui/templates/fragments/semantic_drawer.html`, replace:

```html
      <dt>Useful</dt>
      <dd>{{ memory.useful_count }}{% if memory.last_useful_at %} (last: {{ memory.last_useful_at }}){% endif %}</dd>
      {% if memory.times_misled > 0 %}
      <dt>Misled</dt>
      <dd class="text-danger">{{ memory.times_misled }}{% if memory.last_misled_at %} (last: {{ memory.last_misled_at }}){% endif %}</dd>
      {% endif %}
```

with:

```html
      <dt>Useful</dt>
      <dd>{{ memory.useful_count }}{% if memory.last_useful_at %} (last: {{ memory.last_useful_at }}){% endif %}</dd>
      <dt>Misled</dt>
      <dd class="text-danger">{{ memory.times_misled }}{% if memory.last_misled_at %} (last: {{ memory.last_misled_at }}){% endif %}</dd>
```

- [ ] **Step 5: Add the `.text-danger` rule**

Append to `better_memory/ui/static/app.css`:

```css
/* Misled count emphasis — drawer meta */
.text-danger { color: var(--brut-amber); font-weight: 700; }
```

- [ ] **Step 6: Run the tests, verify they pass**

Run: `uv run pytest tests/ui/test_reflections.py tests/ui/test_semantic.py -v`
Expected: PASS (both files).

- [ ] **Step 7: Commit**

```bash
git add better_memory/ui/templates/fragments/reflection_drawer.html better_memory/ui/templates/fragments/semantic_drawer.html better_memory/ui/static/app.css tests/ui/test_reflections.py tests/ui/test_semantic.py
git commit -m "feat(ui): drawers always show the Misled line"
```

---

## Final verification

- [ ] **Run the full UI test suite**

Run: `uv run pytest tests/ui/ -v -m "not integration"`
Expected: PASS — all UI tests, confirming no regression in observation / reflection / semantic / drawer rendering.

- [ ] **Type-check**

Run: `uv run pyright better_memory/ui/queries.py`
Expected: no new errors (the added `reinforcement_score: float` field is fully typed).

- [ ] **Manual smoke check**

Launch the UI (`memory.start_ui` or `uv run python -m better_memory.ui`) and confirm: reflection and semantic rows show the `useful N` / `misled N` pair (grey at 0, ink/amber when set); observation rows show `reinf N.N`; the reflection and semantic drawers show the `Misled` line even at 0.
