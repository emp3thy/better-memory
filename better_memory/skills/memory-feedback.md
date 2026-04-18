# memory-feedback

When to use: immediately after a retrieved memory influences your work.

## Single rule

Every time a `retrieve()` result was used, call:

```python
memory.record_use(id, outcome='success' | 'failure' | None)
```

- `outcome='success'` — the memory's approach worked for you.
- `outcome='failure'` — the memory's approach did NOT work; something has changed.
- `outcome=None` (omit param) — memory was read/considered but wasn't validated.

## Cost: 2 seconds. Do it inline.

Do NOT batch feedback at session end. Call `record_use` the moment you've validated or disproved the memory.

## Why outcome matters

`outcome='success'` bumps `reinforcement_score += 1`; `outcome='failure'` drops it by 1.
High-reinforcement items rank above low-reinforcement at equivalent similarity — so your feedback directly shapes the next retrieval.

Recording a `failure` against a stale memory is how you retire bad advice over time.

## Inline usage pattern

```python
hits = memory.retrieve(query="add FK index", component="db")

for item in hits["do"]:
    # About to apply this approach — note the use.
    memory.record_use(item["id"])

# ... you finish the work ...

# Approach from item[0] worked.
memory.record_use(hits["do"][0]["id"], outcome="success")

# Approach from item[1] no longer applies (schema moved on).
memory.record_use(hits["do"][1]["id"], outcome="failure")
```
