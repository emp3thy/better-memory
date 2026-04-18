# session-close

When to use: at the end of a coding session, before wrapping up.

## Checklist

1. Decision points since the last `memory.observe()` — did you capture each with the right `outcome`?
2. Any memory you `retrieve()`d and USED — did you call `record_use(id, outcome=...)`?
3. Any abandoned approaches — recorded as `failure`?
4. Any unexpected behaviour worth the next session knowing — recorded as `neutral` or `failure`?

If you missed any of the above, write them now. Short. Specific. Past tense.

## Audit: walk back through the session

Starting from the last `memory.observe()` you made, scan forward in the transcript:

- Every decision point → expect a matching `observe()` with an explicit `outcome`.
- Every failed attempt → expect an `observe(outcome='failure')`.
- Every applied retrieval → expect a `record_use(id, outcome=...)`.

If any are missing, write them now.

## Anti-patterns

- Don't fabricate a memory to meet a quota. If nothing decision-worthy happened, record nothing.
- Don't re-record what you already observed inline — each decision point is a single observation.
- Don't default to `outcome='neutral'` when you actually know the result. Be honest about success or failure.
