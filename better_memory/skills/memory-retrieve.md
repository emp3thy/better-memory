# memory-retrieve

When to use: before starting any meaningful coding task, or when entering a new component.

## Steps

1. Identify the component(s) you'll touch (`auth`, `payments`, etc.).
2. Call `memory.retrieve(query='<what you're about to do>', component='<component>', window='30d')`.
3. Inspect the three buckets:
   - `do` — prior successes. Reuse patterns and approaches listed here.
   - `dont` — **hard constraints**. Do NOT repeat these approaches. If you're tempted to try something here, stop and reconsider.
   - `neutral` — general context, no strong signal either way.
4. Also read `insights` (confirmed patterns) and `knowledge` (standards, language conventions, project docs).
5. After reading, call `memory.record_use(id)` for any item you're about to apply — even before you know the outcome. If the approach later succeeds, re-call with `outcome='success'`; if it fails, `outcome='failure'`.

## Golden rule

If a `dont` memory exactly matches what you were about to do, treat that as a hard stop. Look for an alternative or ask the user.

## Window guidance

- Default `30d` is right for active projects.
- `window='none'` retrieves all history — use when debugging long-standing issues.
- `window='7d'` for very recent context.

## Worked example

You're about to refactor the auth middleware.

```python
result = memory.retrieve(
    query="refactor auth middleware to use dependency injection",
    component="auth",
    window="30d",
)

for item in result["dont"]:
    # Hard constraint. Read it. Do not repeat it.
    print(item["content"])

for item in result["do"]:
    # Prior art. Reuse the pattern.
    memory.record_use(item["id"])  # mark it as applied; outcome comes later
```

If `dont` includes "tried injecting auth middleware through FastAPI Depends at module scope, broke test isolation" — do not repeat that. Find another way or ask.
