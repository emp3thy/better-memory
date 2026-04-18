# session-close

When to use: at the end of a coding session, before wrapping up.

## Checklist

1. Every decision point since the last `memory.observe()` — captured?
2. Every `observe(outcome='neutral')` whose outcome is now known — closed via `record_use(id, outcome=...)`?
3. Every retrieved memory you actually applied — stamped via `record_use(id, outcome=...)`?
4. Any unexpected behaviour worth the next session knowing — recorded as `neutral`?

If any are missing, write them now. Short. Specific. Past tense.

## Audit: walk back through the session

Starting from the last `memory.observe()` you made, scan forward in the transcript:

- **Every decision point** → expect a matching `observe()` with an `outcome`.
- **Every `observe(outcome='neutral')` you wrote as a decision** → expect a corresponding `record_use(id, outcome=...)` once validated. If the outcome is now known but the record_use never happened, call it now.
- **Every failed attempt you reverted** → expect an `observe(outcome='failure')` (evidence was in hand at the time).
- **Every retrieved memory you applied** → expect a `record_use(id, outcome=...)`.

The goal: no in-flight decisions with unknown outcomes, and no applied retrievals without feedback.

## Still-in-flight decisions are OK

If an `observe(outcome='neutral')` has no `record_use` yet because the work genuinely isn't validated — tests still running, feature not yet shipped, user hasn't confirmed — leave it. The next session's session-close or memory-feedback skill will pick it up.

## Anti-patterns

- **Don't fabricate a memory** to meet a quota. If nothing decision-worthy happened, record nothing.
- **Don't re-record** what you already observed inline — each decision point is a single observation.
- **Don't claim `success` retroactively** on a neutral observe if you never actually validated it. `record_use` with no evidence is worse than no record_use.
- **Don't default everything to `neutral` forever.** `neutral` is the waiting room; memories that never leave it don't contribute to the reinforcement signal.
