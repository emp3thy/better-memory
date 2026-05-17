# Memory Rating — Design Spec

> **Update (2026-05-17):** The rating model was extended to a 5th class,
> `overlooked`, by issue #60. See
> `docs/superpowers/specs/2026-05-17-overlooked-memory-rating-class-design.md`.

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
- Two complementary rating paths:
  - **Opportunistic per-tool-use crediting**: when the LLM actively uses a retrieved memory, it credits the specific memory at that moment via `memory_credit`. Captures the freshest "cited" / "shaped" signal.
  - **Session-end sweep**: at session end, the LLM classifies all remaining unrated exposures (defaulting to `ignored`, flagging any `misled` with hindsight). Catches everything that wasn't opportunistically credited.

**Non-goals**
- Rating observations. Observations already have `used_count` / `reinforcement_score`. Adding a parallel mechanic there is out of scope.
- Decay or rate-normalization of `useful_count`. Acknowledged as a likely follow-up but deferred. The exposure table preserves the data needed to add either later without schema change.
- Auto-retiring memories based on `times_misled`. The misled count is surfaced in the UI; retirement remains a human-in-the-loop action.
- Rating sessions retroactively. Once a session is past, unrated exposures stay unrated.

## 3. Architecture Overview

```
SESSION START          DURING SESSION                            SESSION END
───────────            ──────────────                            ───────────
session_bootstrap      memory_retrieve(...)                      session_close (Stop hook)
   │  injects N+M         │  returns K memories                     │  reads unrated exposures
   │  memories            │                                         │
   ▼                      ▼                                         ▼
session_memory_       session_memory_                          {"decision": "block",
exposure              exposure                                  "hookSpecificOutput":
(source='bootstrap')  (source='retrieve')                         {"additionalContext":
                                                                    "RATE_MEMORIES..."}}
                              │                                       │
                              │  WHEN LLM USES A MEMORY:               │ forces LLM to continue
                              ▼                                       ▼
                      memory_credit(id, class)                LLM invokes
                      ── per-use, one row ──                  rate-session-memories skill
                       cited / shaped /                              │
                       (rarely) misled                               ▼
                                                              memory_apply_session_ratings
                                                                ── one SAVEPOINT ──
                                                                 remaining unrated → ignored
                                                                 (LLM flags any misled)
```

**Five new components, one extended hook:**

| Component | Type | Purpose |
|---|---|---|
| `session_memory_exposure` table + writes | Schema + service edits | Track which memory IDs were exposed in each session |
| `memory_credit` | New MCP tool | Single-rating per-use credit, called mid-session by the LLM |
| `memory_apply_session_ratings` | New MCP tool + `MemoryRatingService` | Atomic batch update at session end |
| `memory_list_session_exposures` | New MCP tool | Read-only authoritative list of unrated exposures |
| `rate-session-memories` skill | New symlinked skill | Drives the LLM through end-of-session sweep |
| Extended `session_close` (Stop hook) | Hook code | Inject the rating directive with `decision: "block"` |

**Key invariant:** `session_id` is the join key between exposure and rating. The `rated_at IS NULL` filter on the exposure row gates whether a memory is re-rated. A memory may be rated once via `memory_credit` mid-session OR once via the end-of-session sweep; never both, never twice.

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

Both methods gain a private `_record_exposure(conn, session_id, ...)` helper that the public methods call. `session_id` is resolved server-side from `os.environ["CLAUDE_SESSION_ID"]` — see §5.2.1.

`memory_retrieve_observations` is NOT instrumented (observations out of scope).

### 5.2.1 How `session_id` reaches retrieval calls — verified

Verified by reading the codebase. `CLAUDE_SESSION_ID` is set in the MCP server's environment for the entire session, so the tool handler can resolve it directly without LLM plumbing.

**Why env is sufficient:**
- The MCP server uses **stdio transport** (`better_memory/mcp/server.py:1181` — `async with stdio_server()`).
- Stdio MCP servers are spawned per-Claude-session by Claude Code as child processes with the parent's env.
- Once the server starts, `CLAUDE_SESSION_ID` stays put for the lifetime of the session — across every compact, every tool call, every sub-agent dispatch — until the session actually ends and the MCP server dies with it.
- The codebase already uses this pattern in 5+ places (`server.py:1079`, `observation.py:141`, `session_bootstrap.py:64`, `session_close.py:51,89`, `post_commit.py:149`).

**Resolution rule inside the exposure-writing helpers:**

```python
sid = os.environ.get("CLAUDE_SESSION_ID")
if not sid:
    # exposure tracking is best-effort; skip rather than fabricate
    return retrieved_memories
_record_exposure(conn, sid, ...)
return retrieved_memories
```

**Why "skip" rather than `or uuid4().hex` fallback:** the existing call sites use `or uuid4()` because every observation write *must* have some session_id. Our case is different — a synthetic per-call UUID would create one-call sessions that never group together, and the Stop hook's query (which uses the real session_id) would never find them. Skipping the exposure write is strictly better than fabricating a session_id.

**Diagnostics counter:** add a small counter to the `/diagnostics` panel — *"retrieve calls where CLAUDE_SESSION_ID was missing"*. In normal Claude-Code-driven flows this is always 0; a non-zero value flags a future Anthropic env-var change before we lose months of signal.

**When env may be absent:**
- MCP server invoked manually (debug, tests, another MCP client).
- Future Claude Code change renames or removes the var.
- Sandboxed / containerised Claude that strips certain env vars.

All three degrade silently. Confidence: ~95%.

### 5.3 Edge cases

| Case | Behavior |
|---|---|
| Same memory injected at bootstrap AND pulled mid-session | Two rows, different `exposed_at` and `source`. Rating skill collapses by ID. |
| Same memory pulled twice mid-session | Two rows; primary key includes `exposed_at`. |
| `CLAUDE_SESSION_ID` missing | Skip the insert. No synthetic IDs. |
| Memory retired/superseded mid-session | Exposure row remains. Rating apply-tool skips updates for retired memories. |
| Sub-agent within session | Same `CLAUDE_SESSION_ID`; exposures roll up. |

`INSERT OR IGNORE` is used defensively so any future code path that produces a duplicate insertion does not crash a hot path.

## 6. End-of-Session Hook Directive

One hook fires the end-of-session sweep: the existing `session_close` (Stop hook), extended to emit a force-continue directive that the LLM acts on.

### 6.1 Extended `session_close`

The existing hook writes a `session_end` spool marker. We extend it to first emit the rating directive on stdout, then write the marker. The marker behavior is preserved.

Behavior:
1. Read `CLAUDE_SESSION_ID` from env.
2. Open SQLite read-only.
3. Query `session_memory_exposure WHERE session_id = ? AND rated_at IS NULL`, joined with reflection title / semantic content.
4. If the unrated set is empty: skip the directive emission (write the marker as today and exit).
5. Otherwise: emit the directive in the form below. Truncate per-row payload to ~80 chars; cap full payload at 8 KB. If the unrated set exceeds the cap, list the first N items only; the skill's STEP 1 re-fetches the full list via `memory_list_session_exposures`.

### 6.2 Hook output JSON — verified

Verified against Claude Code hooks docs (https://code.claude.com/docs/en/hooks.md): emitting `{"decision": "block", ...}` on the Stop hook prevents the stop and forces Claude to continue, with `hookSpecificOutput.additionalContext` injected into the next model request.

```json
{
  "decision": "block",
  "hookSpecificOutput": {
    "additionalContext": "RATE_MEMORIES — <directive body, see §6.3>"
  }
}
```

Confidence on this mechanism: ~95%. Single residual risk is whether the rating skill's MCP calls all complete before Claude actually stops on the next attempt; mitigated because the skill emits one batched MCP call (`memory_apply_session_ratings`), so latency is bounded to one round-trip.

### 6.3 Rating directive template

```
RATE_MEMORIES — before this session ends, classify the memories that
were exposed during this session and that you did NOT already credit
via memory_credit.

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
  ignored — read but did not affect this session  (default)
  misled  — caused a wrong direction or wasted effort

Most exposures default to `ignored` — only flag the few that actually
shaped the session or misled you. Invoke the skill `rate-session-memories`.
It will collect your classifications and submit them as one batch via
memory_apply_session_ratings.
```

### 6.4 Why one hook, not two

The original draft also used `PreCompact`. Verification showed that PreCompact's `decision: "block"` only prevents compaction — it does not reliably force an LLM turn. Without a force-turn path, the directive would sit in context unused. Dropped for v1.

A future Claude Code event between PreCompact and the compaction running could revive that path; the data model and tools already support it.

### 6.5 Hook safety

`session_close` follows the existing hooks-never-raise rule. Any DB error during the directive query → log to `hook_errors`, skip the directive emission, write the marker, exit 0. The marker write is independent of the rating logic and must always succeed (downstream services depend on it).

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
Call `memory_list_session_exposures()` (no arguments — the server
resolves the current session from env). Read the returned list. This
is the ONLY valid set of ids to rate. (The list in the directive may
have been truncated.)

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
    "ratings": [
      {"kind": "reflection", "id": "r-abc...", "class": "cited"},
      {"kind": "semantic",   "id": "s-def...", "class": "ignored"},
      ...
    ]
  }

Call `memory_apply_session_ratings` with this JSON (server resolves
the session from env). ONE call, all ratings. Partial batches will
be rejected by the server.

STEP 4 — Verify.
The tool returns counts: {cited, shaped, ignored, misled, skipped}.
If `skipped > 0`, the server dropped ids that didn't exist or were
duplicated. That's fine — don't retry. The session is now marked rated.
```

### 7.4 Why the explicit anti-fabrication rules

The synthesize skill prompt has the same hardening because the LLM left to its own devices invents plausible-looking IDs. `memory_list_session_exposures` returns ground truth; the skill anchors classification on it before submission.

### 7.5 Opportunistic crediting via `memory_credit`

The end-of-session sweep is the safety net, but the highest-quality signal comes from crediting a memory **at the moment it is used**. Two existing skills are extended with a one-liner reminder:

- `memory-retrieve.md` (the skill that wraps `memory_retrieve`): after retrieval, add: *"When you actively use one of these memories (quoting it, applying its hint to a decision, following its do/dont guidance), call `memory_credit(id, class)` immediately. Class is `cited` if quoted, `shaped` if it guided the decision."*
- `CLAUDE.snippet.md` (the better-memory skill index): add one bullet under the "Record" section: *"Opportunistic crediting: call `memory_credit(id, class)` when a retrieved memory is actively used. The session-end sweep will catch anything you don't credit, defaulting to `ignored`."*

Constraints to keep the LLM honest:
- `memory_credit` only accepts IDs that exist in `session_memory_exposure` for the current session (otherwise it returns `not_exposed` and writes nothing — see §8.4).
- The credit can be `cited`, `shaped`, or `misled`. `ignored` is **not** a valid input class for `memory_credit` — ignored is the session-end default, not a deliberate per-use call.
- Idempotent: once a memory is credited mid-session, the session-end sweep skips it (via `rated_at IS NOT NULL`).

There is no PostToolUse hook reminding the LLM to credit. A hook that fires after every tool call would be too chatty (most tool calls don't use a memory). Crediting is LLM-initiative; the sweep is the safety net.

## 8. MCP Tools

Three new tools, all registered in `better_memory/mcp/server.py`.

### 8.1 `memory_list_session_exposures`

Read-only. Returns the unrated exposure rows for the **current** session (resolved server-side from `CLAUDE_SESSION_ID`), joined with title/content.

```python
# input
{}    # no parameters — session_id resolved server-side

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

If `CLAUDE_SESSION_ID` is missing from env → returns `{"session_id": null, "exposures": []}` and bumps the diagnostics counter. No side effects.

### 8.2 `memory_apply_session_ratings`

Atomic batch update for the **current** session (resolved server-side from `CLAUDE_SESSION_ID`). Single SAVEPOINT.

```python
# input
{
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

If `CLAUDE_SESSION_ID` is missing from env → returns `ValueError("no active session")`. Apply is meaningless without a session.

### 8.3 `memory_credit`

Per-tool-use credit for the **current** session (resolved server-side from `CLAUDE_SESSION_ID`). One memory, one classification, no batching. Called opportunistically by the LLM right after it uses a memory.

```python
# input
{
  "kind":  "reflection" | "semantic",
  "id":    "<string, required>",
  "class": "cited" | "shaped" | "misled"     # NOT "ignored"
}

# output
{
  "applied": "cited" | "shaped" | "misled" | null,    # null when skipped
  "skipped": "not_exposed" | "already_rated"
           | "memory_missing" | "memory_retired"
           | "no_session" | null
}
```

If `CLAUDE_SESSION_ID` is missing from env → returns `skipped: "no_session"` (silent, non-error — the LLM keeps working; we bump the diagnostics counter).

**Semantics:**
- Validates class ≠ `ignored` (the LLM should not call this with "ignored" — that's the session-end default).
- Validates `(session_id, kind, id)` exists in `session_memory_exposure` with `rated_at IS NULL`.
- If valid: bumps `useful_count` (for `cited`/`shaped`) or `times_misled` (for `misled`) on the target row, stamps `last_useful_at` or `last_misled_at`, and sets `rated_at = now, classification = class` on the matching exposure row.
- If the exposure is already rated, returns `skipped: "already_rated"` without writing — idempotent on retries.
- Wrapped in its own small SAVEPOINT (single-row, but still atomic so the exposure flip and the memory column bump can never disagree).

Backed by `MemoryRatingService.credit_one`, sibling of `apply_session_ratings`. Both methods share the same row-skip / update logic; `apply_session_ratings` is essentially `credit_one` called in a loop inside one SAVEPOINT.

### 8.4 Service: `MemoryRatingService`

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

### 8.5 Deliberately NOT in the apply tool

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
| Diagnostics tab | New panel "Recent ratings" — last 20 rows from `session_memory_exposure WHERE rated_at IS NOT NULL`, joined with title/content, ordered `rated_at DESC`. Add a small counter row: *"retrieve/credit calls where `CLAUDE_SESSION_ID` was missing"* — sparkline of last 7 days. Always 0 in normal Claude-Code flows; non-zero flags an env-var regression. |

### 10.1 Not building

- No dedicated rating-audit page — the diagnostics panel is enough.
- No bulk-retire-by-misled-count action — human-in-the-loop only.
- No per-memory rating history detail page — the exposure table is queryable from a SQL shell.

## 11. Error Handling

### 11.1 Exposure recording failure

Exposure inserts ride on the existing service transactions. If the insert fails, the existing rollback covers it; the LLM still receives its memories but they go untracked. Acceptable degradation. No `hook_errors` row (these are service writes, not hook writes).

### 11.2 Hook failure

`session_close` honours the hooks-never-raise rule. Exceptions → `hook_errors` row, skip directive emission, write the marker, exit 0. Symptom: that session goes unrated. The query `WHERE session_id = ? AND rated_at IS NULL` is session-scoped, so an unrated past session stays unrated — by design. Any memories that were credited mid-session via `memory_credit` are unaffected; the failure only loses the end-of-session sweep for the rest.

### 11.3 Malformed apply input

Server raises `ValueError` with a specific message. Skill instructs the LLM to read the error and resubmit. If recovery fails, the session is unrated. No crash, no partial state — the SAVEPOINT was never opened.

### 11.4 Hallucinated ID

Not an error. Surfaces as `skipped.not_exposed += 1` in the response. Skill prompt: "don't retry skipped IDs."

### 11.5 `CLAUDE_SESSION_ID` missing at MCP-tool time

Best-effort skip. `memory_list_session_exposures` returns empty, `memory_credit` returns `skipped: "no_session"`, `memory_apply_session_ratings` raises `ValueError` (apply is meaningless without a session). All three bump the diagnostics counter. No crash, no corruption.

### 11.6 Memory deleted between exposure and rating

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
- `MemoryRatingService.credit_one`:
  - Each valid class (cited/shaped/misled) produces the right column updates.
  - `class="ignored"` rejected (ValueError).
  - All four skip cases exercised (idempotent on `already_rated`).
  - Credit-then-sweep round-trip: credited memory is skipped by the subsequent sweep.
- New ORDER BY: `useful_count` precedence verified with mixed-rated-and-unrated data.

### 12.2 Hook tests

- Extended `session_close.py`:
  - Empty unrated set → directive emission skipped; marker still written.
  - Non-empty unrated set → directive contains correct IDs, truncation respected, 8 KB cap honoured, JSON shape is `{"decision":"block", "hookSpecificOutput":{"additionalContext":...}}`.
  - DB error → `hook_errors` row written, directive skipped, marker still written, exit 0.

### 12.3 Integration

- Round-trip: bootstrap exposes → mid-session retrieve exposes more → mid-session `memory_credit` rates a few → Stop hook directive emits for the rest → `apply_session_ratings` updates the unrated → re-running the sweep query within the same session returns an empty unrated set.

### 12.4 Skill smoke

The skill itself is a prompt; not unit-testable directly. A small fixture session can drive it through `FakeChat` (already used by synthesis tests) to verify the JSON it produces is well-formed.

## 13. Migration & Rollout

- One migration file: `0009_memory_rating.sql`.
- Backfill: none required. Existing reflections / semantic memories start at `useful_count = 0`; the new ORDER BY tiebreakers keep their existing order until ratings accumulate.
- Hook installation: `bm install-hooks` is extended to symlink the new `rate-session-memories` skill. The Stop hook entry (`session_close.py`) is already registered today; no new hook file. Idempotent.
- No feature flag. The system fails open: if any layer is missing, retrieval works as before.

## 13.1 Confidence and verification

Verifications completed during spec-writing:

- **Hook output mechanism for Stop.** Verified. `{"decision":"block", "hookSpecificOutput":{"additionalContext":...}}` reliably forces Claude to continue with the directive in context. Source: https://code.claude.com/docs/en/hooks.md. Confidence: ~95%.
- **`semantic_memories.status` column.** Verified by reading `better_memory/db/migrations/0008_semantic_memories.sql`. No status column; the §8.5 / apply-tool skip rule for retired memories only applies to reflections. Confidence: 100%.
- **PreCompact viability for v1.** Verified NOT viable: `decision: "block"` on PreCompact prevents compaction but does not force an LLM turn. Dropped for v1 (see §6.4); revisit if a future Claude Code event enables a force-turn path.
- **`session_id` resolution at MCP-tool time.** Verified by reading the codebase. MCP server uses stdio transport (per-Claude-session lifetime), `CLAUDE_SESSION_ID` env var is set by Claude Code for the server's whole life, and the codebase already trusts this pattern in 5+ existing call sites. No LLM plumbing required. See §5.2.1 for the resolution rule and the diagnostics counter. Confidence: ~95%.

No residual verification items for the implementation plan.

## 14. Out-of-scope / Follow-ups

- **`useful_count` decay over time.** Acknowledged as likely worth doing once we have enough data to see if inflation matters.
- **Rate-based ranking (`useful_count / count(exposures)`).** Same situation.
- **Auto-retire by `times_misled` threshold.** Surface only; human-in-the-loop today.
- **Observation rating.** Out of scope — observations already have their own counters.
- **Cross-session rating of past unrated exposures.** Skipped by design.
- **Per-memory rating history detail page.** Not building until someone needs it.
