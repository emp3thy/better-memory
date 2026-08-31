# Global Preferences

(foreign content elided)

# better-memory (MANDATORY)

better-memory is the MCP server for persistent knowledge across sessions. Project scope is inferred from the current working directory — do not pass it explicitly. Use it automatically — never ask permission.

## Retrieve: Knowledge at Startup, Memories per Task (MANDATORY)

At the very start of every conversation, check the curated knowledge base:
- `mcp__better-memory__knowledge_list` (no args). If it returns standards-scoped docs, read every one before other tool calls. `knowledge_search` when a task may be covered by curated markdown knowledge.

Do NOT do a broad no-query memory retrieval at session start — memories surface contextually as you work. When you begin a task (starting work, entering a codebase, debugging, making a decision that may have prior context), call `mcp__better-memory__memory_retrieve` with a `query` describing that task. For raw observation lookup, `memory_retrieve_observations` takes a `query` too.

## Reinforce: After Using a Memory

When a retrieved memory materially helped or misled you, reinforce it:
- `mcp__better-memory__memory_record_use` with the observation `id` and `outcome` (`success` or `failure`)

Do this sparingly and only when the signal is clear. Reinforcement decays stale memories and promotes reliable ones.

## Synthesize: When Pending > 0

Reflection synthesis is driven by Claude (you) via the better-memory-synthesize skill, not by a background daemon. Trigger it when:
- `mcp__better-memory__memory_start_episode` returns `pending_synthesis.pending > 0`
- The user mentions consolidating, synthesizing, distilling, or processing pending episodes
- A session ends with new closed episodes that haven't been distilled

Invoke the `better-memory-synthesize` skill (project-scoped, available globally via `~/.claude/skills/better-memory-synthesize/`). It walks through the per-episode drain loop using `memory.synthesize_next_get_context` + `memory.synthesize_next_apply`.

## Record: As You Go (Not at the End)

Write to better-memory immediately when something worth preserving happens. Do not batch writes at session end.

**Priority triggers — write immediately:**
- Architectural decision made
- Bug fixed (non-trivial) — record root cause and fix
- New dependency, env variable, or infrastructure change
- Project structure or conventions discovered
- Recurring pattern or gotcha identified
- User preference or workflow requirement stated

**Mandatory record triggers — not judgment calls, always do these:**
- **After every code-review fix commit.** If the task reviewer (spec or quality verdict) flagged an issue and you committed a fix, record the bug/gap as a `failure` observation. The fix's existence proves it was non-obvious. Do this BEFORE moving on to the next task.
- **Before marking a subagent task complete.** Sweep that task's fix commits and reviewer findings. If any revealed a non-obvious fact, record it.
- **At the end of each phase / PR cycle.** Before invoking `superpowers:finishing-a-development-branch`, pause and do a memory sweep: walk the phase's commits and reviewer comments for anything worth preserving across sessions.

These triggers override the "worth preserving" judgment call — if the trigger fires, you record. Skipping a trigger is a CLAUDE.md violation.

**How to write:**
- `mcp__better-memory__memory_observe` with a concise, factual `content` summary
- Set `outcome` deliberately: `success` (do this again), `failure` (don't do this), `neutral` (reference only). The outcome determines which bucket the memory lands in on retrieval — choose it based on what future-you should take away.
- Always fill the typed fields where applicable (see schema below)
- Retrieve first to avoid duplicates

**Do not record:**
- Trivial tasks or one-off commands
- Speculative conclusions from incomplete information
- Anything already in CLAUDE.md

## Observation Schema (MANDATORY)

`memory_observe` takes typed fields instead of a free-form metadata dict. Fill them deliberately so future retrievals hit.

| Field | Required | Description |
|-------|----------|-------------|
| `content` | Yes | Concise factual summary. Include enough specifics (names, paths, values) that the memory stands alone. |
| `outcome` | Yes in practice | `success`, `failure`, or `neutral`. Defaults to `neutral` if omitted, but omitting loses signal — set it explicitly. |
| `component` | When applicable | Subsystem / module / package name (e.g. `orchestrator`, `dashboard`, `growatt_client`). Enables component-scoped retrieval. |
| `theme` | When applicable | Cross-cutting topic tag (e.g. `bug`, `decision`, `architecture`, `convention`, `gotcha`, `dependency`, `infrastructure`, `preference`). Roughly equivalent to a category. |
| `trigger_type` | When applicable | What prompted the observation (e.g. `user-feedback`, `test-failure`, `review`, `deploy`). Optional but useful for filtering. |

Project scope is attached automatically from the session/cwd. Do not try to embed a project name in `content` unless it is genuinely cross-project.

**Examples:**

Bug fix:
```
memory_observe(
  content="Growatt API Timespan.day returned inflated consumption; switched to Timespan.hour (288 snapshots / 12 = kWh). Removed get_daily_data.",
  component="growatt_client",
  theme="bug",
  outcome="failure",
  trigger_type="debugging"
)
```

Architectural decision:
```
memory_observe(
  content="Calculator uses _estimate_generation_hourly + _morning_floor_kwh; charge = max(gap_pct, morning_floor_pct).",
  component="calculator",
  theme="decision",
  outcome="success"
)
```

General-purpose gotcha (no component):
```
memory_observe(
  content="Python ZoneInfo is unavailable on Windows without tzdata package; install tzdata or fall back to zoneinfo backport.",
  theme="gotcha",
  outcome="failure"
)
```

# Process Discipline

(foreign content elided)
