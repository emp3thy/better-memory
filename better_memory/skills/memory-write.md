# memory-write

When to use: at every decision point — design finalised, implementation done, bug fixed, approach abandoned, unexpected behaviour observed.

## Mandatory fields

- `content` — short narrative (max 500 chars). Past tense. Specific.
- `outcome` — one of `success`, `failure`, `neutral`. **Required.** Pick honestly.
- `component` — the subsystem you were working on.
- `trigger_type` — what caused the write: `decision | implementation | bug | abandoned | observation`.

## When to mark outcome

| Situation | Outcome |
|---|---|
| Approach worked, tests pass, shipped | `success` |
| Tried X, it caused Y, reverted | `failure` |
| Observed unexpected behaviour but no change made | `neutral` |
| Bug identified AND fixed (same session) | `success` — record the fix |
| Bug identified but NOT fixed yet | `neutral` — record the observation |
| Design chosen after considering alternatives | `success` for chosen, optional `failure` for abandoned |

## Examples

### Success

```python
memory.observe(
    content="Added FK index on insight_sources.observation_id — query cost dropped from 40ms to 2ms on 10k rows.",
    component="services/insight",
    trigger_type="implementation",
    outcome="success",
)
```

### Failure (the important one — record these!)

```python
memory.observe(
    content="Tried opening two sqlite3 connections to memory.db from a thread pool. WAL mode hung on writer handoff. Reverted to single-connection per MCP loop.",
    component="db/connection",
    trigger_type="abandoned",
    outcome="failure",
)
```

### Failure — "tried X, caused Y, don't do this when Z"

```python
memory.observe(
    content="Tried wrapping retrieve() in asyncio.to_thread from the MCP handler. Caused connection-pool contention under 3+ concurrent calls. Don't do this when the underlying conn is per-loop.",
    component="mcp/server",
    trigger_type="abandoned",
    outcome="failure",
)
```

### Neutral

```python
memory.observe(
    content="FTS5 treats '-' as NOT. Query 'test-marker' parses as `test AND NOT marker`. Documented; no code change.",
    component="search/hybrid",
    trigger_type="observation",
    outcome="neutral",
)
```

## Cadence

Record failures with the same frequency as successes. Failures prevent future regressions — they are not bad memories, they are learning.

## Anti-patterns

- Don't batch at session end. Write at the point of decision.
- Don't prefer `neutral` when you have actual signal. If it worked, say `success`. If it didn't, say `failure`.
- Don't write a memory for every trivial action — only decision points.
- Don't omit `outcome`. The field is mandatory for a reason: the retrieval bucketer depends on it.
