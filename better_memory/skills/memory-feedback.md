# memory-feedback

When to use: immediately after evidence arrives that confirms or disproves a prior decision.

## Two call sites

`record_use` is the canonical way to stamp an outcome once evidence is in hand. It's called in two situations:

**1. Closing the loop on a memory YOU wrote as neutral.** Every `memory.observe(outcome='neutral')` with a decision baked in should, eventually, get a matching `record_use(id, outcome=...)` once validation arrives.

**2. Validating a memory you RETRIEVED and applied.** If a retrieved memory influenced your work, close the loop with `record_use(retrieved_id, outcome=...)` once you know whether it held up.

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

## Pattern 2 — validating a retrieved memory

```python
hits = memory.retrieve(query="add FK index", component="db")

for item in hits["do"]:
    # About to apply — mark the read
    memory.record_use(item["id"])

# ... finish the work, observe the result ...

# The first approach worked
memory.record_use(hits["do"][0]["id"], outcome="success")

# The second approach is stale — schema changed underneath
memory.record_use(hits["do"][1]["id"], outcome="failure")
```

## Rule of thumb

If you're about to write `memory.observe(outcome='success')` but the tests haven't run, stop. Write `outcome='neutral'`, hold the id, and come back with `record_use` once the evidence is real.
