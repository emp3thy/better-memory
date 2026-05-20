# Architecture

better-memory is a four-layer epistemic hierarchy backed by a single SQLite database, with embeddings supplied by a local Ollama instance.

## The four layers

| Layer | Purpose | Lifecycle |
|---|---|---|
| **Observation** | A factual snapshot the AI writes at a decision point. Tagged with an outcome, component, theme, and trigger type. | Created by `memory.observe`. Eventually consumed into a reflection or archived. |
| **Reflection** | A distilled lesson synthesised from one or more observations. Has a polarity (`do` / `dont` / `neutral`), a confidence, and a use-cases description. | Created by the synthesis pipeline (LLM-driven). Reinforced by `memory.record_use`. |
| **Episode** | A bounded session of work — opened on session start, closed when the goal is met or abandoned. Observations and reflections are scoped to an episode. | Background episodes open implicitly on first observe; foreground episodes are explicit (`memory.start_episode`). |
| **Knowledge** | Human-authored markdown — standards, language conventions, per-project docs. Indexed via SQLite FTS5. Read-only for the AI. | Edited by humans. Reindexed on MCP server startup (mtime-only). |

## Storage

- **`memory.db`** — Observations, episodes, reflections, audit_log, retention runs, hook errors. Migrations at `better_memory/db/migrations/NNNN_*.sql` apply lexically at boot and are idempotent.
- **`knowledge.db`** — FTS5 index over the contents of `~/.better-memory/knowledge-base/`. Rebuilt on mtime change.
- **`spool/`** — JSON payloads written by Claude Code hooks, drained lazily by the next `memory.retrieve` call. Bad files quarantine to `spool/.quarantine/` rather than blocking the drain.

## Retrieval

`memory.retrieve` returns three buckets — `do`, `dont`, `neutral` — built from a hybrid search:

1. **FTS5 lexical** match against observation content + reflection use-cases.
2. **sqlite-vec** dense vector match against observation embeddings (768-dim from `nomic-embed-text`).
3. **Reciprocal Rank Fusion (RRF)** combines the two ranked lists.
4. Results are filtered by the bucket's polarity and weighted by `reinforcement_score` (each `memory.record_use` shifts a memory's score up on success or down on failure).

## Reinforcement

Each observation and reflection has a `reinforcement_score` that decays slowly over time and is updated by validated use:

- `memory.record_use(id, outcome="success")` → score goes up.
- `memory.record_use(id, outcome="failure")` → score goes down.

This is the lever that keeps recall faithful: a well-validated `dont` will keep surfacing for the same query class; a once-true-now-misleading observation gets demoted by repeated failure stamps.

## Self-rating loop

`memory.record_use` is the in-band reinforcement primitive. Layered on
top of it is a closed-loop self-rating cycle that runs per session and
captures whether memories actually shaped Claude's work:

1. **Exposure** — every reflection or semantic memory surfaced by
   `memory.retrieve` / `memory.semantic_retrieve` / the SessionStart
   bootstrap is logged to `session_memory_exposure` with the active
   `session_id`.
2. **Mid-session credit** — `memory.credit(kind, id, class)` lets
   Claude credit a memory as `cited`, `shaped`, `misled`, or `overlooked` the moment
   it's used. Survives context compaction.
3. **End-of-session sweep** — the
   [`session_close`](https://github.com/emp3thy/better-memory/blob/main/better_memory/hooks/session_close.py)
   hook checks for unrated exposures. If any exist, it emits a `Stop`
   block directive triggering the `rate-session-memories` skill, which
   calls `memory.list_session_exposures` and submits
   `memory.apply_session_ratings` with one class per id
   (`cited` / `shaped` / `ignored` / `misled` / `overlooked`). Only on the second Stop
   fire — after ratings land — does the hook drop the `session_end`
   marker into the spool.
4. **Aggregation** — `useful_count` / `times_overlooked` / `times_misled`
   columns on reflections and semantic memories accumulate. Retrieval
   queries `ORDER BY (useful_count + 3 × times_overlooked) DESC` so
   memories that proved themselves — or that the user had to recover —
   surface first.

The management UI's Reflections and Semantic tabs surface useful /
overlooked / misled badges per row, and `/diagnostics` exposes recent
ratings, a total overlooked count, and a `session_id_missing` counter
for instrumentation gaps.

## Synthesis pipeline

Synthesis is **IDE-driven** — Claude itself is the LLM. better-memory ships no chat client; it exposes two MCP tools and a skill that orchestrates them.

The drain loop, per pending episode:

1. **`memory.synthesize_next_get_context`** — server returns one closed-but-not-yet-synthesized episode's full context: episode metadata, all observations on it, and existing reflections filtered by tech.
2. **Claude decides** — the [`better-memory-synthesize`](https://github.com/emp3thy/better-memory/blob/main/.claude/skills/better-memory-synthesize/SKILL.md) skill walks Claude through producing a JSON decision: lists of `new`, `augment`, `merge`, and `ignore` actions per observation.
3. **`memory.synthesize_next_apply`** — server validates the decision JSON and applies it atomically: creates new reflections, augments existing ones, merges near-duplicates (combining their evidence and rating counters onto the survivor), marks observations consumed (or ignored), and stamps the episode synthesized. `audit_log` records each action.

The trigger: when `memory.start_episode` returns `pending_synthesis.pending > 0`, the skill fires and drains the queue one episode at a time. Same skill is invoked manually when the user asks to consolidate or distill pending episodes.

The lifecycle implications for observations are detailed in [Observation lifecycle](observation-lifecycle.md).

## Audit log

Every state change writes a row to `audit_log`. It's append-only — no row is ever updated or deleted — so the full history of what the AI saw, what it wrote, and what it consumed is reconstructable at any time.

## Full design spec

See [`docs/superpowers/specs/2026-04-06-better-memory-design.md`](https://github.com/emp3thy/better-memory/blob/main/docs/superpowers/specs/2026-04-06-better-memory-design.md) on GitHub for the original four-phase design with all the trade-offs, deferred decisions, and migration strategy.
