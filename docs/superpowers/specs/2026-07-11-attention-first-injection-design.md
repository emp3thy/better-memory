# Attention-First Injection Redesign — Design

Date: 2026-07-11
Status: approved (user picks: R1=A, R2=A)
Branch: feat/attention-first-injection

## Problem

better-memory injects memories into running Claude Code sessions, but the LLM
ignores them. Root causes identified in code and in episodic-memory literature:

1. **Bootstrap dumps everything at top-of-context.** `SessionBootstrapService`
   renders ALL semantic memories (uncapped) plus up to 20x3 reflections at
   SessionStart — the "lost middle" position whose influence degrades as the
   session grows. Injected semantic ids are truncated to 8 chars, so they
   cannot be rated even in principle.
2. **Contextual injections are unrateable.** The `contextual_inject` hook
   (PR #76) retrieves with `track_exposure=False`, writes no
   `session_memory_exposure` rows, and renders no memory ids. The rating loop
   never sees the memories most likely to matter.
3. **No relevance floor.** `retrieve_relevant` admits any memory with >= 1
   whole-word keyword hit. Marginal matches are noise, and noise trains the
   model to skim the injection block.
4. **No dedup.** The hook re-runs on every UserPromptSubmit and matched
   PreToolUse with no per-session suppression; repeated identical injections
   are documented (Claude Code issue tracker) to consume context and get
   skimmed.
5. **Weak formatting.** A bare `RELEVANT MEMORY — ...` line with `•` bullets:
   no XML tag, no ids, no age, no provenance framing, `dont` items as raw
   prohibitions.

Best-practice anchors (research 2026-07-11): inject per-turn at the recency
end of context (UserPromptSubmit `additionalContext`); 3–5 items max plus an
index/affordance for the rest; precision over recall (inject nothing below a
relevance floor); distinct XML tag with factual — not imperative — phrasing;
timestamp every memory; the Stop hook decision-block is the one place an
imperative rating directive reliably produces tool calls (better-memory
already does this); pair explicit ratings with implicit diagnostics.

## Constraints

- Relevance scoring must stay **backend-agnostic**: pure-Python over
  `StorageBackend.retrieve()` / `.semantic_list()` results so it works on both
  SqliteBackend and AgentCoreBackend. No embeddings, no FTS5 on the hot path.
- Hooks keep the never-raise contract; UserPromptSubmit path stays well under
  1s; failure yields empty context plus a `hook_errors` row.
- AgentCore mode has no exposure log by spec (`list_session_exposures` returns
  an empty envelope); rating there flows through `credit_one`.

## Components

### C1 — Bootstrap slimming (`better_memory/services/session_bootstrap.py`)

- Render **top-N reflections** (across do/dont, existing
  `useful_count + 3*times_overlooked` ranking) and **top-N project-scope
  semantic memories** in full, each with **full id** and an age stamp
  ("recorded 34d ago"). N default 5 per kind, `BETTER_MEMORY_BOOTSTRAP_TOP_N`.
- **General-scope semantic memories always render in full** (user pick R2=A):
  they are few and behavioural (workflow rules).
- Every non-rendered memory gets a **one-line index entry**:
  `- title-or-first-line (do|dont|semantic, conf X)` — nothing becomes
  invisible, it just stops consuming attention.
- Affordance footer: "N more memories are indexed above — call
  memory_retrieve / memory_retrieve_observations when a task touches them."
- Exposure rows written **only for fully-rendered items** (index lines are not
  exposures).
- `BETTER_MEMORY_BOOTSTRAP_TOP_N=0` preserves current full-dump behaviour
  (escape hatch).
- Fix the existing bug: semantic ids render full, never truncated to 8 chars.

### C2 — Relevance scoring v2 (`better_memory/services/relevant.py`)

- Source data unchanged: `backend.retrieve(track_exposure=False)` +
  `backend.semantic_list(track_exposure=False)`.
- Score per candidate: `hit_score * activation` where
  - `hit_score` = distinct keyword hits; hits in the reflection **title**
    count double (title is the distilled signal).
  - `activation` = `(1 + 0.2*ln(1+useful_count)) * confidence_weight`,
    `confidence_weight` = confidence when present else 1.0; multiply by 0.5
    when `times_misled > useful_count` (known-misleading penalty). All fields
    already returned by both backends; missing fields default neutral.
- **Relevance floor**: candidate needs >= `BETTER_MEMORY_CONTEXT_MIN_HITS`
  (default 2) distinct keyword hits. Below floor for all candidates → inject
  nothing (empty string, no envelope).
- Cap: `BETTER_MEMORY_CONTEXT_MAX_ITEMS` (default 3).
- `RelevantMemory` gains fields needed by the renderer: `kind, id, text,
  confidence, useful_count, age_days, polarity, score`.

### C3 — Injection format v2 (renderer in `relevant.py`)

```
<project-memory source="better-memory">
Prior knowledge from past sessions in this project (factual records; verify if stale):
1. [reflection 6aa6bf51e7e94a4a931358605521aed6 | conf 0.9 | used 15x | 34d old]
   Prioritize root-cause fixing: <title + best-matching hint>
2. [semantic 29f762c9d1e24a52... | 2d old]
   <content, truncated at 400 chars>
If any entry above materially helps or misleads this task, credit it now:
memory_credit(kind, id, 'cited'|'shaped'|'misled').
</project-memory>
```

- Distinct XML tag; factual framing (hooks docs: imperative "system command"
  phrasing can trip prompt-injection defenses).
- **Full ids always** — the rating tools require them.
- `dont`-polarity reflections rendered positively: "Do <inverse> instead
  (previously failed: <hint>)" — flipping prohibitions to do-this guidance
  measurably improves compliance. When no inverse is derivable from the
  title/hints, fall back to "Known pitfall: <text>".
- Age stamp on every item (`age_days` from `updated_at` / `created_at`).
- Closing one-line `memory_credit` affordance (C6).

### C4 — Per-session dedup (hook-side, backend-independent)

- Seen-file: `~/.better-memory/state/context_seen_<session_id>.json` holding
  `{memory_id: {last_injected_turn, count}}` plus a monotonically increasing
  turn counter incremented on each hook invocation.
- A memory is contextually injected at most once per session; re-eligible only
  after `BETTER_MEMORY_CONTEXT_REINJECT_TURNS` (default 0 = never re-inject)
  turns since last injection.
- Corrupt or missing file → treated as empty; write failures swallowed.
- Files older than 7 days pruned opportunistically on hook start.
- No DB round-trip; works identically in AgentCore mode.

### C5 — Contextual exposure tracking

- New `StorageBackend` protocol method:
  `record_exposures(*, session_id: str, items: list[tuple[str, str]], source: str) -> None`
  (items = `(kind, id)` pairs).
  - SqliteBackend: `INSERT OR IGNORE` into `session_memory_exposure` with
    `source='contextual'`; own commit; missing session_id → no-op + bump
    `rating_diagnostics.session_id_missing`.
  - AgentCoreBackend: documented no-op (consistent with its empty
    `list_session_exposures` contract).
- Migration `0011`: widen the `source` CHECK constraint to include
  `'contextual'` using the table-recreation pattern. Includes a
  data-preservation round-trip test (seed pre-migration row, migrate, assert
  every column value survives).
- `contextual_inject` calls `record_exposures` AFTER rendering; failures are
  swallowed and logged — exposure write must never block injection.
- Result: contextual injections appear in `list_session_exposures` and the
  existing Stop-hook RATE_MEMORIES directive covers them with no further
  plumbing.

### C6 — Rating loop strengthening

- Stop-hook directive (`session_close.py`) mechanics unchanged (decision:block
  at Stop validated by research). Text changes:
  - per-memory injection source shown (bootstrap / contextual / retrieve);
  - per-source exposure counts in the header line.
- Inline affordance line in every `<project-memory>` block (C3).
- Diagnostics (R1=A): `rating_diagnostics` counters —
  `contextual_fired_userprompt`, `contextual_fired_pretool`,
  `contextual_injected`, `contextual_suppressed_floor`,
  `contextual_suppressed_dedup` — so injected-vs-rated and
  UserPromptSubmit-vs-PreToolUse fire rates are observable.
- Default `BETTER_MEMORY_CONTEXT_INJECT_MODE` stays `both` (R1=A).
- No automatic decay in this phase (YAGNI; explicit ratings + existing
  overlooked-weighting already feed ranking).

### C7 — Config + docs sync

- New env vars in `config.py`, validated like `CONTEXT_INJECT_MODE`:

| Var | Default | Meaning |
|---|---|---|
| `BETTER_MEMORY_BOOTSTRAP_TOP_N` | `5` | Full-render count per kind at bootstrap; `0` = legacy full dump |
| `BETTER_MEMORY_CONTEXT_MIN_HITS` | `2` | Relevance floor (distinct keyword hits) |
| `BETTER_MEMORY_CONTEXT_MAX_ITEMS` | `3` | Max memories per contextual injection |
| `BETTER_MEMORY_CONTEXT_REINJECT_TURNS` | `0` | Turns before a seen memory may re-inject; `0` = never |

- Docs sweep (project reflection "keep website and README in sync"):
  README.md, website/configuration.md (env-var table), website/mcp-tools.md,
  website/architecture.md, website/observation-lifecycle.md, module
  docstrings that enumerate config knobs.

## Data flow (one prompt)

1. UserPromptSubmit fires → `contextual_inject` builds query from prompt text.
2. `retrieve_relevant`: fetch curated sets via backend → score (C2) → floor.
3. Dedup against seen-file (C4).
4. Survivors: render `<project-memory>` block (C3), `record_exposures`
   (C5, swallow failures), mark seen, emit `additionalContext`.
5. No survivors: emit nothing.
6. At Stop: RATE_MEMORIES directive lists all unrated exposures including
   contextual ones → `rate-session-memories` skill →
   `apply_session_ratings` → `useful_count`/`times_misled` feed the next
   session's ranking (C2 activation).

## Error handling

- Hooks never raise; every failure path yields empty context and a
  `hook_errors` row.
- Seen-file corrupt → empty; write failure → swallowed.
- Exposure write failure → swallowed + logged, injection still emitted.
- Latency: one seen-file read/write added; scoring in-memory over already
  fetched lists. No embeddings, no new DB queries on the hot path beyond the
  exposure INSERT (post-render).

## Testing

- **C1**: slim render (top-N + index + affordance + ages + full ids),
  general-scope semantic always full, `TOP_N=0` legacy behaviour, exposure
  rows only for full renders.
- **C2**: scoring (title double-weight, activation multipliers, misled
  penalty), floor boundary (1 hit rejected at default, 2 accepted),
  MIN_HITS/MAX_ITEMS config plumbing, missing-metadata neutrality.
- **C3**: full ids present, age stamps, dont-flip rendering + pitfall
  fallback, XML tag envelope, affordance line, empty on no items.
- **C4**: once-per-session dedup, REINJECT_TURNS re-eligibility, corrupt file
  → empty, prune of stale files, turn counter monotonicity.
- **C5**: protocol conformance both backends (sqlite insert / agentcore
  no-op), migration 0011 round-trip data preservation, source='contextual'
  accepted, exposure-write failure does not block injection.
- **C6**: directive text includes sources and counts, diagnostics counters
  increment on fire/inject/suppress paths.
- **E2E** (integration): contextual injection → exposure row → Stop directive
  lists it → `apply_session_ratings` credits it → ranking reflects the bump.
- Conventions: pytest `asyncio_mode=auto`, tests mirror package dirs, clocks
  injected, no inline INSERT SQL where seed helpers exist.

## Out of scope

- Automatic decay of never-rated memories (revisit with diagnostics data).
- Embedding-based relevance on the hot path.
- AgentCore-side exposure log.
- Promote-to-knowledge flows.
