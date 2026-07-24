# memory-retrieve

When to use: before starting any meaningful coding task, or when entering a new component.

## Two retrieval tools

`better-memory` exposes two distinct retrieve tools. Pick by purpose:

- `memory.retrieve` — distilled **reflections** bucketed by polarity
  (`do` / `dont` / `neutral`). ALWAYS pass `query` — a plain-language
  description of the task at hand — or you get the same generic
  top-ranked lessons every session regardless of what you're working on.
  `query` fuses BM25 + vector search via reciprocal rank fusion against
  a Wilson-score usefulness prior. Also filter by `project`, `tech`,
  `phase`, `polarity`, `limit_per_bucket` (default 5 per bucket). Use
  this as the default at the start of work to find generalised lessons.
- `memory.retrieve_observations` — raw observations. Supports a free-text
  `query` (hybrid FTS5 + sqlite-vec) plus filters like `component`. Use
  this to drill into a specific incident or hunt for an exact prior
  decision.

## Steps

1. Identify the task you're about to do, plus the project/tech it's in.
2. Call `memory.retrieve` with a `query` describing the task, narrowed
   by whichever filters apply — typically `project` and/or `tech`,
   optionally `phase` (`planning` / `implementation` / `general`):

   ```python
   result = memory.retrieve(
       query="refactoring the auth middleware to use dependency injection",
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
5. When a returned reflection or semantic memory actually shapes your
   work, call `memory.credit(kind, id, class, evidence)` — see
   "Crediting memories you use" below. `memory.record_use` is a
   different tool: it operates on raw **observations** (ids returned by
   `memory.observe`), not on reflections/semantic memories, and is a
   no-op if you pass it a reflection id.

## Golden rule

If a `dont` reflection exactly matches what you were about to do, treat
that as a hard stop. Look for an alternative or ask the user.

## Drilling into raw observations

`memory.retrieve` returns *distilled reflections* — the lessons
synthesis has built from observations, ranked by a Wilson-score
usefulness prior. That's almost always what you want.

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

`memory.retrieve(query?, project?, tech?, phase?, polarity?,
limit_per_bucket?)` — reflections, bucketed by polarity. `query` ranks
by BM25 + vector RRF fused with the Wilson-score usefulness prior;
omitting it returns the same generic top-ranked lessons every time.
`limit_per_bucket` defaults to 5. No component/scope/window filters;
use `retrieve_observations` for those.

`memory.retrieve_observations(project?, episode_id?, component?, theme?,
outcome?, query?, limit?)` — raw observations. `outcome` is one of
`success` / `failure` / `neutral`.

## Worked example

You're about to refactor the auth middleware.

```python
result = memory.retrieve(
    query="refactoring the auth middleware to use dependency injection",
    project="my-app",
    tech="python",
    phase="implementation",
)

for item in result["dont"]:
    # Hard constraint. Read it. Do not repeat it.
    print(item["content"])

for item in result["do"]:
    # Prior art worth reusing — credit it once it actually shapes the work,
    # e.g. memory.credit("reflection", item["id"], "shaped", "reused its pattern for X")

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

## Crediting memories you actually use

When you actively use one of the retrieved memories — quote a hint,
follow its do/dont guidance, or it caused a wrong direction — call
`memory.credit(kind, id, class, evidence)` **immediately**. `kind` is
`reflection` or `semantic`. Class is `cited` if quoted, `shaped` if it
guided a decision, `misled` if it led you astray, `overlooked` if the
user pointed you back to a memory you already had but hadn't applied.
`evidence` is a required one-line string (max 500 chars) — what the
memory changed, or a quote. If you can't write one, the memory was
`ignored`; do not call credit.

This is the fresh-context signal. Memories you don't credit will
default to `ignored` at session end (caught by the
`rate-session-memories` skill). Credit-as-you-go survives compaction;
the session-end sweep can't recover what your context has forgotten.
