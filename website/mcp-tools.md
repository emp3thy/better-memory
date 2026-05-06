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

Apply a synthesis decision for one episode. Atomically creates new reflections, augments existing ones, merges duplicates, marks observations consumed (or ignored), and stamps the episode synthesized.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `episode_id` | string | yes | The episode being committed. |
| `decision` | object | yes | Shape: `{new: [...], augment: [...], merge: [...], ignore: [...]}` — see the skill for per-entry field schemas. Validation errors are returned to the caller without stamping the episode failed (caller can retry). |
| `project` | string | optional | Defaults to cwd-derived. |

Returns a step summary: `{episode_id, counts, queue, failure}`.

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
