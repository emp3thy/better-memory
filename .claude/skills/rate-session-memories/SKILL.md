---
name: rate-session-memories
description: Use when a session is about to end and the LLM sees a RATE_MEMORIES directive in additionalContext. Also use when the user explicitly asks to rate this session's memories.
---

# Rate Session Memories

You are about to classify the memories exposed in THIS session that have NOT
already been credited via `memory.credit` mid-session.

## STEP 1 — Refresh the list

Read the `Session:` line from the RATE_MEMORIES directive (its second line,
e.g. `Session: <id>`). If present, call `memory.list_session_exposures` with
`{"session_id": "<that id>"}`. If the directive has no `Session:` line (older
hook), call `memory.list_session_exposures` with no arguments — the server
resolves the current session from env, as before.

Read the returned list. This is the ONLY valid set of ids to rate. The list
in the RATE_MEMORIES directive may have been truncated.

## STEP 2 — Evidence first, then classify

For each `(kind, id)`: FIRST try to write ONE line of evidence — what the
memory changed in this session, or a quote of where you used it. Then:

- Evidence line written → choose `cited` (quoted/directly referenced),
  `shaped` (guided a decision), `misled` (sent you wrong), or `overlooked`
  (user had to point you back to it). Include the evidence line in the
  rating.
- No evidence line possible → the class is `ignored`. Full stop. Do not
  reverse the order; choosing a class first and rationalising evidence
  after is how ratings drift.

Rules:
- Quote the id in your reasoning so you can't drift.
- Do not invent ids. Do not skip ids.
- If genuinely uncertain between two EVIDENCED classes, prefer the weaker
  claim (shaped over cited). `misled` is never a fallback. Once an
  evidence line exists, `ignored` is no longer available — uncertainty
  about class never reverses the evidence decision.
- `overlooked` is never a fallback. Use it ONLY when the user explicitly
  pointed you back to a memory you already had and had not applied —
  that user intervention is the observable anchor. Test for it first,
  separately from the cited/shaped/ignored axis. No anchor event → not
  `overlooked`.

## STEP 3 — Submit ALL ratings in ONE call

Call `memory.apply_session_ratings` with the same `session_id` from STEP 1
alongside `ratings` (omit `session_id` if the directive had no `Session:`
line — the server then resolves it as before):

```json
{
  "session_id": "<that id>",
  "ratings": [
    {"kind": "reflection", "id": "r-abc...", "class": "cited",
     "evidence": "Used its junit-xml flag verbatim in the pytest invocation."},
    {"kind": "semantic",   "id": "s-def...", "class": "ignored"}
  ]
}
```

ONE call. All ratings. Malformed batches (empty, unknown kind/class, duplicate ids) will be rejected; individual id mismatches return in `skipped`.

## STEP 4 — Verify

The tool returns `{applied: {...}, skipped: {...}}`. If `skipped.not_exposed > 0`,
the server dropped ids that weren't actually exposed — don't retry; it just
means you classified an id that wasn't in the authoritative list.

The session is now marked rated. Continue closing the session.
