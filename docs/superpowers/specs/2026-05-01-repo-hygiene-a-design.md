# Repo hygiene (sub-design A) — design

**Status:** Approved 2026-05-01
**Branch target:** `repo-hygiene-a` off `main`
**Successor sub-designs:** B (synthesis route hardening), C (background lifecycle), D (type & identity infra) — out of scope here.

## Goal

Clear four pieces of accumulated repo debt in a single PR:

1. Land the +574-LOC working-tree diff that fixes two real bugs (observation stranding after watermark advance; background episodes piling up across sessions) but has been sitting uncommitted.
2. Remove the `audit_log` column-name translation tax (`created_at AS at` aliases at every callsite).
3. Bring the AI's own MCP skills (`better_memory/skills/*.md`) into alignment with the current MCP surface — they predate the episodic redesign and never mention episodes, hardening, or the `consumed_*` statuses.
4. Archive two large plan documents (`2026-04-19-ui-phase-2-pipeline-kanban.md`, `2026-04-19-phase-3-consolidation.md`) that describe an architecture (Pipeline / ConsolidationService / InsightService) replaced by the episodic redesign. The corresponding service modules were never built.

## Why now

These items share three properties: low risk, no shared file edits, and high reader-confusion cost if left longer. They block sub-design B (which will touch `services/reflection.py` and `ui/app.py`) by leaving uncommitted diff in those files, and they actively mislead future AI agents searching specs for "how does consolidation work."

## Approach

Single branch, single PR, four logical commits. The commits are independent — sequencing is for review clarity, not technical dependency.

| # | Commit | Files touched | Type |
|---|---|---|---|
| 1 | Land WIP | `services/reflection.py`, `services/spool.py`, `ui/app.py`, `ui/templates/fragments/observations_synth_banner.html`, `tests/conftest.py`, `tests/services/test_reflection.py`, `tests/services/test_spool.py`, `docs/hooks-setup.md`, `docs/superpowers/specs/2026-04-20-episodic-memory-design.md` | Feature |
| 2 | Audit-log rename | `better_memory/ui/queries.py`, observation drawer template(s), audit-log-related tests | Refactor |
| 3 | Skills surgical update | `better_memory/skills/{memory-write,memory-retrieve,memory-feedback,session-close}.md` | Docs |
| 4 | Archive dead plans | `docs/superpowers/plans/2026-04-19-ui-phase-2-pipeline-kanban.md`, `docs/superpowers/plans/2026-04-19-phase-3-consolidation.md`, new `docs/superpowers/archive/` directory | Docs |

## Commit 1 — Land WIP

The diff is already two coherent feature commits squashed into the working tree. Land as a single feature commit with this message:

```
feat(synthesis): auto-ignore unused observations; auto-close background episodes on session_end

- ReflectionSynthesisService._auto_ignore_unused marks LLM-ignored
  observations as consumed_without_reflection so they don't strand
  in the active pool after the watermark advances.
- SpoolService._maybe_close_episode_for_session_end closes
  unhardened episodes (goal=NULL) on Stop hook; hardened episodes
  stay open per episodic-memory-design.md §3.
- last_run_counts surfaced in the synthesis banner.
```

**Verification.** Run `uv run pytest` (must be green). Manually exercise `POST /observations/synthesize` against a seeded DB and confirm the banner shows the new run-count fields (`created`, `augmented`, `merged`, `ignored`, `auto_ignored`).

**Failure mode.** If tests are not green right now, this commit splits into smaller pieces or stalls until B fixes them.

## Commit 2 — Audit-log column rename

Mechanical rename. No schema migration. `audit_log.created_at` is the table's column name; `ObservationAuditEntry.at` is the dataclass field. The mismatch has bitten us twice (memories `54446ae9`, `87d40804`). Aligning the names removes the alias dance permanently.

**Edits.**

- Rename the dataclass field: `ObservationAuditEntry.at` → `ObservationAuditEntry.created_at`.
- Drop `created_at AS at` from SELECTs in `ui/queries.py` (and any other callsite).
- Update Jinja templates reading `entry.at` (likely `templates/fragments/observation_drawer.html`) to `entry.created_at`.
- Update any test fixture that constructs the dataclass with `at=...` to use `created_at=...`.

**Verification.** After the rename:

```bash
grep -rE 'entry\.at\b|AS at\b|ObservationAuditEntry\(.*\bat=' better_memory tests
# expected: zero results
```

Then `uv run pytest`.

**Out of scope.** No schema migration. No view. No alias. The column on disk stays `created_at`; the in-memory field becomes `created_at`. One name, end-to-end.

## Commit 3 — Skills surgical update

Four small edits. Existing four-skill structure (write / retrieve / feedback / close) is preserved — what's broken is content drift, not shape.

**`better_memory/skills/memory-write.md`** — add a new subsection "Working with episodes" near the top (before the existing `When to use` rules). Cover:
- When `memory_start_episode` should fire (start of focused work where there's a goal worth tracking).
- What hardening means (close with a real outcome, not `no_outcome`).
- The `consumed_into_reflection` vs `consumed_without_reflection` statuses, and that they happen automatically post-synthesis — observers don't set them.
- Keep the existing evidence-in-hand outcome rule and decision-points guidance unchanged.

**`better_memory/skills/memory-retrieve.md`** — add one paragraph after the existing retrieval-cadence section:
- "For raw observation drill-down (e.g. when investigating a specific incident), use `memory_retrieve_observations`. The default `memory_retrieve` returns distilled reflections, which is usually what you want — drill down only when reflections don't have enough specificity."

**`better_memory/skills/memory-feedback.md`** — add one sentence to the reinforcement section:
- "Hardening an episode with a real outcome (`memory_close_episode` with `outcome='success'` or `'failure'`) is a stronger reinforcement signal than `record_use` alone — the whole episode's observations inherit the outcome."

**`better_memory/skills/session-close.md`** — add one sentence near the end:
- "Background episodes (those with `goal=NULL`) auto-close on the Stop hook now via `SpoolService._maybe_close_episode_for_session_end`. Manual close is only needed for hardened episodes (those with a goal), which the next session's reconcile prompt resolves."

**Verification.** No automated test — read with fresh eyes. The durable fix for skill drift (auto-generate from MCP tool docstrings) is rejected for A; revisit in sub-design D.

## Commit 4 — Archive dead plans

The two plan files describe the Pipeline / Consolidation / Insight architecture that the episodic redesign replaced. There is no `consolidation.py` or `insight.py`; the Pipeline tab is gone; `candidate_approve` / `insight_promote` routes don't exist.

**Steps.**

```bash
mkdir docs/superpowers/archive
git mv docs/superpowers/plans/2026-04-19-ui-phase-2-pipeline-kanban.md docs/superpowers/archive/
git mv docs/superpowers/plans/2026-04-19-phase-3-consolidation.md docs/superpowers/archive/
```

Prepend each archived file with this banner block (above the existing title):

```markdown
> **SUPERSEDED.** This plan describes the Pipeline / Consolidation / Insight
> architecture, replaced by the episodic memory redesign. See
> `docs/superpowers/specs/2026-04-20-episodic-memory-design.md`. Kept for
> design-decision history; not implementable against the current codebase.

```

**Verification.** After the moves:

```bash
grep -rE 'ui-phase-2-pipeline-kanban|phase-3-consolidation' docs/
```

Update any cross-reference to point at the new `archive/` path, or remove the link if it no longer makes sense in context.

**Out of scope.** Wider plan/spec audit — only the two named files get archived. Other plans (e.g. `ui-phase-1-web-skeleton.md`) describe shipped work and stay in `plans/` as historical reference.

## Risks

1. **Skills refresh has no automated check.** Drift will return as the MCP surface evolves. The durable fix (option (c) from brainstorming: generate skills from MCP tool docstrings) is rejected for A and not yet scheduled — flag for sub-design D consideration.
2. **Audit-log rename may miss a callsite** if it's hidden behind a dynamic attribute lookup or a string literal. Mitigated by the grep verification, but `getattr(entry, 'at')`-style code would not be caught.
3. **Commit 1 assumes existing tests are green.** If they're not, the commit splits or stalls until B fixes the underlying issue. The spec assumes green is the current state — verify before starting.
4. **Cross-reference fixup in Commit 4 may surface broken links elsewhere.** The grep is scoped to `docs/`; the codebase or README might also reference the moved files.

## Out of scope (deferred)

| Item | Sub-design |
|---|---|
| Async-bridge helper extraction in `ui/app.py` | B |
| `worker.join()` timeout | B |
| Replace deep `monkeypatch(...synthesize)` tests with real-path tests | B |
| `_last_run_counts` contract widening | B |
| Retention scheduler | C |
| Hook observability log (`BETTER_MEMORY_HOOK_LOG`) | C |
| Pyright in CI | D |
| `_project_name()` ambiguity | D |
| Auto-generate skills from MCP tool docstrings | D (or its own thing) |
| Wider plan/spec audit | None — explicitly out |
| New skills (e.g. dedicated `episode-lifecycle.md`) | None — rejected during brainstorming |

## Decisions log

These were the explicit choices made during brainstorming:

| Decision | Choice | Why |
|---|---|---|
| WIP handling | Land as part of A | One PR clears both the WIP and the cleanup; user-preferred over branching them apart. |
| Skills depth | Surgical update | Existing four-skill structure is the right shape; only content drift is broken. Full restructure deferred. |
| Audit-log direction | Rename dataclass field | The field is UI-internal; no external consumers. Avoids schema-side complexity (generated columns, views). |
| Dead plans handling | Move to `archive/` with SUPERSEDED banner | Kept for design-history reference; banner prevents future readers from treating them as live. |
