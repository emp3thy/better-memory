# memory-retrieve

When to use: before starting any meaningful coding task, or when entering a new component.

## Two retrieval tools

`better-memory` exposes two distinct retrieve tools. Pick by purpose:

- `memory.retrieve` — distilled **reflections** bucketed by polarity
  (`do` / `dont` / `neutral`). Filter-only; no free-text query. Use this
  as the default at the start of work to find generalised lessons.
- `memory.retrieve_observations` — raw observations. Supports a free-text
  `query` (hybrid FTS5 + sqlite-vec) plus filters like `component`. Use
  this to drill into a specific incident or hunt for an exact prior
  decision.

## Steps

1. Identify the project and tech you'll be working in.
2. Call `memory.retrieve` with whichever filters narrow the buckets
   usefully — typically `project` and/or `tech`, optionally `phase`
   (`planning` / `implementation` / `general`):

   ```python
   result = memory.retrieve(
       project="better-memory",
       tech="python",
       phase="implementation",
   )
   ```

3. Inspect the three buckets:
   - `do` — prior successes. Reuse patterns and approaches listed here.
   - `dont` — **hard constraints**. Do NOT repeat these approaches. If
     you're tempted to try something here, stop and reconsider.
   - `neutral` — general context, no strong signal either way.
4. Also read `insights` (confirmed patterns) and `knowledge` (standards,
   language conventions, project docs).
5. After reading, call `memory.record_use(id)` for any item you're about
   to apply — even before you know the outcome. If the approach later
   succeeds, re-call with `outcome='success'`; if it fails,
   `outcome='failure'`.

## Golden rule

If a `dont` reflection exactly matches what you were about to do, treat
that as a hard stop. Look for an alternative or ask the user.

## Drilling into raw observations

`memory.retrieve` returns *distilled reflections* — the
reinforcement-weighted lessons synthesis has built from observations.
That's almost always what you want.

When reflections aren't specific enough — typically when investigating a
specific incident or hunting for an exact prior decision — drop down to
raw observations with `memory.retrieve_observations`:

```python
result = memory.retrieve_observations(
    query="async bridge ollama transport error",
    component="ui",
    limit=10,
)
```

With `query`, results are ranked by hybrid FTS5 + sqlite-vec relevance;
without, they are ordered newest-first. `episode_id` and `theme`
filters are ignored in query mode.

## Parameter reference

`memory.retrieve(project?, tech?, phase?, polarity?, limit_per_bucket?)`
— reflections, bucketed by polarity. No free-text query and no
component/scope/window filters; use `retrieve_observations` for those.

`memory.retrieve_observations(project?, episode_id?, component?, theme?,
outcome?, query?, limit?)` — raw observations. `outcome` is one of
`success` / `failure` / `neutral`.

## Worked example

You're about to refactor the auth middleware.

```python
result = memory.retrieve(
    project="my-app",
    tech="python",
    phase="implementation",
)

for item in result["dont"]:
    # Hard constraint. Read it. Do not repeat it.
    print(item["content"])

for item in result["do"]:
    # Prior art. Reuse the pattern.
    memory.record_use(item["id"])  # mark it as applied; outcome comes later

# Drill into prior auth-middleware incidents.
incidents = memory.retrieve_observations(
    query="auth middleware dependency injection",
    component="auth",
    limit=10,
)
```

If `dont` includes "tried injecting auth middleware through FastAPI
Depends at module scope, broke test isolation" — do not repeat that.
Find another way or ask.
