# PR-D: Deferred task-conditioned memory injection

**Date:** 2026-07-23
**Status:** approved design, pending implementation
**Predecessors:** PR #81 (rating loop), PR #83 (Wilson prior, exploration slot,
embeddings, three-leg fusion). Absorbs PR-C (CLAUDE.md rewrite + drift
sentinel) from the 2026-07-23 retrieval-quality design; PR-B (evidence
ratings) remains separate and follows.

## Guardrails (from planning memories + standards)

- **[[da7ff62e]]** (conf 0.9, useful 15): the user-level CLAUDE.md mandates a
  broad startup `memory_retrieve` + `knowledge_list`. A near-empty bootstrap
  with that mandate intact just re-creates the firehose through the tool
  channel — the mandate rewrite ships IN THIS PR. The knowledge-base channel
  keeps its startup mandate (bootstrap never carried it).
- **[[be7ad6bf]]** (conf 0.9, useful 13): planning memories + standards
  consulted before drafting — done (ralph-runtime.md rules: feature branch at
  task start, confidence-scored plan, visualiser handoffs).
- **[[98056ebc]]** (conf 1.0, useful 18): website prose synced in the same PR.

## Problem

Bootstrap is the retrieval system's weakest channel — measured 12.9–17%
useful after every ranking fix, vs 21–26% for query-conditioned retrieval —
because at SessionStart there is nothing to condition on: the same top-N
memories inject every session regardless of what the session will do. The
fix is architectural, not a better ranker: **don't inject until there is
task signal**, then let the (already live) contextual channel carry
injection with a scorer worthy of being the primary channel.

Measured basis: contextual channel hit 25% useful in its best round with a
crude keyword scorer; its instability (11–25% across rounds) is the scorer,
not the timing.

## Design

### 1. Injection architecture

`BETTER_MEMORY_INJECT_MODE` config (env), values `deferred` | `legacy`.
`legacy` = today's behaviour, byte-identical. Initial live default:
`legacy` (flipped to `deferred` after the post-deploy gate passes; legacy
path deleted after a stable week).

**SessionStart, deferred mode** (`hooks/session_bootstrap.py` +
`services/session_bootstrap.py`):
- Inject in full: ALL general-scope semantic memories (standing user rules).
- Inject one index line: "better-memory knows N reflections + M semantic
  memories for this project; relevant ones will surface as you work — or
  ask via memory_retrieve with a task query."
- Exposures recorded only for the fully-rendered general semantics.
- Episode open, session marker, failure-isolation: unchanged.

**Contextual channel** (`hooks/contextual_inject.py`):
- `UserPromptSubmit`: fires every prompt (unchanged trigger).
- `PreToolUse`: matcher widens to ALL tools with a first-fire-per-session
  latch persisted in the session's `context_seen` state; after the first
  fire, later PreToolUse events no-op immediately. (Today's `Skill|Task|
  Write` matcher misses sessions that open with Read/Bash.)
- Per-memory once-per-session dedup: unchanged.

**CLAUDE.md rewrite (absorbed PR-C):**
- `better_memory/skills/CLAUDE.snippet.md` rewritten to behavioural
  instructions only — "pass a task-describing `query` when you begin a
  task", "credit with evidence" — never enumerating parameter names/types.
  The startup mandate becomes: `knowledge_list` at startup (unchanged);
  `memory_retrieve` WITH a task query when a task begins — no broad
  no-query dump at session start.
- The user's live `~/.claude/CLAUDE.md` better-memory section gets the same
  edit (local change alongside the PR, not a repo artifact).
- **Drift sentinel** in `session_bootstrap`: scans the CLAUDE.md
  better-memory section for parameter tokens adjacent to tool names that
  are absent from the live registry (imported from `mcp/tools.py`, so it
  tracks future schema changes). On drift: one warning line appended to
  bootstrap additionalContext. Silent when clean; best-effort; never blocks.

### 2. Scorer + gate (`services/relevant.py` rewrite)

Candidate pool unchanged: active project + general reflections (non-neutral
unless `include_neutral`) + semantic memories, minus session-seen.

Three legs, reusing PR-A machinery:
- **BM25**: prompt text via `sanitize_fts5_query` (stopwords, >2-char
  tokens, OR-joined) against `reflection_fts`; semantics via their existing
  text-match path.
- **Vec**: prompt embedded once per firing through the SHARED `SyncEmbedder`
  (same instance and circuit breaker as the services — wired through the
  hook's construction). Cosine against `reflection_embeddings` /
  `semantic_embeddings`. Breaker open → leg absent.
- **Prior**: `wilson_lower_bound` from `services/scoring.py`. The ad-hoc
  `_activation` formula and the `min_hits` keyword floor are DELETED.

Fusion: RRF, same shape/constant as retrieve.

**Gate — inject vs stay silent:** a memory qualifies only with positive
relevance evidence: a BM25 match OR vec cosine ≥ `CONTEXT_VEC_FLOOR`
(config `BETTER_MEMORY_CONTEXT_VEC_FLOOR`, default 0.55, calibrated during
the eval task against labelled prompt/memory pairs from the A/B corpus).
Qualifiers ranked by fused score; top `context_max_items` (3) inject; zero
qualifiers → the hook emits nothing at all. The Wilson prior ranks among
qualifiers but can NEVER qualify a memory alone — popularity cannot force
an irrelevant injection.

**Latency:** hook is synchronous in the prompt path. Healthy Ollama ≈
20–40 ms per firing. Ollama down: one ≤5 s stall, then the breaker gives
60 s of instant BM25-only firings.

### 3. Exploration tagging + metric redefinition

- Migration (next free number at implementation time; PR-B holds 0015):
  `session_memory_exposure` gains `via_exploration INTEGER NOT NULL
  DEFAULT 0`. Additive.
- `retrieve_reflections`' bucket-fill threads the reserved-slot flag into
  the exposure write. Contextual/bootstrap exposures are always 0.
  First-source-wins dedup unchanged — an exploration serve of an
  already-exposed memory writes nothing.
- Rating flow unchanged: explorers still rated; their ratings feed Wilson.
- **Standing metric definition (the 30% goal is judged on headline):**
  - headline `useful_pct` = useful / exposures where `via_exploration = 0`
  - all-in (old definition) reported for continuity with 22.96 / 18.47
  - exploration conversion rate = fraction of exploration serves whose
    memory later earns a non-ignored rating
  `metric.py` and `analyze.py` report all three.

### 4. Delivery, rollout, validation

- One PR, branch `feat/deferred-injection`, then PR-B.
- Merge + deploy with live default `legacy` — zero behaviour change on
  merge day.
- **Post-deploy A/B, env-flag arms:** both arms run identical live code;
  the runner sets only `BETTER_MEMORY_INJECT_MODE` per arm. This is the
  structural fix for the A8 contamination (user-level hooks and runner
  hooks execute the same code either way). 24 sessions per arm.
- Gate: deferred arm's headline useful% not statistically below legacy's
  (one-sided z, α=0.05), AND deferred injection precision ≥ legacy's
  bootstrap+contextual combined. Pass → flip live default to `deferred`;
  delete legacy path after a stable week.

### Error handling

Every new path inherits existing contracts: SyncEmbedder never raises;
hooks wrap in their existing best-effort shells; gate-with-no-qualifiers
emits nothing (no empty block); sentinel silent-when-clean and never
blocks bootstrap; mode flag misparse coerces to `legacy` (fail-safe to
known behaviour).

### Testing

- Unit: gate floor (BM25-only / vec-only / neither / both), first-tool
  latch (fires once, any tool, persists across events), mode flag both
  ways (deferred renders general-semantics+index; legacy byte-identical to
  today), `via_exploration` tagging incl. dedup interaction, metric
  arithmetic for all three numbers, sentinel (phantom param detected /
  clean silent / malformed CLAUDE.md never raises), vec-floor boundary.
- Integration: hook-level e2e for both modes; contextual e2e for a
  Read-first session (latch fires).
- Deleted with `_activation`/`min_hits`: their tests.
- Website prose sync (guardrail): bootstrap/injection descriptions in
  website/*.md updated in-PR.
