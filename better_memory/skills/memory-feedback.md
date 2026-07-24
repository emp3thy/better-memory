# memory-feedback

When to use: immediately after evidence arrives that confirms or disproves a prior decision.

## Two call sites

`record_use` is the canonical way to stamp an outcome on a raw
**observation** (an id returned by `memory.observe`) once evidence is
in hand:

**1. Closing the loop on a memory YOU wrote as neutral.** Every `memory.observe(outcome='neutral')` with a decision baked in should, eventually, get a matching `record_use(id, outcome=...)` once validation arrives.

**2. Validating an observation you RETRIEVED via `memory.retrieve_observations` and applied.** If a retrieved observation influenced your work, close the loop with `record_use(retrieved_id, outcome=...)` once you know whether it held up.

For a **reflection** or **semantic memory** (returned by `memory.retrieve` / `memory.semantic_retrieve`), `record_use` is a no-op — use `memory.credit(kind, id, class, evidence)` instead (see Pattern 2 below).

```python
memory.record_use(id, outcome='success' | 'failure' | None)
```

- `outcome='success'` — the approach worked. `reinforcement_score += 1.0`.
- `outcome='failure'` — the approach did NOT work (or no longer applies). `reinforcement_score -= 1.0`. The memory stays; only its ranking drops.
- `outcome=None` (omit) — you looked at it but don't have evidence yet.

## Cost: 2 seconds. Do it inline.

Do NOT batch feedback at session end. Call `record_use` the moment evidence is in hand.

## Why this matters

`reinforcement_score` is multiplied into every future retrieval's ranking (`score *= (1 + α·reinforcement_score)`). Proven successes surface first; proven failures sink. A memory that nobody ever validates stays ambient — the system can't learn from it.

Recording a `failure` against a stale memory is how you retire bad advice over time.

## Hardening: the strongest signal

`memory.close_episode(outcome='success' | 'partial' | 'abandoned')` is a stronger reinforcement signal than per-observation `record_use`. Every observation made during the episode inherits the outcome at synthesis time.

If the work was opened with `memory.start_episode(...)` and the goal is now resolved, prefer hardening — call `close_episode` with a real outcome. Per-observation `record_use` is still right when you're closing the loop on a single decision (e.g. validating one retrieved observation you applied), but for goal-driven work, hardening is the higher-leverage move.

## Pattern 1 — closing your own neutral observe

```python
# At decision time — no evidence yet
obs_id = memory.observe(
    content="Switched to async embedder to allow batch requests.",
    component="embeddings/ollama",
    trigger_type="decision",
    outcome="neutral",
)

# ... implement and test ...

# Evidence arrives
memory.record_use(obs_id, outcome="success")  # if tests passed
# OR
memory.record_use(obs_id, outcome="failure")  # if it broke something
```

## Pattern 2 — validating a retrieved reflection or semantic memory

`record_use` only operates on raw **observations** (ids from
`memory.observe`) — calling it with a reflection or semantic-memory id
is a silent no-op. For memories returned by `memory.retrieve` /
`memory.semantic_retrieve`, close the loop with
`memory.credit(kind, id, class, evidence)` instead:

```python
hits = memory.retrieve(query="add FK index to observations table", project="db")

# ... apply the first approach, finish the work ...

# It worked — credit it as having shaped the change, with evidence
memory.credit("reflection", hits["do"][0]["id"], "shaped",
              "reused the covering-index pattern for the new FK column")

# The second approach turned out stale — schema changed underneath
memory.credit("reflection", hits["do"][1]["id"], "misled",
              "followed this but the referenced column no longer exists")
```

## Rule of thumb

If you're about to write `memory.observe(outcome='success')` but the tests haven't run, stop. Write `outcome='neutral'`, hold the id, and come back with `record_use` once the evidence is real.
