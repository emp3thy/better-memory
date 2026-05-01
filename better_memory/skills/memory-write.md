# memory-write

When to use: at every decision point — design finalised, implementation done, bug fixed, approach abandoned, unexpected behaviour observed.

## Mandatory fields

- `content` — short narrative (max 500 chars). Past tense. Specific.
- `component` — the subsystem you were working on.
- `trigger_type` — what caused the write: `decision | implementation | bug | abandoned | observation`.
- `outcome` — one of `success`, `failure`, `neutral`. **Default to `neutral`.** Only claim `success` or `failure` when the evidence exists *right now*.

## The evidence-in-hand rule

Set `outcome='success'` or `outcome='failure'` at observe-time ONLY when the work is already complete and the evidence is already visible:

- Tests ran and you saw the exit code.
- The approach was reverted and you committed the revert.
- The user explicitly confirmed or rejected it.

For decisions whose outcome you cannot yet prove, write `outcome='neutral'`, **keep the returned id**, and close the loop later with `memory.record_use(id, outcome=...)` once validation arrives.

This matches the reinforcement loop's design: `record_use(outcome)` is what moves `reinforcement_score`, so the outcome you stamp there is what future retrievals rank on.

## Working with episodes

`memory.observe` is the per-decision-point write. When focused work has a clear goal you want tracked end-to-end, also bracket it with an episode:

```python
episode_id = memory.start_episode(
    project="myproj",
    goal="Extract the async-bridge helper from ui/app.py",
)
# ... memory.observe() calls during the work ...
memory.close_episode(
    episode_id=episode_id,
    outcome="success",   # or "failure" / "partial" — see hardening
    summary="One sentence on what was done.",
)
```

**Hardening.** Closing an episode with a *real* outcome (`success`, `failure`, or `partial` — NOT `no_outcome`) is a stronger reinforcement signal than `record_use` alone. Every observation made during the episode inherits the outcome at synthesis time.

**Background episodes.** Sessions without an explicit `start_episode` get a background episode (no goal) auto-opened by the session-start hook and auto-closed on session-end. You only need `start_episode` / `close_episode` yourself for goal-driven work.

**Lifecycle is automatic.** Observation `status` flips through `active` → `consumed_into_reflection` (cited as a reflection source) or `consumed_without_reflection` (seen by synthesis but not cited). You don't set these — synthesis does. Just `memory.observe()` and trust the pipeline.

## When to pick each outcome

| Situation | At observe time | Later via record_use |
|---|---|---|
| Decision just made, not yet validated | `neutral` | `success` or `failure` when validated |
| Tests just passed, shipped the change | `success` (evidence in hand) | — |
| Tried X, caused Y, reverted in-session | `failure` (evidence in hand) | — |
| Pure fact / observed system behaviour | `neutral` (no outcome inherent) | — |
| Bug identified AND fixed same session | `success` (fix verified) | — |
| Bug identified, not yet fixed | `neutral` | `success` when fix lands |
| Applied a memory someone else wrote | **don't observe** — call `record_use(retrieved_id, outcome=...)` instead | — |

## Examples

### Decision at observe-time, outcome later

```python
obs_id = memory.observe(
    content="Chose SAVEPOINT-based rollback over BEGIN/ROLLBACK for ObservationService.create — lets nested calls compose if a later caller holds the outer txn.",
    component="services/observation",
    trigger_type="decision",
    outcome="neutral",   # not yet validated
)
# ... implement, run tests ...
memory.record_use(obs_id, outcome="success")   # tests green → stamp success
```

### Failure — evidence already in hand

```python
memory.observe(
    content="Tried opening two sqlite3 connections to memory.db from a thread pool. WAL mode hung on writer handoff. Reverted to single-connection per MCP loop.",
    component="db/connection",
    trigger_type="abandoned",
    outcome="failure",   # the attempt was made and already reverted
)
```

### "Tried X, caused Y, don't do this when Z"

```python
memory.observe(
    content="Tried wrapping retrieve() in asyncio.to_thread from the MCP handler. Caused connection-pool contention under 3+ concurrent calls. Don't do this when the underlying conn is per-loop.",
    component="mcp/server",
    trigger_type="abandoned",
    outcome="failure",
)
```

### Pure observation — stays neutral forever

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

- **Don't claim `success` before you have evidence.** If the code compiles but tests haven't run, it's still `neutral`. Wait.
- **Don't batch at session end.** Write at the point of decision. Outcome can be closed later via `record_use`.
- **Don't prefer `neutral` when you actually have evidence.** If the result is in hand, say `success` or `failure` honestly.
- **Don't write a memory for every trivial action** — only decision points.
- **Don't omit `outcome`.** The field is mandatory. Default to `neutral`.
