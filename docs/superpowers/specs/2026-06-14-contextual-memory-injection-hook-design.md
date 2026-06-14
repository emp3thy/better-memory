# Contextual Memory Injection Hook — Design

**Date:** 2026-06-14
**Status:** Approved design (brainstorming)
**Repo:** better-memory

## Problem

Memories are loaded **once**, at `SessionStart` (the bootstrap hook), surfaced as
`additionalContext` the assistant treats as background. By the time a relevant
moment arrives (often many turns later — e.g. invoking `writing-plans`), the
pertinent reflection has scrolled out of attention or been dropped by
compaction. Result: high-value memories (e.g. the 0.9-confidence
"surface planning guardrails + confidence scores" reflection) are silently
ignored unless the user explicitly asks for them. This was observed repeatedly,
including the session that motivated this design.

Root cause: retrieval is **one-shot and filter-based**, not **continuous and
relevance-ranked to the current input**. Nothing re-surfaces the *right* memory
at the *right* moment.

## Goal

Surface the **relevant curated memories at the moment they matter** by matching
them against the current prompt / tool-input on every turn (or tool call),
injecting a small, relevance-filtered set as `additionalContext`.

**In scope:** the *managed, scored, curated* memories only —
- **semantic memories** (user-stated facts/preferences), and
- **reflections** (distilled lessons),
both **project- and general-scoped**.

**Out of scope:** observations and knowledge-base docs (raw / separately
surfaced). Embeddings/vector search. LLM-based relevance judging.

## Non-negotiable constraints

1. **No new external dependency.** No Ollama, no AWS semantic search, no new
   pip packages. (On `main`, Ollama is the only embeddings backend and is the
   default; the no-Ollama FTS embeddings backend is unmerged. We must not depend
   on it.)
2. **Backend-agnostic.** Must work under both `BETTER_MEMORY_STORAGE_BACKEND`
   = `sqlite` and `agentcore`. Therefore retrieval MUST go through the
   `StorageBackend` abstraction (`backend.retrieve()`, `backend.semantic_list()`)
   — never direct SQL or direct service calls (those are sqlite-only and are not
   wired for agentcore).
3. **Fast — it blocks the turn.** `UserPromptSubmit` / `PreToolUse` injection is
   synchronous. Target well under ~300 ms. This is achievable because the
   curated set is *small* (tens of items per project), so we fetch the managed
   ranked set and filter it in pure Python — no index, no model.
4. **Never break a turn.** The hook swallows all errors, logs to `hook_errors`,
   and always exits 0.

## Architecture

Two pieces: a reusable retrieval method (the brains) and a thin hook (the
plumbing). Keeping the logic in a service means the MCP layer and the hook share
one implementation.

### 1. `retrieve_relevant` — new service method (the brains)

Location: a service callable by both the MCP server and the hook (e.g.
`better_memory/services/relevant.py`, or a method on an existing service),
constructed over the active `StorageBackend`.

```
retrieve_relevant(
    query: str,
    *,
    project: str,
    limit: int = 5,
    kinds: tuple[str, ...] = ("reflection", "semantic"),
) -> list[RelevantMemory]
```

Algorithm:
1. **Fetch the managed, ranked candidate sets** via the storage abstraction
   (both already union project + general, both backends implement them):
   - reflections: `backend.retrieve(project=…, track_exposure=False)` → the
     `do`/`dont`/`neutral` buckets (already ranked by useful_count/confidence;
     `retrieve()`'s WHERE already includes `scope = 'general'`). Flatten the
     buckets in order **do → dont → neutral**, preserving each bucket's internal
     order; this flattened position is the "managed score" used for tie-breaking.
     (`neutral` may be dropped from injection — tunable.)
   - semantic: `backend.semantic_list(project=…)` → project + general
     (ranked by useful_count + overlooked weight).
2. **Extract keywords from `query`**: lowercase; split on non-alphanumeric;
   drop a small stopword set and tokens shorter than 3 chars. (Deterministic,
   pure Python.)
3. **Whole-word filter**: keep a memory if its searchable text contains at least
   one keyword as a **whole word** (regex `\bword\b`, case-insensitive).
   Searchable text:
   - reflection → `title` + `use_cases` + `hints` (joined),
   - semantic → `content`.
4. **Order** matches by **(# distinct keyword hits) desc, then managed score
   desc** (managed score = the order the backend already returned them in).
5. **Cap** to `limit` and return as a small typed result (kind, id, title/
   content, confidence/score, matched-keyword count).

Properties: pure-Python filter over a small in-memory list → no FTS index, no
migration, no embeddings; identical behavior on sqlite and agentcore because it
operates on the dicts the abstraction returns.

**Upgrade path (documented, not built):** if a project's curated set ever grows
large enough that fetch-all is slow, replace step 1+3 internals with an FTS5
index over reflections/semantic *behind this same signature* — callers
unchanged.

### 2. The hook (the plumbing)

A thin script (`better_memory/hooks/contextual_inject.py`, dispatchable for both
events). Per the existing hook pattern (`session_bootstrap.py`):
1. Read JSON payload from stdin.
2. Derive the **query**:
   - `UserPromptSubmit` → the user prompt text.
   - `PreToolUse` → the tool name + args (e.g. skill name + args, or Write
     target/preview), concatenated.
3. Resolve `project` (existing `config.project_name(cwd)` logic) and open the
   active backend via the factory.
4. Call `retrieve_relevant(query, project=…, limit=…)`.
5. Format and print `additionalContext`. Never raise; exit 0.

### 3. Trigger config switch

`BETTER_MEMORY_CONTEXT_INJECT_MODE` ∈ `userprompt` | `pretool` | `both`
(default `both`). Both hooks are registered in `settings.json`; the switch makes
each hook a no-op when its event isn't selected — so the mode can be changed
without editing `settings.json`, enabling live A/B of which trigger is more
useful.

`PreToolUse` matcher: scope to high-value tools (e.g. `Skill`, `Task`, `Write`)
to avoid firing on every trivial tool call. (Exact matcher set is a tunable.)

### 4. Injection format

Compact, imperative framing so it reads as guidance, not background:

```
RELEVANT MEMORY — apply unless it conflicts with the user's request:
• [reflection · conf 0.90] writing-plans: retrieve planning memories + surface guardrails/confidence BEFORE drafting
• [semantic] Never ask the user to babysit a PR — start the bot-watch loop automatically
```

Caps: **≤ 5 items**, **~400 tokens** total (truncate lowest-ranked first).

## Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| Sources | semantic + reflections only (project + general) |
| Match | whole-word, case-insensitive keyword |
| Ranking | # distinct keyword hits, then managed score |
| Trigger | `UserPromptSubmit` + `PreToolUse`, with `mode` switch (userprompt/pretool/both) |
| Dedup | **None** — proximity/recency matters; a relevant memory re-injects whenever it matches *(deferred option, see below)* |
| Empty match | inject nothing |
| Exposure tracking | `track_exposure=False` for the hook (automated injection must not flood the scoring/decay counters; deliberate MCP consults still track) |
| Deps | none (no Ollama, no FTS, no new packages) |
| Backends | via `StorageBackend` abstraction; works sqlite + agentcore |

## Deferred options (intentionally not built)

- **Dedup / cooldown window.** Considered and rejected for v1: re-surfacing a
  relevant memory at the moment it's relevant is a feature; suppressing it to
  avoid repetition risks the memory being absent exactly when needed. Risk
  acknowledged: repeated identical injection consumes context and, over many
  turns, the assistant habituates and under-weights it. If context bloat or
  habituation becomes a real problem, add a session-scoped dedup keyed by
  `session_id` (suppress re-injecting a memory ID within N turns, and dedup
  against the SessionStart bootstrap set).
- **Always-on floor** on empty match (top-N by managed score) — left as a toggle.
- **FTS5 relevance index** for large curated sets (see Upgrade path).
- **Blocking gate** for non-negotiables (e.g. block writing a plan until
  confidence scores exist) — a separate `PreToolUse` deny hook, complementary to
  this injection hook. Enforcement is a gate's job, not repetition's.

## Error handling

- Hook never raises: wrap body in try/except, log to `hook_errors`, emit empty
  `additionalContext`, exit 0.
- `retrieve_relevant` returns `[]` on any backend error rather than throwing into
  the hook.
- agentcore semantic path: if `semantic_list` is not yet implemented for
  agentcore, `retrieve_relevant` degrades to reflections-only rather than
  failing. (Verify current agentcore `semantic_list` status during planning.)

## Testing

Unit tests for `retrieve_relevant` (the logic that matters), against **both**
backends via the abstraction:
- keyword extraction (lowercasing, stopword/short-token removal),
- whole-word matching (`art` does NOT match `start`; `plan` matches `Plan.`),
- scope union (project + general both returned),
- ordering (more keyword hits ranks higher; ties broken by managed score),
- `limit` cap,
- empty-match returns `[]`,
- error path returns `[]` (no throw).

Hook-level: feed sample `UserPromptSubmit` / `PreToolUse` payloads on stdin,
assert well-formed `additionalContext` JSON and exit 0; assert a forced internal
error still exits 0 with empty context.

## Files (anticipated)

- Create: `better_memory/services/relevant.py` (or method on existing service)
- Create: `better_memory/hooks/contextual_inject.py`
- Modify: `better_memory/config.py` (read `BETTER_MEMORY_CONTEXT_INJECT_MODE`)
- Modify: MCP server (optional) — expose `retrieve_relevant` as a tool too
- Modify: `scripts/setup.sh` + README/hooks-setup.md — register the new hooks
- Tests under the repo's test tree
