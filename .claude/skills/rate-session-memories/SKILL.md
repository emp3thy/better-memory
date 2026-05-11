---
name: rate-session-memories
description: Use when a session is about to end and the LLM sees a RATE_MEMORIES directive in additionalContext. Also use when the user explicitly asks to rate this session's memories.
---

# Rate Session Memories

You are about to classify the memories exposed in THIS session that have NOT
already been credited via `memory.credit` mid-session.

## STEP 1 — Refresh the list

Call `memory.list_session_exposures` (no arguments — the server resolves the
current session from env). Read the returned list. This is the ONLY valid set
of ids to rate. The list in the RATE_MEMORIES directive may have been truncated.

## STEP 2 — Classify each id

For each `(kind, id)` pair returned by the list, assign exactly ONE class:

- **cited** — you quoted or directly referenced the memory in a reply.
- **shaped** — the memory guided a decision but wasn't cited verbatim.
- **ignored** — you saw it but it didn't affect this session. (Default.)
- **misled** — it caused a wrong direction or wasted effort.

Rules:
- Quote the id in your reasoning so you can't drift.
- Do not invent ids. Do not skip ids.
- If genuinely uncertain between two classes, prefer the lower one
  (shaped > cited, ignored > shaped). `misled` is never a fallback.
- Default is `ignored`. "Shaped" requires evidence you can point to.

## STEP 3 — Submit ALL ratings in ONE call

Call `memory.apply_session_ratings` with this exact shape (no `session_id`
— the server resolves it):

```json
{
  "ratings": [
    {"kind": "reflection", "id": "r-abc...", "class": "cited"},
    {"kind": "semantic",   "id": "s-def...", "class": "ignored"}
  ]
}
```

ONE call. All ratings. Partial batches will be rejected.

## STEP 4 — Verify

The tool returns `{applied: {...}, skipped: {...}}`. If `skipped.not_exposed > 0`,
the server dropped ids that weren't actually exposed — don't retry; it just
means you classified an id that wasn't in the authoritative list.

The session is now marked rated. Continue closing the session.
