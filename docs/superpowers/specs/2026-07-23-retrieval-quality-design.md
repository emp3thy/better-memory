# Retrieval quality: unified scoring, embeddings, evidence-anchored ratings

**Date:** 2026-07-23
**Status:** approved design, pending implementation
**Predecessor:** PR #81 (rating loop + shortlist + demotion, measured 22.96% useful in A/B), PR #82.

## Guardrails (from planning memories + standards)

- **[[da7ff62e]] / [[be7ad6bf]]** (conf 0.9, useful 15/13): planning memories + knowledge
  standards consulted before drafting — done; `standards/ralph-runtime.md` rules applied
  (feature branch at task start, confidence-scored plan to follow, visualiser handoffs).
- **[[98056ebc]]** (conf 1.0, useful 18): website/README sync — PR-A and PR-B touch
  `mcp/tools.py` registrations and config: `website/configuration.md`, `website/index.md`
  tools showcase must be updated in the same PRs.
- Dismissed: TDD-RED-step reflection (no guard being moved), ralph-queue reflection
  (work is built here, not queued).

## Problem

Three retrieval-quality gaps remain after #81, all measured on the live DB:

1. **No semantic matching.** `reflection_embeddings` (vec0, 768-dim) has 0 rows —
   the synthesis write path never embeds. `memory.retrieve`'s `query` fusion is
   BM25-only; a query phrased differently from a reflection's wording misses.
2. **Rich-get-richer prior.** Ranking key is raw `useful_count`: 67 useful / 192 served
   (35% hit rate) permanently outranks 3 useful / 4 served (75%). New memories starve
   behind popular ones; the demotion CASE from #81 only handles the zero-useful case.
3. **Noisy labels.** The rating signal every ranking term feeds on is high-variance:
   in A/B runs, byte-identical memory sets on the same task were rated `shaped` in one
   session and `ignored` in another. Nothing anchors a non-ignored rating to anything.

Plus a documentation defect with a root cause worth fixing: the user-level CLAUDE.md
enumerates `memory_retrieve` parameters that do not exist (`component`, `scope_path`,
`window`), training every session to make silently-degraded calls. Enumerating params
in prose is drift-by-construction — the MCP schema already self-describes.

## Design

### 1. Unified scoring model (replaces the ORDER BY stack)

SQL keeps filtering only (project/scope/status/tech/phase/polarity). Ordering moves to
Python (~150 rows; SQLite `sqrt()` not guaranteed; unit-testable):

```python
rated    = useful_count + times_overlooked + times_ignored
positive = useful_count + times_overlooked   # overlooked = relevance evidence
score    = wilson_lower_bound(positive, rated, z=1.96)   # 0.0 when rated == 0
```

Sort: `score DESC, confidence DESC, updated_at DESC`. Same model for semantic memories
(`created_at` as final tiebreaker, as now).

**Deleted:** `OVERLOOKED_RANKING_WEIGHT`, `IGNORED_DEMOTION_FLOOR`,
`IGNORED_DEMOTION_WEIGHT`, and the demotion CASE in both services. Wilson subsumes
them: 0 positive / 58 rated → LB ≈ 0 (stays buried); the 0/0 gap is covered by
exploration below. Worked examples: 67/192 → 0.28; 3/4 → 0.30; 0/58 → 0.00.

**Query fusion unchanged in shape:** RRF (`1/(60+rank)`) over rank lists. The prior
rank list now comes from the Wilson ordering.

### 2. Exploration: reserved slot per bucket

After ranking, each polarity bucket's shortlist (default 5) reserves its **last slot**
for the best *untested* candidate — `rated < 3` — ranked by query relevance, then
recency, when one exists. Buckets with no untested candidate fill all 5 slots normally.

Bounded dilution by construction: proven memories keep ≥4 slots. Deterministic —
testable, reproducible in the A/B harness. With rating coverage at ~100% (sync Stop
hook, #81), every exploration serve converts to a rating within one session.

### 3. Reflection + semantic embeddings, three-leg fusion

- **Write path:** synthesis apply (new / augment / merge) embeds
  `title + "\n" + use_cases + "\n" + "\n".join(hints)` via the existing
  `OllamaEmbedder` (nomic-embed-text, 768-dim — matches the vec0 schema). Best-effort:
  `EmbeddingError` → log + skip; synthesis never blocks on Ollama. Augment/merge
  re-embed (text changed). Semantic memories: same on create/update_text, into a new
  `semantic_embeddings` vec0 table (**migration 0014**).
- **Lazy self-heal:** in `memory.retrieve`'s query path, candidates missing embeddings
  are embedded opportunistically (single `embed_batch`, cap ~20/call, best-effort).
  Historical rows heal on first relevant retrieval; write-time failures self-repair.
- **One-shot CLI:** `python -m better_memory.cli.backfill_embeddings` — embeds all
  active reflections + semantic memories missing vectors; idempotent; run at deploy.
- **Fusion:** `_fuse_by_relevance` becomes three-leg RRF: prior rank (Wilson), BM25
  rank, vec-kNN rank — same constant as `search/hybrid.py`. Query embedded once per
  retrieve call; Ollama failure → two-leg (today's behaviour). Rows without embeddings
  are simply absent from the vec leg. sqlite embeddings backend (embedder None):
  everything no-ops to BM25-only.

### 4. Evidence-anchored ratings

- **Migration 0015:** `evidence TEXT` (nullable) on `session_memory_exposure`.
- **Validation** (`services/memory_rating.py`, in the existing
  validate-before-savepoint pass): `cited/shaped/misled/overlooked` require non-empty
  `evidence` (trimmed, ≤500 chars) — one line: what the memory changed, or a quote.
  Missing → `ValueError`, batch rejected. `ignored` requires none (accepted if sent).
- **Tool schemas:** rating items in `memory.apply_session_ratings` gain optional
  `evidence` property (required-by-validation for non-ignored; description says so).
  `memory.credit` gains `evidence`, required for all its classes.
- **Skill rewrite** (`rate-session-memories`): STEP 2 becomes evidence-first — *write
  the evidence line before choosing the class; no evidence → the class is `ignored`.*
  Applying the test before choosing the label is the variance-reduction mechanism.
- **UI:**
  - Reflection and semantic detail/drawer: **evidence history** section — exposure
    rows with `evidence IS NOT NULL`, newest first, cap 10, each with classification
    badge + evidence + rated date. Answers "why does this rank here" with receipts.
  - `/diagnostics` last-20-rated table: evidence column.
- **Compat:** contextual-inject footer nudge updated to mention evidence. AgentCore
  paths remain no-ops. Evidence is audit-only in this PR — no scoring use yet.

### 5. Docs: drift-proof CLAUDE.md + startup sentinel

- **Snippet rewrite** (`better_memory/skills/CLAUDE.snippet.md`): behavioural
  instructions only — "always pass `query` describing the task", "credit with
  evidence" — **never enumerate parameter names/types**. Schemas are the single
  source of truth; nothing left to drift.
- **Live edit:** same rewrite applied to the user's `~/.claude/CLAUDE.md`
  better-memory section (local edit, not a PR artifact).
- **Drift sentinel** (`hooks/session_bootstrap.py`, best-effort, ~ms): scan the
  CLAUDE.md better-memory section for param tokens adjacent to tool names that are
  absent from the live registry (imported from `mcp/tools.py`, so the check tracks
  future schema changes automatically). On drift, append one warning line to bootstrap
  additionalContext naming the phantom params. Silent when clean; never blocks.

## Delivery

| PR | branch | content | gate |
|----|--------|---------|------|
| A | `feat/retrieval-quality` | §1 + §2 + §3, migration 0014, backfill CLI, website sync | unit (Wilson math, slot, fusion, self-heal) + full suite + **24-session A/B: distinct useful% not below the 22.96% baseline** |
| B | `feat/evidence-ratings` | §4, migration 0015, skill + UI + website sync | unit + integration + full suite (no A/B — ranking untouched) |
| C | `feat/claude-md-drift` | §5 snippet + sentinel (+ live CLAUDE.md edit alongside) | proofread + sentinel unit test |

Order: **A → B** (B's evidence requirement would break A's A/B rating sessions if it
landed first). C anytime. Each PR babysat to merge: checks green + threads resolved →
squash-merge.

## Error handling

Every Ollama touchpoint is best-effort with graceful degradation (vec leg absent →
BM25; embed failure → skip). Evidence validation fails loudly by design. Migrations
idempotent; 0014/0015 are additive (new table / new nullable column) — no table
recreation, no data-preservation risk.

## Testing

- Wilson: closed-form cases (0/0→0, 0/58→~0, 3/4>67/192, monotonicity, z pinned).
- Slot: untested present/absent, exactly-one-slot, relevance-then-recency ordering.
- Fusion: three-leg RRF vs hand-computed; missing-embedding absence; Ollama-down
  degradation (embedder raising).
- Self-heal: candidate without embedding gets one after retrieve (fake embedder);
  cap respected; failure silent.
- Evidence: batch rejection per class; `ignored` exempt; ≤500 enforcement; credit
  path; UI read-model rows.
- Sentinel: phantom param detected; clean file silent; malformed/absent CLAUDE.md
  never raises.
- A/B rerun for PR-A via existing `autoresearch/memuse-260721-run/runner.py`.
