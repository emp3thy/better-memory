# MCP tools

better-memory exposes its functionality via the [Model Context Protocol](https://modelcontextprotocol.io/) over stdio. Once installed, Claude Code calls these tools directly.

## Memory tools

### `memory.observe`

Create a new observation at a decision point.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `content` | string | yes | The factual summary. Include enough specifics that the memory stands alone. |
| `outcome` | `success` / `failure` / `neutral` | recommended | Determines which retrieval bucket the memory lands in. Defaults to `neutral` if omitted. |
| `component` | string | optional | Subsystem / module / package name. Enables component-scoped retrieval. |
| `theme` | string | optional | Cross-cutting tag (`bug`, `decision`, `architecture`, `convention`, `gotcha`, `dependency`, `infrastructure`, `preference`). |
| `trigger_type` | string | optional | What prompted the observation (`user-feedback`, `test-failure`, `review`, `deploy`). |
| `tech` | string | optional | Free-text tech tag (`python`, `react`, `sqlite`). |

Project scope is auto-derived from `Path.cwd()`. Returns `{"id": "<uuid>"}`.

### `memory.retrieve`

Get reflections bucketed by polarity, plus drained spool observations.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `project` | string | optional | Defaults to current project (cwd-derived). |
| `tech` | string | optional | Filter by tech tag. |
| `phase` | `planning` / `implementation` / `general` | optional | Filter by reflection phase. |
| `polarity` | `do` / `dont` / `neutral` | optional | Restrict to one bucket. |
| `limit_per_bucket` | int | optional | Defaults to 20. |

Returns `{"do": [...], "dont": [...], "neutral": [...]}`. Drains the spool first.

### `memory.retrieve_observations`

Drill-down: get raw observations matching filters. Use `memory.retrieve` for the distilled-reflections default.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `query` | string | optional | Hybrid FTS5 + vector search. Without it, ordered by `created_at DESC`. |
| `component` | string | optional | |
| `theme` | string | optional | Ignored in query mode. |
| `outcome` | `success` / `failure` / `neutral` | optional | |
| `episode_id` | string | optional | Ignored in query mode. |
| `project` | string | optional | Defaults to current project. |
| `limit` | int | optional | Defaults to 50. |

### `memory.record_use`

Stamp reinforcement outcome on a memory after validation.

| Parameter | Type | Required |
|---|---|---|
| `id` | string | yes |
| `outcome` | `success` / `failure` | yes |

Use sparingly and only when the signal is clear. Reinforcement decays stale memories and promotes reliable ones.

### `memory.start_episode`

Start a foreground episode for a specific goal. Triggers synthesis on the prior episode if one was open.

| Parameter | Type | Required |
|---|---|---|
| `goal` | string | yes |
| `tech` | string | optional |

Returns `{"episode_id": "<uuid>", "reflections": {...}}`.

### `memory.close_episode`

Close the active episode.

| Parameter | Type | Required |
|---|---|---|
| `outcome` | `success` / `partial` / `abandoned` / `no_outcome` | yes |
| `summary` | string | optional |
| `close_reason` | string | optional |

!!! note "Outcome enum differs from observations"
    Episode outcomes do **not** include `failure`. The valid set is `success` / `partial` / `abandoned` / `no_outcome`. `failure` is valid for `memory.observe` and `memory.record_use`, not for episodes.

### `memory.list_episodes`

List recent episodes with their open/closed state.

### `memory.reconcile_episodes`

Surface and resolve inconsistencies in episode state.

### `memory.run_retention`

Manually trigger the retention service. (Auto-fires on `memory.retrieve` once per 24h.)

### `memory.start_ui`

Spawn or reuse the management UI. Returns `{"url": "...", "reused": bool}`.

### `memory.session_bootstrap`

Open or reuse a session episode and inject project + general semantic memories and reflections as `additionalContext` markdown. Reflections are capped at 20 per polarity bucket (`do` / `dont` / `neutral`), ranked by usefulness then confidence. Mirrors what the SessionStart hook does; callable manually for recovery, testing, or post-`/clear` re-injection.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `source` | `startup` / `resume` / `clear` / `compact` | optional | SessionStart payload source. Unknown values coerce to `startup` inside the service. |
| `session_id` | string | optional | Optional SessionStart payload `session_id`. Defaults to `$CLAUDE_SESSION_ID` env var, or a fresh UUID. |
| `cwd` | string | optional | Optional working directory. Defaults to the server's process cwd. |

## Semantic memory tools

Episodic observations (`memory.observe`) record what happened during a session. Semantic memories are different: user-stated facts and preferences that should surface every session.

### `memory.semantic_observe`

Record a user-stated fact or preference. Distinct from `memory.observe` — semantic memories are user-asserted current truths, not historical observations.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `content` | string | yes | The factual statement. |
| `scope` | `project` / `general` | optional | `project` (default) for project-scoped rules; `general` for cross-project workflow rules. |

### `memory.semantic_retrieve`

Return user-stated facts and preferences for the current project, merged with all general-scope semantic memories. Flat list ordered newest-first.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `project` | string | optional | Defaults to cwd-derived. |

### `memory.semantic_update`

Edit a semantic memory's content in place. Bumps `updated_at`.

| Parameter | Type | Required |
|---|---|---|
| `id` | string | yes |
| `content` | string | yes |

### `memory.semantic_delete`

Remove a semantic memory. Idempotent — no error if `id` is absent.

| Parameter | Type | Required |
|---|---|---|
| `id` | string | yes |

## Synthesis tools

Synthesis runs in Claude itself, not on the server. The two tools below split the loop: the server hands one episode's context to the IDE, the IDE-LLM decides, the server commits the decision atomically. See the [`better-memory-synthesize`](https://github.com/emp3thy/better-memory/blob/main/.claude/skills/better-memory-synthesize/SKILL.md) skill for the full workflow and decision schema.

### `memory.synthesize_next_get_context`

Return the next pending episode's full context: episode metadata, all observations on it, and tech-filtered existing reflections. Returns `{"episode_id": null, "queue": {...}}` when the queue is empty.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `project` | string | optional | Defaults to cwd-derived. |

### `memory.synthesize_next_apply`

Apply a synthesis decision for one episode. Atomically creates new reflections, augments existing ones, merges near-duplicates (combining their evidence and rating counters onto the survivor), marks observations consumed (or ignored), and stamps the episode synthesized.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `episode_id` | string | yes | The episode being committed. |
| `decision` | object | yes | Shape: `{new: [...], augment: [...], merge: [...], ignore: [...]}` — see the skill for per-entry field schemas. Validation errors are returned to the caller without stamping the episode failed (caller can retry). |
| `project` | string | optional | Defaults to cwd-derived. |

Returns a step summary: `{episode_id, counts, queue, failure}`.

## Rating tools

Memories prove their worth by being used. The rating loop closes the
feedback cycle on top of [`memory.record_use`](#memoryrecord_use):
mid-session credit via `memory.credit`, and an end-of-session sweep
driven by the
[`rate-session-memories`](https://github.com/emp3thy/better-memory/blob/main/.claude/skills/rate-session-memories/SKILL.md)
skill that classifies every exposed reflection / semantic memory as
`cited`, `shaped`, `ignored`, `misled`, or `overlooked`. The session_close hook emits
a Stop-block directive when unrated exposures remain so the skill fires
before the session ends.

Session ids resolve server-side from `CLAUDE_SESSION_ID`; none of these
tools accept a session id parameter.

### `memory.credit`

Opportunistic per-tool-use credit. Call this immediately whenever you
actively use a memory retrieved this session — quote it, follow its
guidance, or note that it misled you. Credit-as-you-go survives
context compaction; the session-end sweep catches anything missed.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `kind` | `reflection` / `semantic` | yes | Memory kind. |
| `id` | string | yes | Id of the exposed memory. |
| `class` | `cited` / `shaped` / `misled` / `overlooked` | yes | Mid-session credit cannot mark a memory `ignored` — that's only valid via the end-of-session sweep. |

### `memory.list_session_exposures`

Return the unrated `session_memory_exposure` rows for the current Claude
session. Read-only; no side effects. Used by the `rate-session-memories`
skill as the authoritative anti-hallucination list. No parameters.

### `memory.apply_session_ratings`

Atomic batch rating for the current Claude session. Called by the
`rate-session-memories` skill at session end after `memory.list_session_exposures`.
Raises if `CLAUDE_SESSION_ID` is unset.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `ratings` | array | yes | One entry per exposure. Each entry: `{kind: "reflection" \| "semantic", id: string, class: "cited" \| "shaped" \| "ignored" \| "misled" \| "overlooked"}`. Minimum one entry. |

Returns `{applied: {...}, skipped: {...}}`. Ids not in the
authoritative exposure list land in `skipped.not_exposed`.

## Knowledge tools

### `knowledge.search`

BM25 search against the knowledge base.

| Parameter | Type | Required |
|---|---|---|
| `query` | string | yes |
| `project` | string | optional |

### `knowledge.list`

List indexed knowledge documents.

| Parameter | Type | Required |
|---|---|---|
| `project` | string | optional |

## Standalone server

For manual poking, run the MCP server directly:

```bash
uv run python -m better_memory.mcp
```

It speaks JSON-RPC over stdio — pipe `initialize` / `tools/list` / `tools/call` payloads in.
