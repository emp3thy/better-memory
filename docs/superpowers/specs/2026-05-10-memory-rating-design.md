# Memory Rating — Design Spec

**Date:** 2026-05-10
**Status:** Draft (pending implementation plan)
**Scope:** Reflections + semantic memories. Observations out of scope (they already have their own `reinforcement_score` mechanic via `ObservationService.record_use`).

## 1. Motivation

Reflections and semantic memories are injected into every session by `SessionBootstrapService` and may be pulled mid-session via `memory_retrieve` / `memory_retrieve_observations`. Today, there is no signal back from the LLM about whether any individual memory actually proved useful in that session.

The LLM that just consumed the memories is uniquely positioned to rate them. This spec adds a closed-loop rating system: at session end and before context compaction, the LLM classifies each exposed memory and the system records that signal on the memory itself. The recorded counts then influence future retrieval ranking, so memories that have proven useful surface first.

## 2. Goals and Non-goals

**Goals**
- Capture which reflections and semantic memories the LLM was exposed to in each session.
- Have the LLM self-classify each exposed memory as `cited`, `shaped`, `ignored`, or `misled`.
- Record `useful_count` (cited or shaped) and `times_misled` per memory.
- Use `useful_count` as the primary ranking key in retrieval and bootstrap injection.
- Run automatically on `PreCompact` and `SessionEnd` events.

**Non-goals**
- Rating observations. Observations already have `used_count` / `reinforcement_score`. Adding a parallel mechanic there is out of scope.
- Decay or rate-normalization of `useful_count`. Acknowledged as a likely follow-up but deferred. The exposure table preserves the data needed to add either later without schema change.
- Auto-retiring memories based on `times_misled`. The misled count is surfaced in the UI; retirement remains a human-in-the-loop action.
- Rating sessions retroactively. Once a session is past, unrated exposures stay unrated.

## 3. Architecture Overview

```
SESSION START                  DURING SESSION                  SESSION END / PRE-COMPACT
───────────                    ──────────────                  ─────────────────────────
session_bootstrap              memory_retrieve(...)            PreCompact / session_close
   │  injects N+M memories        │  returns K memories            │  reads exposure table
   ▼                              ▼                                ▼
session_memory_exposure       session_memory_exposure        additionalContext envelope
(source='bootstrap')          (source='retrieve')             "Rate these N memories:
                                                                [reflection r-abc...] ...
                                                                Run skill rate-session-memories"
                                                                       │
                                                                       ▼
                                                           LLM invokes skill, emits ONE JSON:
                                                                       │
                                                                       ▼
                                                          memory_apply_session_ratings
                                                            ── one SAVEPOINT ──
                                                             cited/shaped → useful_count++
                                                             misled       → times_misled++
                                                             ignored      → no-op
```

**Four new components, one extended hook:**

| Component | Type | Purpose |
|---|---|---|
| `session_memory_exposure` table + writes | Schema + service edits | Track which memory IDs were exposed in each session |
| `memory_apply_session_ratings` | New MCP tool + `MemoryRatingService` | Atomic batch update of `useful_count` / `times_misled` |
| `memory_list_session_exposures` | New MCP tool | Read-only authoritative list of unrated exposures |
| `rate-session-memories` skill | New symlinked skill | Drives the LLM through self-classification |
| `pre_compact` hook (new) + extension to `session_close` | Hook code | Inject the rating directive when those events fire |

**Key invariant:** `session_id` is the join key between exposure and rating. A session may produce multiple rating passes (PreCompact can fire several times in a long session); `rated_at IS NULL` on the exposure row gates whether a memory is re-rated. Once rated, never re-rated within the same session.

## 4. Data Model

### 4.1 New table: `session_memory_exposure`

```sql
CREATE TABLE session_memory_exposure (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve')),
    rated_at       TEXT,             -- NULL until rated
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);
```

The composite PK includes `exposed_at` so the same memory exposed at bootstrap and again mid-session lands as two rows (different timestamps, different sources, both kept for audit).

### 4.2 New columns on existing tables

```sql
ALTER TABLE reflections ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_useful_at TEXT;
ALTER TABLE reflections ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_misled_at TEXT;

ALTER TABLE semantic_memories ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_useful_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_misled_at TEXT;
```

### 4.3 Migration

Single file: `better_memory/db/migrations/0009_memory_rating.sql` containing the table plus both ALTER blocks.

### 4.4 Explicitly NOT in the schema

- No `rated_count` per memory — derivable from `session_memory_exposure`.
- No separate `cited_count` / `shaped_count` — combined `useful_count` is the simpler signal that was chosen.
- No `useful_score` float — integer count is enough for ranking. Decay or rate-normalization can be added later.

## 5. Exposure Tracking

Two write paths, both inside their existing service transaction envelopes (no new commits, no new connections).

### 5.1 Bootstrap injection

In `better_memory/services/session_bootstrap.py`, after `_render_reflection_bucket` and `_render_semantic` decide the rendered ID set:

```python
def _record_exposure(
    conn, session_id, reflection_ids, semantic_ids, now,
):
    rows = (
        [(session_id, "reflection", rid, now, "bootstrap") for rid in reflection_ids] +
        [(session_id, "semantic",   sid, now, "bootstrap") for sid in semantic_ids]
    )
    conn.executemany(
        "INSERT OR IGNORE INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
```

Called only after rendering succeeds — a bootstrap that errored before injection should not credit memories the LLM never saw.

### 5.2 Mid-session retrieval

Append the same insert to:
- `ReflectionSynthesisService.retrieve_reflections` — for each returned reflection ID, kind=`'reflection'`, source=`'retrieve'`.
- `SemanticMemoryService.list_for_project` — kind=`'semantic'`, source=`'retrieve'`.

Both methods gain an optional `session_id: str | None = None` parameter. When `None`, the exposure insert is skipped (no synthetic IDs). The MCP tools (`memory_retrieve` and the semantic list tool) gain the same optional parameter and pass it through.

`memory_retrieve_observations` is NOT instrumented (observations out of scope).

### 5.2.1 How `session_id` reaches retrieval calls

The MCP server is a long-lived process serving potentially multiple Claude sessions, so process-level env is not sufficient. `session_id` flows in via the MCP tool arguments:

- `session_bootstrap` runs in the SessionStart hook subprocess and already reads `CLAUDE_SESSION_ID` from env. It passes that into `_record_exposure` directly.
- `memory_retrieve` / semantic list MCP tools accept an optional `session_id` argument. Claude (the LLM) is responsible for passing the current `CLAUDE_SESSION_ID` value when invoking these tools. The skill docs (`memory-retrieve.md`) gain a one-liner reminder: "pass `session_id` when retrieving so the call contributes to the rating signal."
- If the LLM omits `session_id`, retrieval still works — only exposure tracking is skipped for that call. Graceful degradation.

This is a known cost: relying on the LLM to plumb `session_id` is fragile. **Open question for implementation:** verify whether Claude Code's MCP transport exposes a server-side hook for the current session (e.g., per-request metadata) that the tool can read without an explicit parameter. If yes, prefer that. If not, accept the optional parameter approach.

### 5.3 Edge cases

| Case | Behavior |
|---|---|
| Same memory injected at bootstrap AND pulled mid-session | Two rows, different `exposed_at` and `source`. Rating skill collapses by ID. |
| Same memory pulled twice mid-session | Two rows; primary key includes `exposed_at`. |
| `CLAUDE_SESSION_ID` missing | Skip the insert. No synthetic IDs. |
| Memory retired/superseded mid-session | Exposure row remains. Rating apply-tool skips updates for retired memories. |
| Sub-agent within session | Same `CLAUDE_SESSION_ID`; exposures roll up. |

`INSERT OR IGNORE` is used defensively so any future code path that produces a duplicate insertion does not crash a hot path.

## 6. Hook Directives

Both hooks inject `additionalContext` into the LLM's view. Neither hook calls the LLM; they only place a directive in front of it.

### 6.1 `pre_compact` (new)

New file: `better_memory/hooks/pre_compact.py`. Follows the existing hook contract: never raises, exits 0, swallows errors to `hook_errors`.

Behavior:
1. Read `CLAUDE_SESSION_ID` from env.
2. Open SQLite read-only.
3. Query `session_memory_exposure WHERE session_id = ? AND rated_at IS NULL`, joined with reflection title / semantic content for each row.
4. If empty: emit empty output (no-op).
5. Otherwise: emit the rating directive (template in §6.3). Truncate per-row payload to ~80 chars; cap the full payload at 8 KB. If the unrated set exceeds the cap, list only the first N items and the skill's `STEP 1` re-fetches the full list via `memory_list_session_exposures`.

### 6.2 Extended `session_close`

The existing hook writes a `session_end` spool marker. We extend it to also emit the rating directive (same query, same template, same cap). The marker write is preserved as-is.

### 6.2.1 Hook output mechanism — confidence: medium, verify during plan

For the rating directive to produce an actual LLM turn (not just be discarded), the hook output has to use a mechanism that Claude Code recognises as "force Claude to continue." This is **unverified** as of spec-writing and is the single most important thing the implementation plan must confirm before coding:

| Hook | Likely mechanism | Verification needed |
|---|---|---|
| `PreCompact` | Emit `{"hookSpecificOutput": {"additionalContext": "<directive>"}}` on stdout. The directive is prepended to the compaction input, so the LLM sees it before context is summarised. | Confirm `additionalContext` is in fact prepended (and not discarded), and that the LLM has a turn to act on it before compact actually runs. If compact runs immediately without an LLM turn, this approach fails and we either (a) drop PreCompact, or (b) use `decision: "block"` to cancel compact, force a turn, then rely on the next compact attempt. |
| `Stop` (session end) | Emit `{"decision": "block", "reason": "<directive>"}` to force Claude to continue with the directive as the next instruction. Plain `additionalContext` on Stop is typically informational, not turn-generating. | Confirm `decision: "block"` semantics on the current Claude Code version, and confirm the rating skill's MCP calls complete before Claude actually stops the second time. |

If either mechanism turns out not to do what we expect, fall back to **manual-only** invocation (a `/rate-memories` slash command running the same skill against the same MCP tools). The data model, MCP tools, and skill itself are unchanged; only the auto-trigger is dropped. Implementation plan should treat the hook auto-trigger as a separate sub-phase that ships only after the manual path is proven, so we don't block the rest of the system on this unverified piece.

### 6.3 Rating directive template

```
RATE_MEMORIES — before {context-window-compaction|session ends}, classify
the memories that were exposed in this session.

Reflections (N):
- r-abc123: <truncated title, 80 chars>
- r-def456: <truncated title, 80 chars>
  ...

Semantic memories (M):
- s-ghi789: <truncated content, 80 chars>
  ...

For each id, classify as one of:
  cited   — quoted or directly referenced in your reply
  shaped  — guided a decision but not cited verbatim
  ignored — read but did not affect this session
  misled  — caused a wrong direction or wasted effort

Invoke the skill `rate-session-memories`. It will collect your
classifications and submit them as one batch via
memory_apply_session_ratings.
```

### 6.4 Why both hooks

- `PreCompact` fires while conversation context is intact — highest-quality classification signal — but does not fire on short sessions.
- `SessionEnd` fires unconditionally but may run after compaction has already evicted the classifying context.
- Belt + braces. The `rated_at IS NULL` filter prevents double-rating within a session.

### 6.5 Hook safety

`pre_compact.py` follows the hooks-never-raise rule. Any DB error → log to `hook_errors`, emit empty `additionalContext`, exit 0.

## 7. `rate-session-memories` Skill

### 7.1 Location and installation

Pattern matches the existing `better-memory-synthesize` skill:

| | Path |
|---|---|
| Canonical SKILL.md | `<repo>/.claude/skills/rate-session-memories/SKILL.md` (committed) |
| User-level symlink | `~/.claude/skills/rate-session-memories` → above |

`better_memory.cli.install_hooks` is extended to (re)create the symlink alongside the hook entries it already writes. Idempotent: if the symlink exists and points to the right place, skip; if it points elsewhere, back up via `_backup()` and recreate.

### 7.2 Frontmatter

```yaml
---
name: rate-session-memories
description: Use when a session is about to end or compact, and the LLM sees a
  RATE_MEMORIES directive in additionalContext. Also use when the user explicitly
  asks to rate this session's memories.
---
```

### 7.3 Skill body

```
You are about to classify the memories exposed in THIS session.

STEP 1 — Refresh the list.
Call `memory_list_session_exposures(session_id=<current>)` and read
the returned list. This is the ONLY valid set of ids to rate.
(The list in the directive may have been truncated.)

STEP 2 — Classify each id.
For each (memory_kind, memory_id), assign exactly ONE class:
  cited   — quoted or directly referenced in your reply
  shaped  — guided a decision but not cited verbatim
  ignored — read but did not affect this session
  misled  — caused a wrong direction or wasted effort

Rules:
- Quote the id in your internal reasoning.
- Do not invent ids. Do not skip ids.
- If genuinely uncertain between two classes, prefer the lower one
  (shaped > cited, ignored > shaped, misled is never a fallback).
- Default is `ignored`, not `shaped`. "Shaped" requires evidence
  you can point to.

STEP 3 — Submit ALL ratings in ONE call.
Build a single JSON object:

  {
    "session_id": "<current>",
    "ratings": [
      {"kind": "reflection", "id": "r-abc...", "class": "cited"},
      {"kind": "semantic",   "id": "s-def...", "class": "ignored"},
      ...
    ]
  }

Call `memory_apply_session_ratings` with this JSON. ONE call, all
ratings. Partial batches will be rejected by the server.

STEP 4 — Verify.
The tool returns counts: {cited, shaped, ignored, misled, skipped}.
If `skipped > 0`, the server dropped ids that didn't exist or were
duplicated. That's fine — don't retry. The session is now marked rated.
```

### 7.4 Why the explicit anti-fabrication rules

The synthesize skill prompt has the same hardening because the LLM left to its own devices invents plausible-looking IDs. `memory_list_session_exposures` returns ground truth; the skill anchors classification on it before submission.

## 8. MCP Tools

Two new tools, both registered in `better_memory/mcp/server.py`.

### 8.1 `memory_list_session_exposures`

Read-only. Returns the unrated exposure rows for a session, joined with title/content.

```python
# input
{"session_id": "<string, required>"}

# output
{
  "session_id": "...",
  "exposures": [
    {"kind": "reflection", "id": "r-abc...", "title": "Prefer pathlib...",
     "exposed_at": "...", "source": "bootstrap"},
    {"kind": "semantic",   "id": "s-def...", "content": "User prefers dark mode",
     "exposed_at": "...", "source": "retrieve"},
    ...
  ]
}
```

No side effects.

### 8.2 `memory_apply_session_ratings`

Atomic batch update. Single SAVEPOINT.

```python
# input
{
  "session_id": "<string, required>",
  "ratings": [
    {
      "kind": "reflection" | "semantic",
      "id": "<string>",
      "class": "cited" | "shaped" | "ignored" | "misled"
    },
    ...
  ]
}

# output
{
  "session_id": "...",
  "applied": {"cited": 3, "shaped": 5, "ignored": 8, "misled": 1},
  "skipped": {
    "not_exposed":     0,
    "already_rated":   0,
    "memory_missing":  0,
    "memory_retired":  0
  }
}
```

### 8.3 Service: `MemoryRatingService.apply_session_ratings`

New file: `better_memory/services/memory_rating.py`. Connection-owning service, same pattern as `ReflectionService` and `ObservationService` — writes within its own SAVEPOINT + commit envelope.

**Pre-SAVEPOINT validation** (wholesale rejection):
- `session_id` empty → `ValueError`.
- `ratings` empty → `ValueError`.
- Any entry with unknown `kind` or `class`, or non-string `id` → `ValueError`.
- Duplicate `(kind, id)` pairs within one batch → `ValueError` (ambiguous intent; the skill instructs one rating per id).

**Inside SAVEPOINT, per rating:**

```
skip if (session_id, kind, id) not in session_memory_exposure         → skipped.not_exposed++
skip if exposure.rated_at IS NOT NULL                                  → skipped.already_rated++
skip if memory_id no longer exists in reflections/semantic_memories    → skipped.memory_missing++
skip if memory.status IN ('retired','superseded') (reflections only)   → skipped.memory_retired++

case class:
  'cited' or 'shaped':
    UPDATE <table> SET useful_count = useful_count + 1,
                       last_useful_at = :now
                   WHERE id = :id
  'misled':
    UPDATE <table> SET times_misled = times_misled + 1,
                       last_misled_at = :now
                   WHERE id = :id
  'ignored':
    pass (no UPDATE on the memory row itself)

UPDATE session_memory_exposure
   SET rated_at = :now, classification = :class
 WHERE session_id = :sid AND memory_kind = :kind AND memory_id = :id
   AND rated_at IS NULL
```

On any unhandled exception inside the SAVEPOINT: `ROLLBACK TO SAVEPOINT memory_rating_apply`, then re-raise. No partial commit.

### 8.4 Deliberately NOT in the apply tool

- No `audit_log` write. The exposure table itself carries the audit (`rated_at`, `classification`).
- No cascade from reflection misled to its source observations. The signal is about reflection-level usefulness, not the underlying evidence.
- No retry / partial commit. Validation errors fail the batch; the LLM resubmits.

## 9. Retrieval Ranking Impact

### 9.1 New ORDER BY

| Where | Before | After |
|---|---|---|
| `ReflectionSynthesisService.retrieve_reflections` | `ORDER BY confidence DESC, updated_at DESC` | `ORDER BY useful_count DESC, confidence DESC, updated_at DESC` |
| `ReflectionSynthesisService._load_episode_context` (synthesis input) | `ORDER BY confidence DESC, updated_at DESC` | Same change. |
| Semantic memory list (`SemanticMemoryService` list method) | `ORDER BY updated_at DESC` | `ORDER BY useful_count DESC, updated_at DESC` |

`useful_count` becomes the primary key. Existing fields remain as tiebreakers, so memories with no ratings yet (`useful_count = 0`) fall back to the prior order. Graceful degradation when no rating data exists.

### 9.2 `times_misled` is NOT used for ranking

- A single bad rating could permanently bury an otherwise-good memory.
- `retire` / `supersede` already exist for "this memory is wrong" — that path stays human-in-the-loop.
- `times_misled` surfaces in the UI only (§10).

### 9.3 Bootstrap injection cap interaction

`session_bootstrap` injects the top-N reflections per polarity bucket (`limit_per_bucket`, default 10). With the new ORDER BY, the top-N now favours useful memories. This is the intended outcome.

### 9.4 Inflation risk, deferred

A high-`useful_count` memory will continue to be injected and continue to be eligible for more ratings — a rich-get-richer dynamic. Two mitigations available later without schema change (the exposure table preserves the data both need):
- Decay `useful_count` over time.
- Rank by `useful_count / count(exposures)` (rate, not absolute count).

Neither is needed at v1.

## 10. UI Surfacing

Minimal additions to existing pages; no new pages except a diagnostics panel.

| Page | Change |
|---|---|
| `fragments/reflection_row.html` | Add "★ useful: N" badge next to the existing confidence chip when `useful_count > 0`. Hidden when 0. |
| `fragments/reflection_filter_form.html` | Add a "useful only" checkbox that filters `useful_count > 0`. |
| `fragments/reflection_drawer.html` | Below the existing confidence + evidence_count line, show `useful: N (last: <relative time>)` and (when M>0) `misled: M (last: <relative time>)`. |
| `fragments/semantic_row.html` + `semantic_drawer.html` | Same pattern, scaled to the simpler semantic shape. |
| Diagnostics tab | New panel "Recent ratings" — last 20 rows from `session_memory_exposure WHERE rated_at IS NOT NULL`, joined with title/content, ordered `rated_at DESC`. |

### 10.1 Not building

- No dedicated rating-audit page — the diagnostics panel is enough.
- No bulk-retire-by-misled-count action — human-in-the-loop only.
- No per-memory rating history detail page — the exposure table is queryable from a SQL shell.

## 11. Error Handling

### 11.1 Exposure recording failure

Exposure inserts ride on the existing service transactions. If the insert fails, the existing rollback covers it; the LLM still receives its memories but they go untracked. Acceptable degradation. No `hook_errors` row (these are service writes, not hook writes).

### 11.2 Hook failure

`pre_compact` and `session_close` both honour the hooks-never-raise rule. Exceptions → `hook_errors` row, empty `additionalContext`, exit 0. Symptom: that session goes unrated. The query `WHERE session_id = ? AND rated_at IS NULL` is session-scoped, so an unrated past session stays unrated — by design.

### 11.3 Malformed apply input

Server raises `ValueError` with a specific message. Skill instructs the LLM to read the error and resubmit. If recovery fails, the session is unrated. No crash, no partial state — the SAVEPOINT was never opened.

### 11.4 Hallucinated ID

Not an error. Surfaces as `skipped.not_exposed += 1` in the response. Skill prompt: "don't retry skipped IDs."

### 11.5 Memory deleted between exposure and rating

`apply_session_ratings` skips it (`skipped.memory_missing` or `skipped.memory_retired`). The exposure row stays in the table as a historical fact but remains `rated_at IS NULL`. Harmless.

## 12. Testing Strategy

### 12.1 Unit tests

- `session_memory_exposure` migration: schema created with the right columns, PK, indexes.
- `_record_exposure` helper in `session_bootstrap`: inserts both kinds, idempotent on duplicate insert.
- `retrieve_reflections` / `semantic` list: appends exposure rows when called with a `session_id`; skips when missing.
- `MemoryRatingService.apply_session_ratings`:
  - Each class produces the right column updates.
  - `not_exposed`, `already_rated`, `memory_missing`, `memory_retired` are each exercised.
  - Validation rejects empty / malformed inputs.
  - Duplicate `(kind, id)` in one batch rejected.
  - Exception inside SAVEPOINT rolls back cleanly.
- New ORDER BY: `useful_count` precedence verified with mixed-rated-and-unrated data.

### 12.2 Hook tests

- `pre_compact.py` and extended `session_close.py`:
  - Empty unrated set → empty `additionalContext`.
  - Non-empty unrated set → directive contains correct IDs, truncation respected, 8 KB cap honoured.
  - DB error → `hook_errors` row written, exit 0.

### 12.3 Integration

- Round-trip: bootstrap exposes → mid-session retrieve exposes more → hook directive emits → apply_session_ratings updates → re-running PreCompact within the same session produces empty directive.

### 12.4 Skill smoke

The skill itself is a prompt; not unit-testable directly. A small fixture session can drive it through `FakeChat` (already used by synthesis tests) to verify the JSON it produces is well-formed.

## 13. Migration & Rollout

- One migration file: `0009_memory_rating.sql`.
- Backfill: none required. Existing reflections / semantic memories start at `useful_count = 0`; the new ORDER BY tiebreakers keep their existing order until ratings accumulate.
- Hook installation: `bm install-hooks` is extended to register `pre_compact.py` and symlink the new skill. Idempotent.
- No feature flag. The system fails open: if any layer is missing, retrieval works as before.

## 13.1 Confidence and verification

This spec has a few areas where the implementation plan must do specific verification BEFORE coding to avoid building on wrong assumptions. They are all in §6.2.1 and §5.2.1, but to consolidate for the plan:

1. **Hook output mechanism for triggering an LLM turn.** Confidence: ~60%. Verify via Claude Code hook docs and a one-line spike that confirms the chosen mechanism actually produces an LLM turn. If it doesn't, ship manual-trigger only and treat hook auto-trigger as a follow-up.
2. **How `session_id` reaches mid-session retrieval calls.** Confidence: ~80% that "optional arg passed by LLM" works; verify whether MCP transport already exposes per-call session metadata that would let us skip the LLM-plumbing step.
3. **`semantic_memories.status` column.** Confidence: ~70%. The spec assumes semantic memories DO NOT have a `retired`/`superseded` status; only reflections do. Verify against the migration `0008_semantic_memories.sql`. If semantic memories also have a status, extend the §8.3 "memory_retired" skip rule.

## 14. Out-of-scope / Follow-ups

- **`useful_count` decay over time.** Acknowledged as likely worth doing once we have enough data to see if inflation matters.
- **Rate-based ranking (`useful_count / count(exposures)`).** Same situation.
- **Auto-retire by `times_misled` threshold.** Surface only; human-in-the-loop today.
- **Observation rating.** Out of scope — observations already have their own counters.
- **Cross-session rating of past unrated exposures.** Skipped by design.
- **Per-memory rating history detail page.** Not building until someone needs it.
