# Episodic Memory Design

**Status:** draft · **Date:** 2026-04-20

**Relationship to prior specs.** This spec supersedes the aggregation model and UI defined in `2026-04-18-better-memory-ui-design.md` and redefines Plan 2 Phase 4 from `2026-04-06-better-memory-design.md` §12. The observation-level capture story from the parent spec (§4, §5, §7) is retained. The `insights` aggregation story (§8, §9) and the Management UI surface (§11) are replaced by what follows.

---

## 1. Motivation

The current system captures **observations** (atomic facts with an outcome) and aggregates them into **insights** by clustering on `(component, theme)`. The web UI surfaces this as a five-tab experience: Pipeline kanban, Sweep queue, Knowledge editor, Audit timeline, Graph view.

Two problems have emerged:

1. **Aggregation is shapeless.** Insights synthesised from theme-clusters lack a goal context. A cluster of observations tagged `(consolidation, bug)` may come from three unrelated debugging sessions; the resulting insight generalises poorly because nothing ties the observations to a shared arc of work.
2. **The UI is too complex** for how memory is actually used. Five tabs (most of them read-only browsers) overshoot the workflow.

The AWS Bedrock AgentCore **episodic memory** pattern addresses both. Observations are grouped into **episodes** — goal-bounded arcs of work with a clear outcome. **Reflections** (replacing insights) are generalised lessons synthesised *across episodes with known outcomes*. Observations from episodes that fizzled without achievement do not leak into reflections; observations from abandoned episodes feed reflections as negative signals.

The retrieval surface for the LLM becomes reflections, not observations: distilled, polarity-tagged, confidence-scored guidance.

## 2. Conceptual model

Three layers, top-down:

**Observations** — atomic per-event facts. Unchanged in purpose. Schema gains two columns (`episode_id`, `tech`).

**Episodes** — goal-bounded containers. An episode has a soft start (session-start hook opens a background episode with no declared goal) and a hard end (explicit close on goal achievement, abandonment, or superseding). Session-end does not auto-close; unclosed episodes reconcile at the next session start.

**Reflections** — generalised lessons. Replace `insights`. Carry a `polarity` (`do`/`dont`/`neutral`), a `phase` (`planning`/`implementation`/`general`), `use_cases`, `hints[]`, `confidence` (0.1-1.0), plus `project` and `tech` tags.

### Why pre-start synthesis

Synthesis is not triggered on episode close. It runs when a new episode hardens (a goal is declared). At that moment, the LLM has maximum context: the incoming goal, the tech stack, all prior reflections, and the observations accumulated since the last synthesis. The LLM proposes structured actions (new reflections, augmentations to existing reflections, merges) and the system persists them atomically. Retrieval returns the post-synthesis state.

This beats close-time synthesis because the lessons materialise when they are about to be used, not when the prior arc ended, and the synthesising agent (the LLM itself) has the context to decide what is novel versus what strengthens an existing pattern.

## 3. Episode lifecycle

### Triggers

| Trigger | Action |
|---|---|
| Session-start hook | Open background episode: `id, project, session_id, started_at, goal=NULL, hardened_at=NULL` |
| `memory.start_episode(goal, tech?)` | If a foreground episode is already open with a different goal, close it as `superseded` with `outcome=partial` (or `no_outcome` if no success signals fired). Harden the background episode (set `goal`, `hardened_at`, `tech`) or open a new foreground one. **Triggers pre-start synthesis.** |
| Git post-commit hook | Close the active episode: `outcome=success`, `close_reason=goal_complete` |
| Plan-complete signal (from `superpowers:executing-plans`) | Close as `outcome=success`, `close_reason=plan_complete` |
| `memory.close_episode(outcome='abandoned', summary=...)` | The LLM calls this when the user steers away ("no, stop, change direction"). `summary` captures what was rejected. |
| Session-end | **No action.** Open episodes remain open. |
| Next session-start, unclosed episode exists | LLM calls `memory.reconcile_episodes()` and prompts user: *"Prior session left this episode open (goal: X, started: Y). How did it end? (success / abandoned / partial / no_outcome / continuing)"*. Default on no answer: `abandoned`. |

### Nesting

Not supported. One foreground episode per session. Declaring a new goal while one is active closes the prior one as `superseded`.

### Continuing across sessions

When the user picks `continuing` at reconciliation, the episode stays open and the new session's observations bind to it. The goal and `hardened_at` carry over; `ended_at` and `close_reason` remain `NULL`. Pre-start synthesis still runs (it evaluates whether newly-accumulated observations from the prior session warrant reflections). A join table (`episode_sessions`) records the session membership so multi-session episodes trace cleanly.

### Outcome semantics and synthesis feed

| outcome | feeds synthesis? | polarity signal |
|---|---|---|
| `success` | yes | positive — strengthens `do` reflections |
| `partial` | yes | positive, weaker |
| `abandoned` | yes | **negative** — strengthens `dont` reflections |
| `no_outcome` | no | — |

Observations from `no_outcome` episodes stay in the database for audit and direct lookup but never enter synthesis prompts.

## 4. Data model

SQLite. Migration `0003_episodic_memory.sql` (exact filename subject to naming convention).

### `episodes` (new)

```
id              TEXT PRIMARY KEY
project         TEXT NOT NULL
tech            TEXT            -- nullable; lowercase-normalised on write
goal            TEXT            -- null while background
started_at      TEXT NOT NULL   -- ISO-8601 UTC
hardened_at     TEXT            -- null if never hardened
ended_at        TEXT            -- null until closed
close_reason    TEXT            -- 'goal_complete' | 'plan_complete' | 'abandoned' | 'superseded' | 'session_end_reconciled'
outcome         TEXT            -- 'success' | 'partial' | 'abandoned' | 'no_outcome' | null
summary         TEXT            -- free-text, populated on abandoned close
```

Indexes: `(project, ended_at)`, `(project, outcome)`, `(tech)`.

### `episode_sessions` (new)

```
episode_id      TEXT NOT NULL  -- FK → episodes.id
session_id      TEXT NOT NULL
joined_at       TEXT NOT NULL
left_at         TEXT            -- null while still active in this session
PRIMARY KEY (episode_id, session_id)
```

### `observations` (existing, modified)

Add columns:
- `episode_id TEXT` (FK → episodes.id; nullable — `NULL` for pre-migration rows or observations written before session-start hook fires).
- `tech TEXT` (nullable; lowercase-normalised on write).
- `status` (existing) gains new valid values: `consumed_into_reflection`, `consumed_without_reflection`.

Indexes: add `(episode_id)`, `(tech)`.

### `reflections` (new — replaces `insights`)

```
id              TEXT PRIMARY KEY
title           TEXT NOT NULL
project         TEXT NOT NULL
tech            TEXT            -- nullable; cross-project when null
phase           TEXT NOT NULL   -- 'planning' | 'implementation' | 'general'
polarity        TEXT NOT NULL   -- 'do' | 'dont' | 'neutral'
use_cases       TEXT NOT NULL   -- "when this applies" — short paragraph
hints           TEXT NOT NULL   -- JSON array of short actionable strings
confidence      REAL NOT NULL   -- 0.1-1.0
status          TEXT NOT NULL   -- 'pending_review' | 'confirmed' | 'retired' | 'superseded'
superseded_by   TEXT            -- FK self; set when a merge replaces it
evidence_count  INTEGER NOT NULL
created_at      TEXT NOT NULL
updated_at      TEXT NOT NULL
```

Indexes: `(project, status)`, `(tech)`, `(phase, polarity)`.

### `reflection_sources` (new — replaces `insight_sources`)

```
reflection_id   TEXT NOT NULL   -- FK
observation_id  TEXT NOT NULL   -- FK
PRIMARY KEY (reflection_id, observation_id)
```

### `synthesis_runs` (new — watermark)

```
project         TEXT NOT NULL
tech            TEXT            -- nullable; matches the filter used
last_run_at     TEXT NOT NULL
PRIMARY KEY (project, tech)
```

Used to scope "observations since last synthesis" on next run.

### `insights_legacy` (snapshot)

A one-off table storing a snapshot of the pre-migration `insights` and `insight_sources` rows for safety. Not written to after migration completes.

### Tables retained unchanged

`audit_log`, `knowledge_*`. Audit data continues to accrue; it simply has no dedicated UI tab.

### Tables dropped

`insights`, `insight_sources`, `insight_relations` — after content is migrated into `reflections`/`reflection_sources` and snapshotted into `insights_legacy`.

## 5. Synthesis flow

Triggered by `memory.start_episode(goal, tech?)`. Runs **before** the tool returns its reflection set to the caller.

### Steps

1. **Load context.** The service fetches:
   - All reflections for `project` with `status IN ('pending_review', 'confirmed')`, filtered by `tech` if declared.
   - Observations written since `synthesis_runs.last_run_at` for `(project, tech)`, filtered to episodes with `outcome IN ('success', 'partial', 'abandoned')`.
   - The owning episode's `goal` and `outcome` for each observation (joined), so the LLM sees context.

2. **Build prompt.** The prompt contains:
   - The incoming `goal` + `tech`.
   - The existing reflections (full schema).
   - The new observations (content, outcome, component, theme, tech, owning episode's goal + outcome).
   - A structured-output instruction block specifying the JSON response shape.

3. **LLM response schema.**

```
{
  "new": [
    {
      "title":          "<short>",
      "phase":          "planning" | "implementation" | "general",
      "polarity":       "do" | "dont" | "neutral",
      "use_cases":      "<paragraph>",
      "hints":          ["<string>", ...],
      "tech":           "<string>" | null,
      "confidence":     0.1..1.0,
      "source_observation_ids": ["<id>", ...]
    }, ...
  ],
  "augment": [
    {
      "reflection_id":  "<existing id>",
      "add_hints":      ["<string>", ...],
      "rewrite_use_cases": "<paragraph>" | null,
      "confidence_delta": float,          -- applied, then clamped to [0.1, 1.0]
      "add_source_observation_ids": ["<id>", ...]
    }, ...
  ],
  "merge": [
    {
      "source_id":      "<existing id>",
      "target_id":      "<existing id>",
      "justification":  "<short>"
    }, ...
  ],
  "ignore": ["<observation id>", ...]
}
```

4. **Persist atomically.** Within a single SQLite transaction:
   - `new` rows → `INSERT INTO reflections` with `status='pending_review'`, `evidence_count=len(source_observation_ids)`. Link via `reflection_sources`. Mark those observations `status='consumed_into_reflection'`.
   - `augment` → append hints (JSON concat, dedup), rewrite use_cases if provided, apply clamped confidence delta, add source links, recompute `evidence_count` from actual source count, bump `updated_at`. Mark those observations `status='consumed_into_reflection'`.
   - `merge` → `UPDATE reflections SET status='superseded', superseded_by=target_id` (distinct from user-invoked retire). `INSERT OR IGNORE INTO reflection_sources SELECT target_id, observation_id FROM reflection_sources WHERE reflection_id=source_id`. Delete source's rows from `reflection_sources`. Recompute target's `evidence_count`.
   - `ignore` → mark those observations `status='consumed_without_reflection'`.
   - Upsert `synthesis_runs.last_run_at = now` for `(project, tech)`.

5. **Return current reflections.** After persistence, the tool returns reflections filtered by `(project, tech, phase?)` and bucketed by polarity, ordered by `confidence DESC, updated_at DESC`.

### Short-circuit

If `memory.start_episode` is called with the same goal inside the short-circuit window (default: 10 minutes) and no new observations have arrived since `synthesis_runs.last_run_at`, the synthesis step is skipped and the existing reflection set is returned directly. Protects against redundant LLM calls on resumed work.

### Idempotency

If the LLM response references an `observation_id` that does not exist, or a `reflection_id` that is already retired, those entries are dropped and logged — the rest of the response applies. No partial commit.

## 6. MCP tool surface

| Tool | Signature | Purpose |
|---|---|---|
| `memory.observe` | `(content, outcome?, component?, theme?, trigger_type?, tech?)` — existing signature; auto-binds `episode_id` and `session_id` | Write an observation |
| `memory.start_episode` | `(goal: str, tech: str?)` → `{episode_id, reflections: {do, dont, neutral}}` | Harden/open a foreground episode; run pre-start synthesis; return reflection set |
| `memory.close_episode` | `(outcome, summary?)` → `{}` | Explicit close |
| `memory.retrieve` | `(project?, tech?, phase?, polarity?)` → `{do, dont, neutral}` | **Returns reflections.** Replaces current observation-bucket retrieval |
| `memory.retrieve_observations` | `(project?, episode_id?, component?, theme?, outcome?)` → list | Raw observation lookup (secondary surface) |
| `memory.reconcile_episodes` | `()` → list of open episodes with their `episode_id, goal, started_at` | Called by LLM at session start; drives the reconcile prompt |
| `memory.list_episodes` | `(project?, outcome?, open?)` → list | For UI |
| `memory.record_use` | `(reflection_id, outcome)` | Reinforce a reflection after it helped/misled |
| `memory.start_ui` | existing | Unchanged |

Hooks invoke `memory.close_episode` directly (not through the LLM) on commit/plan-complete.

## 7. Retrieval behaviour

Default `memory.retrieve` response shape stays bucketed (`do`/`dont`/`neutral`) for continuity with the existing CLAUDE.md workflow, but each bucket now contains **reflections**, not observations:

```
{
  "do":      [{id, title, phase, use_cases, hints, confidence, tech, evidence_count}, ...],
  "dont":    [...],
  "neutral": [...]
}
```

Observations are retrievable separately via `memory.retrieve_observations` when the LLM needs raw evidence (e.g. to cite a specific past event).

Filters: `project` (default: inferred from cwd), `tech` (optional), `phase` (optional), `polarity` (optional — subset the buckets).

Ordering within each bucket: `confidence DESC, updated_at DESC`, capped at a reasonable default (20).

## 8. UI

Two tabs in the top nav. Pipeline, Sweep, Knowledge, Audit, Graph are removed from the default nav. The Knowledge editor may return in a later phase as a standalone surface if needed, but it is out of scope for this redesign.

### Tab 1 — Episodes

Timeline, most-recent-first, grouped by day.

Row fields: `goal` (or "background session" if never hardened) · `project` · `tech` · time range · `close_reason` · `outcome` badge · observation count · reflection count.

Click → drawer with:
- Full episode details (goal, hardened_at, close_reason, summary if abandoned).
- List of observations (content snippet, outcome, component, theme).
- List of reflections this episode contributed to (via `reflection_sources`).
- Actions for open episodes: close as `success` / `partial` / `abandoned` / `no_outcome`, or `continuing` (no-op, for UI symmetry).

Top-of-tab banner when unclosed episodes exist from prior sessions; clicking the banner opens the reconcile drawer.

### Tab 2 — Reflections

Filter panel: `project` · `tech` · `phase` · `polarity` · `status` · min confidence.

Row fields: title · phase badge · polarity badge · tech · confidence bar · use_cases (preview) · `evidence_count`.

Click → drawer with:
- Full reflection (title, use_cases, hints list, phase, polarity, tech, confidence, status, evidence_count).
- Source observations linked through their episodes, showing the owning episode's outcome.
- Actions: confirm (`pending_review` → `confirmed`), retire, edit (in-place edit of `use_cases` / `hints`), promote to knowledge base.

### Reconciliation UX

Primary channel: in-chat message at session start. The LLM calls `memory.reconcile_episodes()` and presents the prompt directly.

Secondary channel: UI banner on the Episodes tab.

Both channels write to the same underlying close mechanism.

## 9. Retention

- **Reflections:** never deleted automatically. Retired reflections stay with `status='retired'` for audit.
- **Observations linked to non-retired reflections:** kept indefinitely (reflection drill-down needs them).
- **Observations linked only to retired reflections:** retained for 90 days after the reflection was retired, then archived.
- **Observations with `status='consumed_without_reflection'`:** archived 90 days after consumption.
- **Observations in `no_outcome` episodes:** archived 90 days after the episode closed.
- **Archived** = status flip to `archived`, not delete. A separate prune job (off by default) can hard-delete archived rows older than a configurable threshold.

Retention runs as an MCP tool (`memory.run_retention`) the user can invoke; no automatic scheduling in this spec.

## 10. Migration

A one-off migration script runs when the new schema ships:

1. Apply SQL migration (create `episodes`, `episode_sessions`, `reflections`, `reflection_sources`, `synthesis_runs`, `insights_legacy`; add `episode_id` and `tech` columns to `observations`).
2. Snapshot existing `insights` and `insight_sources` rows into `insights_legacy`.
3. Run a one-shot LLM synthesis pass per project: for each project, pass the existing insights + their source observations to the LLM using the same synthesis prompt shape (with "existing reflections" = empty). The LLM produces reflections. Insert with `status='pending_review'` so the user can sanity-check.
4. Observations: `episode_id` and `tech` remain `NULL` for pre-migration rows. Optional backfill — cluster existing observations into synthetic episodes by session_id + time range — is out of scope; `NULL` is acceptable and simply means the observation predates episodic capture.
5. Drop `insights`, `insight_sources`, `insight_relations` tables after the LLM-migration step succeeds.

If the LLM migration step fails for any project, it can be re-run. `insights_legacy` is never dropped automatically.

## 11. Testing

**Unit.**
- Schema migrations apply cleanly and are idempotent.
- Episode lifecycle state machine: all legal transitions; illegal transitions raise.
- Synthesis-output JSON parser: handles malformed JSON, unknown reflection ids, duplicate observation ids in `new` + `augment`.
- Merge semantics: source retires, sources move, evidence recounts, no dangling references.
- Polarity and phase values constrained by CHECK constraints.

**Integration.**
- `FakeChat` returning canned synthesis responses; drive full `start_episode` → synthesis → persist → retrieve cycles; assert DB state after each event.
- Reconciliation flow: open episode in one "session", reconcile in the next; assert `close_reason='session_end_reconciled'` and `outcome` matches user choice.
- Continue flow: reconcile as `continuing`; assert episode stays open and new observations bind to it.

**End-to-end.**
- Flask subprocess + real SQLite, Playwright for the two tabs.
- Episodes tab: create an episode, close it, verify it appears in the timeline.
- Reflections tab: seed a reflection, confirm it, verify status transition.
- Real Ollama only under `pytest -m integration`.

## 12. Risks

- **Synthesis quality.** The LLM may over-produce reflections, miss patterns, or repeatedly propose near-duplicates. Mitigation: all new reflections land as `pending_review`; confirmation is a human gate. Source observations are retained so re-synthesis with a better prompt is possible.
- **Reconciliation friction.** Users may find the session-start prompt annoying. Mitigation: the prompt is skippable with one key; default on skip is `abandoned`, which still produces useful (negative) synthesis signal rather than losing data.
- **Git hook install.** Users without the post-commit hook get no auto-close on commit; episodes fall through to reconciliation. Mitigation: bundled install command; reconcile prompt catches the gap.
- **Tech tag fragmentation.** Free-text `tech` normalised only to lowercase may still fragment (`py` vs `python`, `pg` vs `postgres`). Mitigation: accept fragmentation initially; introduce a controlled vocabulary later if it becomes a problem.
- **Migration loss.** LLM migration of existing insights is best-effort. Mitigation: `insights_legacy` retains the originals; all migrated reflections land as `pending_review` so nothing confirmed is lost silently.
- **Multi-project contamination.** An observation written under project A could, after a `tech` match, influence a reflection retrieved under project B. Mitigation: reflections always carry `project`; cross-project retrieval requires an explicit flag on `memory.retrieve`, off by default. Tech-tagged cross-project reflections are opt-in.

## 13. Out of scope

- Real-time collaborative editing of reflections.
- Graph visualisation of reflection relationships.
- Automatic scheduling of retention runs.
- Backfilling pre-migration observations into synthetic episodes.
- A Knowledge editor surface (may return in a later phase).
- Audit timeline UI (data still accrues, just no tab).
- Promotion-to-knowledge-base workflow (can be added as a simple reflection action in a later phase).

## 14. Build order (indicative)

The detailed implementation plan is the next artefact. High-level phases:

1. Schema migration (new tables, observation columns, `insights_legacy` snapshot).
2. Episode lifecycle service + `start_episode` / `close_episode` / `reconcile_episodes` / `list_episodes` MCP tools.
3. Session-start hook integration: background-episode open on session start; reconcile prompt on next session start.
4. Git post-commit hook + plan-complete integration.
5. Reflection service + pre-start synthesis orchestration.
6. `memory.retrieve` returning reflections; `memory.retrieve_observations` kept for raw lookup.
7. One-shot legacy insights → reflections LLM migration.
8. UI: Episodes tab.
9. UI: Reflections tab.
10. Strip old UI surfaces (Pipeline, Sweep, Audit, Graph).
11. Retention MCP tool.
12. End-to-end tests across both tabs.

Each phase is its own implementation plan; each delivers value independently.
