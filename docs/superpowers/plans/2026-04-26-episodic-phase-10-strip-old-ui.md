# Episodic Memory Phase 10 — Strip Old UI Surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the pre-episodic UI surfaces (Pipeline / Sweep / Knowledge / Audit / Graph) along with the supporting service-layer code (`ConsolidationService`, `InsightService`), the UI jobs module, the legacy `ui/queries.py` helpers, and all related templates and tests. Leaves the app with a clean two-tab nav (Episodes + Reflections) and no orphan code that bots will flag on review.

**Architecture:** Pure deletion pass — no new logic, no new templates, no schema changes. Phase 1 already dropped the `insights` table; the code stripped here has been dead-on-data since then and nav-orphaned since Phase 8 swapped the navigation. Each task removes a cohesive group of files / routes / fragments and ends with a green test suite. The final test count drops from 438 to roughly 320-340 as the pipeline-era tests come out alongside the production code they cover.

**Tech Stack:** Python 3.12 · Flask · Jinja2 · pytest · uv.

**Scope boundary.** Strip Pipeline / Sweep / Knowledge / Audit / Graph + the `InsightService` + `ConsolidationService` + the UI jobs module + `ui/queries.py` legacy helpers.

**Out of scope** (deferred):
- **`ObservationService.retrieve()` + `BucketedResults`** — orphaned by Phase 6's retrieve-flip (zero production callers, only tests reference them) but logically a Phase 6 cleanup, not a UI cleanup. Leaving for a separate follow-up.
- **Knowledge editor UI** — spec §13 says "may return in a later phase as a standalone surface." `KnowledgeService` itself stays (used by `knowledge.search` / `knowledge.list` MCP tools); only the `/knowledge` Flask route + template are deleted.
- **Retention MCP tool** (Phase 11).
- **End-to-end browser tests** (Phase 12).
- **Pyproject dependency cleanup** — dependencies that were ONLY used by `ConsolidationService` (none identified in initial scan) would be removed in a follow-up; this plan does not edit `pyproject.toml`.

**Reference spec:** `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` §8 ("Pipeline, Sweep, Knowledge, Audit, Graph are removed from the default nav") and §14 (build order phase 10).

**Reference plans:** Phase 8 (`2026-04-25-episodic-phase-8-episodes-ui.md`) for the nav swap that orphaned these routes.

**Pre-existing constraints:**
- The Phase 8 nav already removed Pipeline/Sweep/Knowledge/Audit/Graph anchors. Phase 10 deletes the routes + templates + tests behind them.
- `tests/ui/test_app.py::TestLayoutShell::test_pipeline_renders_base_layout` is already `@pytest.mark.skip`-decorated (it tested `/pipeline` rendering through the now-stripped insights queries). Phase 10 deletes it.
- `tests/ui/test_app.py::TestEmptyViews` (4 tests for `/sweep` `/knowledge` `/audit` `/graph`) currently passes because the routes still exist. Phase 10 deletes those routes AND those tests in the same task.
- `tests/ui/test_browser.py` is module-level skipped (Phase 2-era candidate-card test). Phase 10 deletes that test file (Phase 12 will introduce a new browser-test file targeting Episodes + Reflections).

---

## File Structure

**Files deleted (production code):**
- `better_memory/ui/jobs.py` (consolidation job runner — UI-only, no other callers)
- `better_memory/ui/templates/pipeline.html`
- `better_memory/ui/templates/sweep.html`
- `better_memory/ui/templates/knowledge.html`
- `better_memory/ui/templates/audit.html`
- `better_memory/ui/templates/graph.html`
- `better_memory/ui/templates/fragments/badge.html`
- `better_memory/ui/templates/fragments/observation_card_compact.html`
- `better_memory/ui/templates/fragments/panel_observations.html`
- `better_memory/ui/templates/fragments/panel_candidates.html`
- `better_memory/ui/templates/fragments/panel_insights.html`
- `better_memory/ui/templates/fragments/panel_promoted.html`
- `better_memory/ui/templates/fragments/candidate_card_compact.html`
- `better_memory/ui/templates/fragments/candidate_card_expanded.html`
- `better_memory/ui/templates/fragments/insight_card_compact.html`
- `better_memory/ui/templates/fragments/insight_card_expanded.html`
- `better_memory/ui/templates/fragments/insight_edit_form.html`
- `better_memory/ui/templates/fragments/insight_sources.html`
- `better_memory/ui/templates/fragments/promoted_card_compact.html`
- `better_memory/ui/templates/fragments/merge_picker.html`
- `better_memory/ui/templates/fragments/consolidation_job.html`
- `better_memory/ui/templates/fragments/promotion_stub_modal.html`
- `better_memory/services/consolidation.py`
- `better_memory/services/insight.py`

**Files deleted (tests):**
- `tests/ui/test_pipeline.py`
- `tests/ui/test_apply_job.py`
- `tests/ui/test_consolidation_e2e.py`
- `tests/ui/test_queries.py`
- `tests/ui/test_browser.py`
- `tests/services/test_consolidation.py`
- `tests/services/test_consolidation_integration.py`
- `tests/services/test_insight.py`

**Files modified:**
- `better_memory/ui/app.py` — remove `ConsolidationService` and `InsightService` imports; remove the `chat` parameter to `create_app` and the `OllamaChat` import; remove `app.extensions["insight_service"]` and `app.extensions["chat"]` registrations; delete the route handlers for `/pipeline`, `/pipeline/panel/<stage>`, `/pipeline/badge`, `/pipeline/consolidate`, `/candidates/<id>/...` (8 routes), `/insights/<id>/...` (8 routes), `/jobs/<id>` (2 routes), `/sweep`, `/knowledge`, `/audit`, `/graph`. Remove the `from better_memory.ui import jobs` import and any `jobs.*` calls.
- `better_memory/ui/queries.py` — delete `KanbanCounts`, `kanban_counts`, `ObservationListRow`, `list_observations`, `_list_insights_by_status`, `list_candidates`, `list_insights`, `list_promoted`, `list_insight_sources`. Delete the `from better_memory.services.insight import Insight, row_to_insight` import.
- `tests/ui/conftest.py` — remove `FakeChat` injection (no longer needed once `ConsolidationService` is gone).
- `tests/ui/test_app.py` — delete `TestLayoutShell` (1 skipped test referencing `/pipeline`) and `TestEmptyViews` (4 tests for `/sweep` `/knowledge` `/audit` `/graph`).
- `better_memory/skills/CLAUDE.snippet.md` — no edits required (Phase 6 already swapped the retrieval section to reflections; Phase 8 swapped the UI section to Episodes; Phase 9 added Reflections — none of those reference the dropped surfaces).

---

## Task 0: Worktree

Already created at `C:/Users/gethi/source/better-memory-episodic-phase-10-strip-old-ui` on branch `episodic-phase-10-strip-old-ui` (based on `origin/episodic-phase-1-schema` post-Phase-9-merge). Baseline: **438 passed, 141 skipped, 4 deselected**. Skip this task.

---

## Task 1: Delete the four placeholder routes (sweep / knowledge / audit / graph)

The lightest task: four trivial 1-line route handlers that return their template, four near-empty templates, and four tests in `TestEmptyViews` that exercise them. Land first so subsequent tasks can ignore them entirely.

**Files:**
- Delete: `better_memory/ui/templates/sweep.html`
- Delete: `better_memory/ui/templates/knowledge.html`
- Delete: `better_memory/ui/templates/audit.html`
- Delete: `better_memory/ui/templates/graph.html`
- Modify: `better_memory/ui/app.py` (remove the four route handlers)
- Modify: `tests/ui/test_app.py` (delete `TestEmptyViews` class)

- [ ] **Step 1: Delete the templates**

```bash
rm better_memory/ui/templates/sweep.html
rm better_memory/ui/templates/knowledge.html
rm better_memory/ui/templates/audit.html
rm better_memory/ui/templates/graph.html
```

- [ ] **Step 2: Remove the four routes from `better_memory/ui/app.py`**

Find and delete these handlers (currently around lines 669-682):

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

- [ ] **Step 3: Delete `TestEmptyViews` from `tests/ui/test_app.py`**

Locate the `class TestEmptyViews:` block and delete it entirely. The tests are around lines 67-86 of `test_app.py`. Verify with `grep -n "TestEmptyViews" tests/ui/test_app.py` returning nothing after the edit.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: `434 passed, 141 skipped, 4 deselected` (4 tests deleted from the 438 baseline).

- [ ] **Step 5: Commit**

```bash
git add better_memory/ui/app.py better_memory/ui/templates/ tests/ui/test_app.py
git commit -m "Phase 10: drop placeholder /sweep /knowledge /audit /graph routes + templates"
```

---

## Task 2: Delete the Pipeline tab + Candidate/Insight CRUD + Consolidation/Jobs flow

The largest task. Removes the Pipeline page, all its supporting candidate/insight CRUD routes, the consolidation job flow, the UI `jobs` module, and every template/fragment those routes used. Three test files go with them.

**Files:**
- Delete: `better_memory/ui/jobs.py`
- Delete: `better_memory/ui/templates/pipeline.html`
- Delete: `better_memory/ui/templates/fragments/badge.html`
- Delete: `better_memory/ui/templates/fragments/observation_card_compact.html`
- Delete: `better_memory/ui/templates/fragments/panel_observations.html`
- Delete: `better_memory/ui/templates/fragments/panel_candidates.html`
- Delete: `better_memory/ui/templates/fragments/panel_insights.html`
- Delete: `better_memory/ui/templates/fragments/panel_promoted.html`
- Delete: `better_memory/ui/templates/fragments/candidate_card_compact.html`
- Delete: `better_memory/ui/templates/fragments/candidate_card_expanded.html`
- Delete: `better_memory/ui/templates/fragments/insight_card_compact.html`
- Delete: `better_memory/ui/templates/fragments/insight_card_expanded.html`
- Delete: `better_memory/ui/templates/fragments/insight_edit_form.html`
- Delete: `better_memory/ui/templates/fragments/insight_sources.html`
- Delete: `better_memory/ui/templates/fragments/promoted_card_compact.html`
- Delete: `better_memory/ui/templates/fragments/merge_picker.html`
- Delete: `better_memory/ui/templates/fragments/consolidation_job.html`
- Delete: `better_memory/ui/templates/fragments/promotion_stub_modal.html`
- Delete: `tests/ui/test_pipeline.py`
- Delete: `tests/ui/test_apply_job.py`
- Delete: `tests/ui/test_consolidation_e2e.py`
- Modify: `better_memory/ui/app.py` (delete ~24 route handlers)
- Modify: `tests/ui/test_app.py` (delete `TestLayoutShell` skipped test)

- [ ] **Step 1: Delete the templates**

```bash
rm better_memory/ui/templates/pipeline.html
rm better_memory/ui/templates/fragments/badge.html
rm better_memory/ui/templates/fragments/observation_card_compact.html
rm better_memory/ui/templates/fragments/panel_observations.html
rm better_memory/ui/templates/fragments/panel_candidates.html
rm better_memory/ui/templates/fragments/panel_insights.html
rm better_memory/ui/templates/fragments/panel_promoted.html
rm better_memory/ui/templates/fragments/candidate_card_compact.html
rm better_memory/ui/templates/fragments/candidate_card_expanded.html
rm better_memory/ui/templates/fragments/insight_card_compact.html
rm better_memory/ui/templates/fragments/insight_card_expanded.html
rm better_memory/ui/templates/fragments/insight_edit_form.html
rm better_memory/ui/templates/fragments/insight_sources.html
rm better_memory/ui/templates/fragments/promoted_card_compact.html
rm better_memory/ui/templates/fragments/merge_picker.html
rm better_memory/ui/templates/fragments/consolidation_job.html
rm better_memory/ui/templates/fragments/promotion_stub_modal.html
```

- [ ] **Step 2: Delete the test files**

```bash
rm tests/ui/test_pipeline.py
rm tests/ui/test_apply_job.py
rm tests/ui/test_consolidation_e2e.py
```

- [ ] **Step 3: Delete `better_memory/ui/jobs.py`**

```bash
rm better_memory/ui/jobs.py
```

- [ ] **Step 4: Delete `TestLayoutShell` from `tests/ui/test_app.py`**

Locate the `class TestLayoutShell:` block (currently around lines 49-64, marked `@pytest.mark.skip`) and delete it entirely.

- [ ] **Step 5: Strip the routes from `better_memory/ui/app.py`**

Delete every handler from `pipeline()` through `pipeline_badge()`. The block to remove starts at the `@app.get("/pipeline")` decorator (around line 381) and ends after `pipeline_badge()`'s body (around line 666 — just before `/sweep`, which Task 1 already removed). Specifically:

- `pipeline` (`/pipeline`)
- `pipeline_panel` (`/pipeline/panel/<stage>`)
- `candidate_card` (`/candidates/<id>/card`)
- `candidate_approve` (`/candidates/<id>/approve`)
- `candidate_reject` (`/candidates/<id>/reject`)
- `candidate_edit` (`/candidates/<id>/edit`)
- `candidate_edit_save` (`/candidates/<id>/edit` POST)
- `candidate_compact_card` (`/candidates/<id>/compact`)
- `candidate_merge_picker` (`/candidates/<id>/merge`)
- `candidate_merge` (`/candidates/<id>/merge` POST)
- `insight_card` (`/insights/<id>/card`)
- `insight_promote` (`/insights/<id>/promote`)
- `insight_retire` (`/insights/<id>/retire`)
- `insight_demote` (`/insights/<id>/demote`)
- `insight_edit` (`/insights/<id>/edit`)
- `insight_edit_save` (`/insights/<id>/edit` POST)
- `insight_compact_card` (`/insights/<id>/compact`)
- `insight_sources` (`/insights/<id>/sources`)
- `pipeline_consolidate` (`/pipeline/consolidate`)
- `jobs_apply` (`/jobs/<id>/apply`)
- `jobs_get` (`/jobs/<id>`)
- `pipeline_badge` (`/pipeline/badge`)

After deletion, the next remaining handler should be `shutdown()` (which Task 1 left intact at the end of `create_app`).

Verify with:

```bash
grep -n "@app\.\(get\|post\)" better_memory/ui/app.py | grep -v "episodes\|reflections\|/healthz\|/$\|/shutdown"
```

Expected: empty output (no remaining old-UI routes).

- [ ] **Step 6: Strip the `from better_memory.ui import jobs` import + `jobs.` references from `app.py`**

```bash
grep -n "from better_memory.ui import jobs\|jobs\." better_memory/ui/app.py
```

Should return ONE remaining line (the `from ... import jobs, queries` import). Replace with just `from better_memory.ui import queries`. All `jobs.*` callsites were inside the deleted route handlers (Step 5).

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: drops by approximately the size of the three deleted test files. The exact final number depends on pytest's per-file collection count — record what comes out and use it as the baseline for Task 3. (Approximate: `~340-360 passed, 141 skipped, 4 deselected`.)

If imports are still broken (e.g. `app.py` references `ConsolidationService` or `InsightService` that aren't yet stripped), expect ImportError. Tasks 4 + 5 will resolve those — for now we accept that the suite may fail to collect on the consolidation/insight test side. **If `tests/ui/` collects cleanly and runs green, Task 2 is done — don't worry about errors in `tests/services/test_consolidation*.py` / `test_insight.py`; Task 4 deletes them.**

Run targeted UI tests to verify:

```bash
uv run pytest tests/ui/ --tb=short -q 2>&1 | tail -5
```

Expected: clean pass on remaining UI tests (test_app, test_episodes, test_queries_episodes, test_queries_reflections, test_reflections, test_entrypoint_integration, test_browser-skipped, conftest).

- [ ] **Step 8: Commit**

```bash
git add -A better_memory/ui/app.py better_memory/ui/jobs.py \
        better_memory/ui/templates/pipeline.html \
        better_memory/ui/templates/fragments/ \
        tests/ui/test_pipeline.py tests/ui/test_apply_job.py \
        tests/ui/test_consolidation_e2e.py tests/ui/test_app.py
git commit -m "Phase 10: drop Pipeline tab + Candidate/Insight CRUD + Consolidation/Jobs flow"
```

(Use `git add -A` to capture the deletions cleanly; verify the staged set with `git status` before committing.)

---

## Task 3: Strip legacy helpers from `better_memory/ui/queries.py` + delete `test_queries.py`

`queries.py` still has the pre-episodic helpers (`kanban_counts`, `list_observations`, `list_candidates`, `list_insights`, `list_promoted`, `list_insight_sources`, `KanbanCounts`, `ObservationListRow`) and an import from `services.insight`. With Task 2 done, none of them have any caller — Task 4 will delete `services/insight.py` itself, so the import must go too.

**Files:**
- Modify: `better_memory/ui/queries.py`
- Delete: `tests/ui/test_queries.py`

- [ ] **Step 1: Delete `tests/ui/test_queries.py`**

```bash
rm tests/ui/test_queries.py
```

- [ ] **Step 2: Strip the legacy helpers + insight import from `queries.py`**

Open `better_memory/ui/queries.py`. Delete:

1. The `from better_memory.services.insight import Insight, row_to_insight` import (currently line 13).
2. `KanbanCounts` dataclass (currently lines 16-21).
3. `kanban_counts` function (currently lines 24-48).
4. `ObservationListRow` dataclass (currently lines 51-58).
5. `list_observations` function (currently lines 61-88).
6. `_list_insights_by_status` function (currently lines 91-103).
7. `list_candidates` function (currently lines 106-111).
8. `list_insights` function (currently lines 114-119).
9. `list_promoted` function (currently lines 122-127).
10. `list_insight_sources` function (currently lines 130-154).

The Phase 8/9 code in `queries.py` (`EpisodeRow`, `episode_list_for_ui`, `EpisodeObservationRow`, `EpisodeReflectionRow`, `EpisodeDetail`, `episode_detail`, `unclosed_episode_count`, `ReflectionListRow`, `_DEFAULT_REFLECTION_STATUSES`, `reflection_list_for_ui`, `ReflectionFull`, `ReflectionSourceObservation`, `ReflectionDetail`, `reflection_detail`) STAYS.

Keep imports: `import sqlite3`, `from dataclasses import dataclass`, `from better_memory.services.episode import Episode, row_to_episode`. The module docstring stays.

After the edit, the file should be roughly 420 lines (down from 580).

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: drops by 10 (size of `test_queries.py`). Use the post-Task-2 baseline as your reference.

- [ ] **Step 4: Commit**

```bash
git add better_memory/ui/queries.py tests/ui/test_queries.py
git commit -m "Phase 10: drop kanban_counts + list_observations/candidates/insights/promoted/sources from ui.queries"
```

---

## Task 4: Delete `services/consolidation.py` + `services/insight.py` + their tests + clean `app.py` imports

The two services have no remaining callers (Tasks 2 + 3 removed every caller). Deleting them along with their test suites + cleaning the `app.py` imports completes the service-layer strip.

**Files:**
- Delete: `better_memory/services/consolidation.py`
- Delete: `better_memory/services/insight.py`
- Delete: `tests/services/test_consolidation.py`
- Delete: `tests/services/test_consolidation_integration.py`
- Delete: `tests/services/test_insight.py`
- Modify: `better_memory/ui/app.py` (remove `ConsolidationService` + `InsightService` imports, the `chat` parameter, `OllamaChat` import, `app.extensions["insight_service"]` and `app.extensions["chat"]` registrations, the `from markupsafe import escape` import iff unused after the consolidation merge handler is gone, the `import threading` if only used by jobs (it's not — used by shutdown))
- Modify: `tests/ui/conftest.py` (remove `FakeChat` injection — `create_app` no longer accepts `chat`)

- [ ] **Step 1: Delete the production files**

```bash
rm better_memory/services/consolidation.py
rm better_memory/services/insight.py
rm tests/services/test_consolidation.py
rm tests/services/test_consolidation_integration.py
rm tests/services/test_insight.py
```

- [ ] **Step 2: Strip imports + extensions from `better_memory/ui/app.py`**

Remove these import lines (currently around lines 17-20):

```python
from better_memory.llm.ollama import ChatCompleter, OllamaChat
from better_memory.services.consolidation import ConsolidationService
...
from better_memory.services.insight import InsightService
```

Verify `escape` and `markupsafe` imports — `escape` is still used by Phase 8 + Phase 9 error-card handlers (`/episodes/<id>/close`, `/reflections/<id>/confirm/retire/edit`). KEEP that import.

Remove the `chat` parameter from `create_app`:

```python
def create_app(
    *,
    inactivity_timeout: float = 1800.0,
    inactivity_poll_interval: float = 30.0,
    start_watchdog: bool = True,
    db_path: Path | None = None,
    chat: ChatCompleter | None = None,   # ← DELETE this line
) -> Flask:
```

Remove the chat resolution and storage (currently around lines 59-60):

```python
resolved_chat: ChatCompleter = chat if chat is not None else OllamaChat()
app.extensions["chat"] = resolved_chat
```

Remove the insight_service registration (currently around line 58):

```python
app.extensions["insight_service"] = InsightService(conn=db_conn)
```

After this edit, the only `app.extensions[...]` registrations remaining should be `db_connection`, `_db_path`, `episode_service`, `reflection_service`.

- [ ] **Step 3: Update `tests/ui/conftest.py`**

Remove the `FakeChat` injection. The `client` fixture body becomes:

```python
@pytest.fixture
def client(tmp_db: Path) -> Iterator[FlaskClient]:
    """Yield a Flask test client backed by a migrated tmp DB.

    Patches ``threading.Timer`` for the lifetime of the fixture so
    ``TestOriginCheck`` POST-to-/shutdown tests don't fire the real
    100 ms timer that calls ``os._exit`` and kills the pytest process.
    """
    app = create_app(start_watchdog=False, db_path=tmp_db)
    app.config["TESTING"] = True
    with patch("better_memory.ui.app.threading.Timer"):
        with app.test_client() as c:
            yield c
```

Also delete the now-unused import:

```python
from better_memory.llm.fake import FakeChat
```

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: drops further by the size of the three service test files (~30-50 tests gone). Final approximate count: `~290-310 passed, 141 skipped, 4 deselected`.

- [ ] **Step 5: Commit**

```bash
git add -A better_memory/services/consolidation.py better_memory/services/insight.py \
        tests/services/test_consolidation.py tests/services/test_consolidation_integration.py \
        tests/services/test_insight.py \
        better_memory/ui/app.py tests/ui/conftest.py
git commit -m "Phase 10: drop ConsolidationService + InsightService + chat/jobs wiring"
```

---

## Task 5: Final cleanup — remove the orphan browser test + verify `__main__` entrypoint

`tests/ui/test_browser.py` is module-level skipped with the reason "Awaiting Phase 2 episodic service layer" — that comment is stale, and the test exercises a candidate-card behaviour that no longer exists. Delete the file (Phase 12 will introduce new browser tests targeting Episodes + Reflections).

Also verify `better_memory/ui/__main__.py` doesn't reference any deleted modules.

**Files:**
- Delete: `tests/ui/test_browser.py`
- Modify (only if necessary): `better_memory/ui/__main__.py`
- Modify (only if necessary): `tests/ui/test_entrypoint_integration.py`

- [ ] **Step 1: Delete the browser test**

```bash
rm tests/ui/test_browser.py
```

- [ ] **Step 2: Verify `__main__.py` is clean**

```bash
grep -n "ConsolidationService\|InsightService\|jobs\|FakeChat\|ChatCompleter" better_memory/ui/__main__.py
```

Expected: no matches. If any do match, remove them — but they shouldn't, since `__main__.py` is just the `python -m better_memory.ui` entrypoint that calls `create_app()` and `app.run()`.

- [ ] **Step 3: Verify `tests/ui/test_entrypoint_integration.py` is clean**

```bash
grep -n "ConsolidationService\|InsightService\|jobs\|FakeChat\|ChatCompleter" tests/ui/test_entrypoint_integration.py
```

If any match, edit to remove the references. The entrypoint test should not depend on any deleted module — it just spawns the subprocess, hits `/healthz`, checks the URL file is written.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest --tb=no -q 2>&1 | tail -3
```

Expected: stays at the post-Task-4 count (test_browser was already module-level skipped, so deleting it doesn't change the passed count — but the skipped count drops by 1).

- [ ] **Step 5: Verify the running app still boots**

```bash
BETTER_MEMORY_HOME=$(mktemp -d) uv run python -m better_memory.ui &
SERVER_PID=$!
sleep 2
curl -s http://localhost:$(cat $BETTER_MEMORY_HOME/ui.url | sed 's|.*:||')/healthz
kill $SERVER_PID
```

(On Windows in bash: this may need `BETTER_MEMORY_HOME=/tmp/bmtest$$` instead of `mktemp -d`. The check is "does the app start without ImportError and respond to /healthz". A simpler verification is to run pytest tests/ui/test_entrypoint_integration.py which exercises the same path.)

Alternative simpler check:

```bash
uv run python -c "from better_memory.ui.app import create_app; create_app(start_watchdog=False); print('OK')"
```

Expected: `OK` — confirms imports + factory wiring is clean.

- [ ] **Step 6: Commit**

```bash
git add -A tests/ui/test_browser.py
git commit -m "Phase 10: drop pre-episodic browser test (Phase 12 will replace)"
```

---

## Final review

After all tasks complete, dispatch a final code-review subagent across the full Phase 10 diff. Confirm:

- All tests pass: `uv run pytest --tb=no -q 2>&1 | tail -3` — final count is the post-Task-5 number, no failures, no errors.
- `grep -rn "ConsolidationService\|InsightService\|kanban_counts\|list_candidates\|list_insights\|list_promoted\|jobs\.start_consolidation_job" better_memory/` returns NOTHING.
- `grep -rn "ConsolidationService\|InsightService\|kanban_counts\|list_candidates\|list_insights\|list_promoted" tests/` returns NOTHING.
- `ls better_memory/ui/templates/` shows only `base.html`, `episodes.html`, `reflections.html`, `fragments/`.
- `ls better_memory/ui/templates/fragments/` shows only `episode_*.html`, `panel_episodes.html`, `panel_reflections.html`, `reflection_*.html`.
- `ls better_memory/services/` shows no `consolidation.py` or `insight.py`.
- The two-tab nav in `base.html` is unchanged (Phase 8's swap stays).
- Spec §8 + §14 are honoured: old surfaces are gone, two tabs remain, Knowledge editor noted as deferred.

Then run `superpowers:finishing-a-development-branch` to push + open the PR.
