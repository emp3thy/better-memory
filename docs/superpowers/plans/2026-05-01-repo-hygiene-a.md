# Repo hygiene (sub-design A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear four pieces of repo debt in a single PR — land an uncommitted +574-LOC bugfix diff, rename `audit_log` dataclass field to match the column, surgically refresh the four MCP skills, and archive two superseded plan documents.

**Architecture:** Implementation lands on the existing `uifix` branch (which already has commit `78f5c38` "Add project filter and project chip to observations page" — that commit also bundled half of the WIP `app.py` changes, so completing the rest of the WIP here is what makes uifix actually work). Four cleanup commits in order: (1) complete the WIP landing (the 8 files NOT already on uifix), (2) refactor commit renaming `ObservationAuditEntry.at` → `created_at`, (3) docs commit updating all four skills, (4) docs commit archiving the dead Pipeline / Consolidation plans. Commits are independent — sequencing is for review clarity. The final PR will contain `78f5c38` plus these 4 commits plus the spec/plan commit (6 total).

**Tech Stack:** Python 3.12, sqlite3, Flask, Jinja2, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-05-01-repo-hygiene-a-design.md`

---

## File Structure

| File | Commit | Change |
|---|---|---|
| `better_memory/services/reflection.py` | 1 | Already modified (WIP): adds `_auto_ignore_unused`, `last_run_counts` |
| `better_memory/services/spool.py` | 1 | Already modified (WIP): adds `_maybe_close_episode_for_session_end` |
| `better_memory/ui/app.py` | 1 | Already modified (WIP): wires `last_run_counts` into synth banner |
| `better_memory/ui/templates/fragments/observations_synth_banner.html` | 1 | Already modified (WIP): renders new run-count fields |
| `tests/conftest.py` | 1 | Already modified (WIP): shared fixtures for new tests |
| `tests/services/test_reflection.py` | 1 | Already modified (WIP): tests `_auto_ignore_unused` |
| `tests/services/test_spool.py` | 1 | Already modified (WIP): tests `_maybe_close_episode_for_session_end` |
| `docs/hooks-setup.md` | 1 | Already modified (WIP): doc edit aligned with hook change |
| `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` | 1 | Already modified (WIP): spec text aligned with code |
| `better_memory/ui/queries.py` | 2 | Rename `ObservationAuditEntry.at` → `created_at`; drop `AS at` |
| `better_memory/ui/templates/fragments/observation_drawer.html` | 2 | `entry.at` → `entry.created_at` |
| `tests/ui/test_queries_observations.py` | 2 | Update `e.at` → `e.created_at`; add field-name regression test |
| `better_memory/skills/memory-write.md` | 3 | Add "Working with episodes" + "Lifecycle is automatic" sections |
| `better_memory/skills/memory-retrieve.md` | 3 | Add "Drilling into raw observations" section |
| `better_memory/skills/memory-feedback.md` | 3 | Add "Hardening: the strongest signal" section |
| `better_memory/skills/session-close.md` | 3 | Add "Episodes close themselves (mostly)" section |
| `docs/superpowers/plans/2026-04-19-ui-phase-2-pipeline-kanban.md` | 4 | Move to `docs/superpowers/archive/`; prepend SUPERSEDED banner |
| `docs/superpowers/plans/2026-04-19-phase-3-consolidation.md` | 4 | Move to `docs/superpowers/archive/`; prepend SUPERSEDED banner |

---

## Confidence summary

Per memory `dde30588` (preference: confidence-scoring on every implementation plan; embed mitigation steps inside the task body for anything below 90%).

| Task | Confidence | Notes |
|---|---|---|
| 1. Pre-flight verification | 95% | Pure read-only checks. |
| 2. Land WIP | 95% | HEREDOC commit — see plan-wide note below. |
| 3. Audit-log rename | 92% | Line numbers verified against current file at plan-write time. Frozen-dataclass `hasattr` semantics confirmed. |
| 4. memory-write.md edit | 92% | Concrete `old_string` / `new_string` for Edit tool. |
| 5. memory-retrieve.md edit | 92% | Same shape. |
| 6. memory-feedback.md edit | 92% | Same shape. |
| 7. session-close.md edit | 92% | Same shape. |
| 8. Commit skills update | 92% | HEREDOC commit — see plan-wide note below. |
| 9. Archive dead plans | 92% | Originally 75%. Mitigations now embedded in Steps 1, 3, 4: `mkdir` made shell-portable; archived files' first headings captured exactly so Edit calls have concrete `old_string`. |
| 10. Final verification + push | 92% | HEREDOC commit + PR body — see plan-wide note below. |

**Plan-wide note (HEREDOC).** Every `git commit -m "$(cat <<'EOF' ... EOF)"` and the `gh pr create --body "$(cat <<'EOF' ... EOF)"` block in this plan **must be executed via the Bash tool**, not PowerShell. PowerShell's here-string syntax is `@'...'@` and is incompatible with Bash heredocs. The Bash tool is available on Windows per the environment header.

---

## Task 1: Pre-flight verification

**Files:** none (read-only checks)

- [ ] **Step 1: Confirm we're on `main`, working tree contains the WIP**

```bash
git status --porcelain
```

Expected output (order may vary):

```
 M better_memory/services/reflection.py
 M better_memory/services/spool.py
 M better_memory/ui/templates/fragments/observations_synth_banner.html
 M docs/hooks-setup.md
 M docs/superpowers/specs/2026-04-20-episodic-memory-design.md
 M tests/conftest.py
 M tests/services/test_reflection.py
 M tests/services/test_spool.py
?? docs/superpowers/plans/2026-05-01-repo-hygiene-a.md
?? docs/superpowers/specs/2026-05-01-repo-hygiene-a-design.md
```

8 modified files (not 9). `better_memory/ui/app.py` is intentionally absent — that part of the WIP was already bundled into commit `78f5c38` on `uifix`. If the modified set differs in any other way, STOP and surface to the user.

- [ ] **Step 2: Run the full test suite against the WIP**

```bash
uv run pytest -q
```

Expected: all tests pass (or only the pre-known Ollama integration skip). Recent baseline was ~500 passed, ~22 skipped.

If anything fails, STOP and report — Commit 1 cannot land until the WIP's own tests are green.

- [ ] **Step 3: Run ruff to confirm lint is clean**

```bash
uv run ruff check .
```

Expected: zero findings. If lint fails, fix the WIP before proceeding.

- [ ] **Step 4: Switch to the existing `uifix` branch**

```bash
git checkout uifix
```

`uifix` already exists locally and on `origin` and contains commit `78f5c38`. Uncommitted modifications and the untracked spec/plan files travel with the branch switch — uifix's HEAD matches main's HEAD for every file we have local mods to (reflection.py, spool.py, banner template, conftest, two tests, two docs), so the switch is conflict-free.

After switching, re-run `git status --porcelain` and confirm the same 8 modified + 2 untracked set.

---

## Task 2: Commit 1 — Land the WIP

**Files:** all 9 files already modified in the working tree (see Pre-flight Step 1).

- [ ] **Step 1: Stage the WIP files explicitly (avoid `git add -A`)**

```bash
git add \
  better_memory/services/reflection.py \
  better_memory/services/spool.py \
  better_memory/ui/templates/fragments/observations_synth_banner.html \
  tests/conftest.py \
  tests/services/test_reflection.py \
  tests/services/test_spool.py \
  docs/hooks-setup.md \
  docs/superpowers/specs/2026-04-20-episodic-memory-design.md
```

`better_memory/ui/app.py` is NOT in this list — its `last_run_counts` wiring already shipped in commit `78f5c38` on `uifix`. Without the rest of the WIP landing, `app.py` references a property that doesn't exist on `ReflectionSynthesisService` yet — this commit is what completes that.

- [ ] **Step 2: Verify the staged diff matches expectation**

```bash
git diff --cached --stat
```

Expected: 8 files, roughly +560/-30 LOC (numbers approximate; flag if wildly off).

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(synthesis): auto-ignore unused observations; auto-close background episodes on session_end

- ReflectionSynthesisService._auto_ignore_unused marks LLM-ignored
  observations as consumed_without_reflection so they don't strand
  in the active pool after the watermark advances.
- SpoolService._maybe_close_episode_for_session_end closes
  unhardened episodes (goal=NULL) on Stop hook; hardened episodes
  stay open per episodic-memory-design.md §3.
- last_run_counts surfaced in the synthesis banner.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify commit landed cleanly**

```bash
git log --oneline -1
git status --porcelain
```

Expected: one new commit, working tree clean except for the two `2026-05-01-...` files in `docs/superpowers/`.

---

## Task 3: Commit 2 — Audit-log column rename

**Files:**
- Modify: `better_memory/ui/queries.py:515-521,560-575`
- Modify: `better_memory/ui/templates/fragments/observation_drawer.html:46`
- Modify: `tests/ui/test_queries_observations.py:223`
- Test: same file (regression test added in Step 1)

- [ ] **Step 1: Add a failing regression test asserting the new field name**

In `tests/ui/test_queries_observations.py`, find the `class TestObservationDetail:` block (or whichever class contains `test_returns_audit_timeline_newest_first`). Add this test method **before** `test_returns_audit_timeline_newest_first`:

```python
    def test_audit_entry_field_is_created_at(self, conn):
        """Regression: ObservationAuditEntry exposes the audit_log row
        timestamp as `created_at`, matching the column name. Avoids the
        `created_at AS at` translation tax that caused two prior bugs
        (memories 54446ae9, 87d40804)."""
        from better_memory.ui.queries import observation_detail

        _seed_episode(conn)
        _seed_obs(conn, oid="o-1")
        conn.execute(
            "INSERT INTO audit_log "
            "(id, entity_type, entity_id, action, actor, created_at) "
            "VALUES ('a-1', 'observation', 'o-1', 'create', 'ai', "
            "'2026-04-26T10:00:00+00:00')"
        )
        conn.commit()

        detail = observation_detail(conn, observation_id="o-1")
        assert detail is not None
        assert len(detail.audit) == 1
        entry = detail.audit[0]
        assert entry.created_at == "2026-04-26T10:00:00+00:00"
        assert not hasattr(entry, "at")
```

- [ ] **Step 2: Run the new test — expect it to fail**

```bash
uv run pytest tests/ui/test_queries_observations.py::TestObservationDetail::test_audit_entry_field_is_created_at -v
```

Expected: FAIL — `AttributeError: 'ObservationAuditEntry' object has no attribute 'created_at'` (and the `not hasattr(entry, "at")` assertion would also fail since `at` still exists). Either failure mode confirms the test is exercising the right thing.

- [ ] **Step 3: Rename the dataclass field**

Edit `better_memory/ui/queries.py` lines 515-521 from:

```python
@dataclass(frozen=True)
class ObservationAuditEntry:
    at: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
```

to:

```python
@dataclass(frozen=True)
class ObservationAuditEntry:
    created_at: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
```

- [ ] **Step 4: Drop the `AS at` alias from the SQL**

Edit `better_memory/ui/queries.py` line 561 from:

```python
        "SELECT created_at AS at, actor, action, from_status, to_status "
```

to:

```python
        "SELECT created_at, actor, action, from_status, to_status "
```

- [ ] **Step 5: Update the constructor call**

Edit `better_memory/ui/queries.py` lines 568-575 from:

```python
    audit = [
        ObservationAuditEntry(
            at=r["at"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
        )
        for r in audit_rows
    ]
```

to:

```python
    audit = [
        ObservationAuditEntry(
            created_at=r["created_at"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
        )
        for r in audit_rows
    ]
```

- [ ] **Step 6: Update the Jinja template**

Edit `better_memory/ui/templates/fragments/observation_drawer.html` line 46 from:

```html
            <span class="at">{{ entry.at }}</span>
```

to:

```html
            <span class="at">{{ entry.created_at }}</span>
```

(The CSS class name `at` is unchanged — no callers depend on it; renaming the class is a separate concern out of scope for this commit.)

- [ ] **Step 7: Update the existing test that reads `e.at`**

Edit `tests/ui/test_queries_observations.py` line 223 from:

```python
        ats = [e.at for e in detail.audit]
        assert ats == sorted(ats, reverse=True)
```

to:

```python
        timestamps = [e.created_at for e in detail.audit]
        assert timestamps == sorted(timestamps, reverse=True)
```

- [ ] **Step 8: Verify no callsites slipped through**

```bash
grep -rE 'entry\.at\b|AS at\b|ObservationAuditEntry\(.*\bat=' better_memory tests
```

Expected: zero results. If anything matches, fix it before continuing.

- [ ] **Step 9: Run the new regression test — expect green**

```bash
uv run pytest tests/ui/test_queries_observations.py::TestObservationDetail::test_audit_entry_field_is_created_at -v
```

Expected: PASS.

- [ ] **Step 10: Run the full test suite — expect green**

```bash
uv run pytest -q
```

Expected: same pass/skip count as Pre-flight Step 2.

- [ ] **Step 11: Run ruff**

```bash
uv run ruff check .
```

Expected: zero findings.

- [ ] **Step 12: Commit**

```bash
git add \
  better_memory/ui/queries.py \
  better_memory/ui/templates/fragments/observation_drawer.html \
  tests/ui/test_queries_observations.py
git commit -m "$(cat <<'EOF'
refactor(ui): rename ObservationAuditEntry.at to created_at

Aligns the dataclass field with the audit_log column name. Removes
the `created_at AS at` SELECT alias that caused two prior bugs
(memories 54446ae9, 87d40804) when plans referenced `at` and
implementations used `created_at` (or vice versa).

Adds a regression test asserting the field name.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Skills update — `memory-write.md`

**Files:**
- Modify: `better_memory/skills/memory-write.md`

- [ ] **Step 1: Insert the "Working with episodes" section after the existing "## Mandatory fields" section, BEFORE "## The evidence-in-hand rule"**

Use the Edit tool. Find the exact text:

```markdown
This matches the reinforcement loop's design: `record_use(outcome)` is what moves `reinforcement_score`, so the outcome you stamp there is what future retrievals rank on.

## When to pick each outcome
```

Replace with:

```markdown
This matches the reinforcement loop's design: `record_use(outcome)` is what moves `reinforcement_score`, so the outcome you stamp there is what future retrievals rank on.

## Working with episodes

`memory.observe` is the per-decision-point write. When focused work has a clear goal you want tracked end-to-end, also bracket it with an episode:

```python
result = memory.start_episode(
    goal="Extract the async-bridge helper from ui/app.py",
)
# result is {"episode_id": ..., "reflections": {...}}
# ... memory.observe() calls during the work ...
memory.close_episode(
    outcome="success",   # or "abandoned" / "partial" — see hardening
    summary="One sentence on what was done.",
)
```

**Hardening.** Closing an episode with a *real* outcome (`success`, `partial`, or `abandoned` — NOT `no_outcome`) is a stronger reinforcement signal than `record_use` alone. Every observation made during the episode inherits the outcome at synthesis time.

**Background episodes.** Sessions without an explicit `start_episode` get a background episode (no goal) auto-opened by the session-start hook and auto-closed on session-end. You only need `start_episode` / `close_episode` yourself for goal-driven work.

**Lifecycle is automatic.** Observation `status` flips through `active` → `consumed_into_reflection` (cited as a reflection source) or `consumed_without_reflection` (seen by synthesis but not cited). You don't set these — synthesis does. Just `memory.observe()` and trust the pipeline.

## When to pick each outcome
```

(The triple-backtick fence inside the inserted section needs careful escaping — when you paste this into Edit, ensure the inner ``` python ... ``` block ends BEFORE the `**Hardening.**` paragraph, not at the end of the whole insertion.)

- [ ] **Step 2: Verify the file still parses as valid markdown by reading it**

```bash
uv run python -c "
import pathlib
content = pathlib.Path('better_memory/skills/memory-write.md').read_text()
assert '## Working with episodes' in content
assert '## When to pick each outcome' in content
assert content.index('## Working with episodes') < content.index('## When to pick each outcome')
print('OK')
"
```

Expected output: `OK`.

---

## Task 5: Skills update — `memory-retrieve.md`

**Files:**
- Modify: `better_memory/skills/memory-retrieve.md`

- [ ] **Step 1: Insert the "Drilling into raw observations" section before "## Window guidance"**

Find:

```markdown
If a `dont` memory exactly matches what you were about to do, treat that as a hard stop. Look for an alternative or ask the user.

## Window guidance
```

Replace with:

```markdown
If a `dont` memory exactly matches what you were about to do, treat that as a hard stop. Look for an alternative or ask the user.

## Drilling into raw observations

`memory.retrieve` returns *distilled reflections* — the reinforcement-weighted lessons synthesis has built from observations. That's almost always what you want.

When reflections aren't specific enough — typically when investigating a specific incident or hunting for an exact prior decision — drop down to raw observations:

```python
result = memory.retrieve_observations(
    query="async bridge ollama transport error",
    component="ui",
    limit=10,
)
```

Hits are ranked by hybrid FTS5 + sqlite-vec relevance. Use this for incident triage and root-cause hunts; use `memory.retrieve` for "should I do X?"

## Window guidance
```

- [ ] **Step 2: Verify by reading**

```bash
uv run python -c "
import pathlib
content = pathlib.Path('better_memory/skills/memory-retrieve.md').read_text()
assert '## Drilling into raw observations' in content
assert 'memory.retrieve_observations' in content
print('OK')
"
```

Expected: `OK`.

---

## Task 6: Skills update — `memory-feedback.md`

**Files:**
- Modify: `better_memory/skills/memory-feedback.md`

- [ ] **Step 1: Insert the "Hardening: the strongest signal" section before "## Pattern 1 — closing your own neutral observe"**

Find:

```markdown
Recording a `failure` against a stale memory is how you retire bad advice over time.

## Pattern 1 — closing your own neutral observe
```

Replace with:

```markdown
Recording a `failure` against a stale memory is how you retire bad advice over time.

## Hardening: the strongest signal

`memory.close_episode(outcome='success' | 'partial' | 'abandoned')` is a stronger reinforcement signal than per-observation `record_use`. Every observation made during the episode inherits the outcome at synthesis time.

If the work was opened with `memory.start_episode(...)` and the goal is now resolved, prefer hardening — call `close_episode` with a real outcome. Per-observation `record_use` is still right when you're closing the loop on a single decision (e.g. validating one retrieved memory you applied), but for goal-driven work, hardening is the higher-leverage move.

## Pattern 1 — closing your own neutral observe
```

- [ ] **Step 2: Verify by reading**

```bash
uv run python -c "
import pathlib
content = pathlib.Path('better_memory/skills/memory-feedback.md').read_text()
assert '## Hardening: the strongest signal' in content
assert 'close_episode' in content
print('OK')
"
```

Expected: `OK`.

---

## Task 7: Skills update — `session-close.md`

**Files:**
- Modify: `better_memory/skills/session-close.md`

- [ ] **Step 1: Insert the "Episodes close themselves (mostly)" section before "## Anti-patterns"**

Find:

```markdown
If an `observe(outcome='neutral')` has no `record_use` yet because the work genuinely isn't validated — tests still running, feature not yet shipped, user hasn't confirmed — leave it. The next session's session-close or memory-feedback skill will pick it up.

## Anti-patterns
```

Replace with:

```markdown
If an `observe(outcome='neutral')` has no `record_use` yet because the work genuinely isn't validated — tests still running, feature not yet shipped, user hasn't confirmed — leave it. The next session's session-close or memory-feedback skill will pick it up.

## Episodes close themselves (mostly)

If the session ran without an explicit `memory.start_episode`, a *background* episode (no goal) was auto-opened by the session-start hook. The Stop hook writes a `session_end` marker which the next `SpoolService.drain` (typically the next `memory.retrieve` call) consumes via `_maybe_close_episode_for_session_end`, closing the episode as `outcome='no_outcome'`. Nothing to do.

Hardened episodes — those you opened with `start_episode` *and a goal* — stay open across sessions deliberately, so the next session's reconcile prompt can resolve them with a real outcome. If the goal is genuinely complete now, call `memory.close_episode(outcome=...)` before wrapping up. That hardens the episode and reinforces every observation inside it.

## Anti-patterns
```

- [ ] **Step 2: Verify by reading**

```bash
uv run python -c "
import pathlib
content = pathlib.Path('better_memory/skills/session-close.md').read_text()
assert '## Episodes close themselves (mostly)' in content
assert '_maybe_close_episode_for_session_end' in content
print('OK')
"
```

Expected: `OK`.

---

## Task 8: Commit 3 — Skills update

**Files:** four skill files modified in Tasks 4–7.

- [ ] **Step 1: Stage the four skill files**

```bash
git add \
  better_memory/skills/memory-write.md \
  better_memory/skills/memory-retrieve.md \
  better_memory/skills/memory-feedback.md \
  better_memory/skills/session-close.md
```

- [ ] **Step 2: Review the staged diff**

```bash
git diff --cached --stat
git diff --cached better_memory/skills/
```

Expected: four files modified, additions only (no deletions). Read the diff to confirm the new sections appear in the right places and the existing content is untouched.

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
docs(skills): align MCP skills with episodic memory surface

The four skills (memory-write, memory-retrieve, memory-feedback,
session-close) predated the episodic redesign and never mentioned
episodes, hardening, or the consumed_* observation statuses. This
commit adds those concepts surgically without restructuring the
existing four-skill shape:

- memory-write.md: new "Working with episodes" + lifecycle notes
- memory-retrieve.md: new "Drilling into raw observations" section
  for memory.retrieve_observations as the drill-down
- memory-feedback.md: new "Hardening" section explaining that
  close_episode with a real outcome is a stronger signal than
  per-observation record_use
- session-close.md: new "Episodes close themselves (mostly)"
  section documenting background-episode auto-close behavior

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Commit 4 — Archive dead plans

**Files:**
- Move: `docs/superpowers/plans/2026-04-19-ui-phase-2-pipeline-kanban.md` → `docs/superpowers/archive/2026-04-19-ui-phase-2-pipeline-kanban.md`
- Move: `docs/superpowers/plans/2026-04-19-phase-3-consolidation.md` → `docs/superpowers/archive/2026-04-19-phase-3-consolidation.md`
- Modify: both moved files (prepend SUPERSEDED banner)

- [ ] **Step 1: Create the archive directory and move both files**

```bash
mkdir docs/superpowers/archive
git mv docs/superpowers/plans/2026-04-19-ui-phase-2-pipeline-kanban.md docs/superpowers/archive/
git mv docs/superpowers/plans/2026-04-19-phase-3-consolidation.md docs/superpowers/archive/
```

(Drop the `-p` from `mkdir` — it's not portable to PowerShell. The parent `docs/superpowers/` already exists, so plain `mkdir` works on every shell.)

- [ ] **Step 2: Verify the moves staged correctly**

```bash
git status
```

Expected: both files shown as `renamed:` from `docs/superpowers/plans/...` to `docs/superpowers/archive/...`.

- [ ] **Step 3: Prepend the SUPERSEDED banner to the Pipeline Kanban file**

Use Edit on `docs/superpowers/archive/2026-04-19-ui-phase-2-pipeline-kanban.md`. The first line was captured at plan-write time, so the Edit's `old_string` is concrete:

`old_string`:

```markdown
# Management UI — Phase 2: Pipeline Kanban Implementation Plan
```

`new_string`:

```markdown
> **SUPERSEDED.** This plan describes the Pipeline / Consolidation / Insight
> architecture, replaced by the episodic memory redesign. See
> `docs/superpowers/specs/2026-04-20-episodic-memory-design.md`. Kept for
> design-decision history; not implementable against the current codebase.

# Management UI — Phase 2: Pipeline Kanban Implementation Plan
```

If Edit fails because the heading no longer matches: Read lines 1–5 of the file, capture the actual `# ...` line, then re-run Edit using that captured line as `old_string`.

- [ ] **Step 4: Prepend the same banner to the Consolidation file**

Use Edit on `docs/superpowers/archive/2026-04-19-phase-3-consolidation.md`. Heading captured at plan-write time:

`old_string`:

```markdown
# ConsolidationService — Phase 3 Implementation Plan
```

`new_string`:

```markdown
> **SUPERSEDED.** This plan describes the Pipeline / Consolidation / Insight
> architecture, replaced by the episodic memory redesign. See
> `docs/superpowers/specs/2026-04-20-episodic-memory-design.md`. Kept for
> design-decision history; not implementable against the current codebase.

# ConsolidationService — Phase 3 Implementation Plan
```

If Edit fails because the heading no longer matches: Read lines 1–5 of the file, capture the actual `# ...` line, then re-run Edit using that captured line as `old_string`.

- [ ] **Step 5: Find any cross-references that point at the moved files**

```bash
grep -rE 'ui-phase-2-pipeline-kanban|phase-3-consolidation' docs/ README.md 2>/dev/null
```

Expected matches: only the moved files themselves (which now live under `archive/`) and possibly cross-references in other docs.

For each non-archive match: either update the link to point at `docs/superpowers/archive/...` or remove the link if it no longer makes sense in context. Show each edit individually — do not make changes blindly.

If there are zero matches outside the archive itself, no cross-reference fixup is needed.

- [ ] **Step 6: Stage the banners + any cross-reference fixes**

```bash
git add docs/superpowers/archive/
# plus any docs files modified in Step 5
```

- [ ] **Step 7: Verify the staged diff**

```bash
git diff --cached --stat
```

Expected: two renames + two banner prepends. If there are cross-reference fixups, they appear here too.

- [ ] **Step 8: Commit**

```bash
git commit -m "$(cat <<'EOF'
docs(plans): archive superseded Pipeline / Consolidation plans

The Phase-2 Pipeline Kanban plan and the Phase-3 ConsolidationService
plan describe an architecture that the episodic memory redesign
(2026-04-20-episodic-memory-design.md) replaced. There is no
consolidation.py or insight.py; the Pipeline tab is gone; the
referenced routes don't exist.

Moved both files to docs/superpowers/archive/ with a SUPERSEDED
banner so future readers find them via search but immediately know
they're not implementable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Final verification

**Files:** none (read-only checks)

- [ ] **Step 1: Confirm working tree is clean except for the spec/plan files**

```bash
git status --porcelain
```

Expected: only `docs/superpowers/specs/2026-05-01-repo-hygiene-a-design.md` and `docs/superpowers/plans/2026-05-01-repo-hygiene-a.md` remain untracked (these get committed at the end of the PR if the user wants them in the same branch).

- [ ] **Step 2: Confirm five commits land on the branch (4 new + the existing 78f5c38)**

```bash
git log --oneline main..HEAD
```

Expected: exactly five commits in this order (newest first):

```
<sha> docs(plans): archive superseded Pipeline / Consolidation plans
<sha> docs(skills): align MCP skills with episodic memory surface
<sha> refactor(ui): rename ObservationAuditEntry.at to created_at
<sha> feat(synthesis): auto-ignore unused observations; auto-close background episodes on session_end
78f5c38 Add project filter and project chip to observations page
```

- [ ] **Step 3: Run the full test suite one more time**

```bash
uv run pytest -q
```

Expected: same pass/skip count as Pre-flight Step 2. If different, investigate which commit broke it (`git bisect` or `git log -p`).

- [ ] **Step 4: Run ruff one more time**

```bash
uv run ruff check .
```

Expected: zero findings.

- [ ] **Step 5: Stage and commit the spec + plan**

```bash
git add docs/superpowers/specs/2026-05-01-repo-hygiene-a-design.md \
        docs/superpowers/plans/2026-05-01-repo-hygiene-a.md
git commit -m "$(cat <<'EOF'
docs(superpowers): spec + plan for repo-hygiene-a

Captures the design and implementation plan for sub-design A of the
tech-debt audit (WIP landing, audit-log rename, skills refresh, dead
plans archive).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Push and open PR (only if the user has confirmed)**

PAUSE HERE. Do NOT push or open a PR without explicit user confirmation. Report the branch state and ask:

> "Branch `uifix` ready with 6 commits ahead of main (78f5c38 project-filter + 4 cleanup + spec/plan). Want me to push and open a PR?"

If the user confirms, then:

```bash
git push -u origin uifix
gh pr create --title "uifix: project filter + complete WIP synthesis fix + repo hygiene" --body "$(cat <<'EOF'
## Summary

This PR bundles three pieces of work that landed on `uifix`:

- **feat(ui) [78f5c38]:** project filter + project chip on the observations page; `observation_list_for_ui.project` now optional.
- **feat(synthesis):** completes the WIP that 78f5c38 partially bundled — adds `ReflectionSynthesisService.last_run_counts` + `_auto_ignore_unused`, `SpoolService._maybe_close_episode_for_session_end`, and the banner template that consumes `run_counts`. Without these, 78f5c38's `app.py` calls `svc.last_run_counts` which doesn't exist on the service yet.
- **refactor(ui):** renames `ObservationAuditEntry.at` → `created_at` to match the `audit_log` column. Removes the `created_at AS at` translation tax that bit us twice (memories `54446ae9`, `87d40804`).
- **docs(skills):** surgical update of the four MCP skills to cover episodes, hardening, and the `consumed_*` observation statuses introduced by the episodic redesign.
- **docs(plans):** archives `2026-04-19-ui-phase-2-pipeline-kanban.md` and `2026-04-19-phase-3-consolidation.md` — both describe an architecture (Pipeline / ConsolidationService / InsightService) the episodic redesign replaced. Moved to `docs/superpowers/archive/` with a SUPERSEDED banner.

Spec: `docs/superpowers/specs/2026-05-01-repo-hygiene-a-design.md`
Plan: `docs/superpowers/plans/2026-05-01-repo-hygiene-a.md`

## Test plan

- [x] `uv run pytest -q` — same pass/skip count as `main`
- [x] `uv run ruff check .` — clean
- [x] Manual smoke: `POST /observations/synthesize` shows new run-count fields in the banner
- [ ] Manual smoke: open the observations page; switch the project dropdown between "all" and a specific project; confirm filtering and the project chip render
- [ ] Manual smoke: open an observation drawer; audit timeline timestamps still render

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Memory sweep before declaring done**

Per CLAUDE.md mandatory triggers ("at the end of each phase / PR cycle"), pause and review what was learned. Skip if nothing in this work was non-obvious.

Candidates to consider for `memory.observe`:
- Was anything in the audit-log rename surprising? (Probably not — mechanical.)
- Did Pre-flight reveal that the WIP wasn't actually green? (Worth recording if so.)
- Did any cross-reference fixup in Task 9 turn up unexpected places linking to the old plans? (Worth recording the location pattern.)

If nothing rose to the bar, no observation is needed — quality over quota.

---

## Self-Review

Spec coverage check (against `2026-05-01-repo-hygiene-a-design.md`):

| Spec section | Plan task |
|---|---|
| Commit 1 — Land WIP | Task 2 |
| Commit 2 — Audit-log rename | Task 3 |
| Commit 3 — Skills surgical update (memory-write episodes section) | Task 4 |
| Commit 3 — Skills surgical update (memory-retrieve drill-down) | Task 5 |
| Commit 3 — Skills surgical update (memory-feedback hardening) | Task 6 |
| Commit 3 — Skills surgical update (session-close auto-close) | Task 7 |
| Commit 3 — single commit | Task 8 |
| Commit 4 — Archive dead plans | Task 9 |
| Risk: tests must be green pre-WIP | Task 1 (pre-flight) |
| Risk: rename grep-verification | Task 3 Step 8 |
| Risk: cross-reference fixup | Task 9 Step 5 |
| Decision: rename direction (field, not schema) | Task 3 (mechanical edits match) |
| Decision: archive (not delete) with banner | Task 9 (matches) |
| Decision: surgical (not restructure) skills | Tasks 4–7 (additions only, structure preserved) |

No spec gaps.

Placeholder scan: no TBDs, no "TODO", no "implement later", no "similar to Task N." Every step has either exact code/text or an exact command with expected output.

Type-consistency check: the `ObservationAuditEntry` rename is consistent across Step 3 (definition), Step 5 (constructor call), Step 6 (template), Step 7 (test). The SQL in Step 4 returns `created_at` (no alias), and Step 5 reads `r["created_at"]`. Consistent.

The plan is ready to execute.
