---
name: better-memory-synthesize
description: Use when the user asks to synthesize, consolidate, distill, or process pending episodes in better-memory. Also use proactively when memory.start_episode reports pending_synthesis.pending > 0. Drives the per-episode reflection synthesis loop via the memory.synthesize_next_get_context / _apply MCP tools.
---

# Synthesizing better-memory episodes

## When to use

- User asks to "synthesize", "consolidate", "distill", "process episodes", "drain the synthesis queue", or similar
- `memory.start_episode` returns `pending_synthesis.pending > 0`
- User mentions stale reflections, drift, or stuck queue
- After completing a major piece of work, before closing a session, if pending > 0

## What synthesis does

better-memory captures observations during sessions and groups them by episode. When an episode closes, its observations sit in a pending queue until consolidation. Synthesis distills observations into **reflections** — durable lessons surfaced in future `memory.retrieve` calls.

The unit is one episode at a time (1-5 observations, ~1-2 KB of context). You decide what to do with the observations and submit a decision JSON. The apply layer is atomic per episode.

## The two-tool workflow

1. **`memory.synthesize_next_get_context`** — fetch the next pending episode's full context. Returns `{episode_id: null, queue: {...}}` when the queue is empty (this is the loop terminator).
2. **`memory.synthesize_next_apply`** — submit your decision JSON for one episode. Atomically applies actions, marks the episode synthesized, and returns the resulting `{counts, queue}` snapshot.

Loop until `episode_id` is `null`.

## The decision schema

Your decision is a single JSON object with FOUR top-level keys, ALL REQUIRED:

```json
{
  "new":     [],
  "augment": [],
  "merge":   [],
  "ignore":  []
}
```

Use `[]` for any category with no real entries. **DO NOT invent entries to mimic the example shapes below — illustrative only.**

### `new` entries (fields ALL REQUIRED)

A new reflection — a generalizable lesson the existing reflections don't already cover.

```json
{
  "title": "Short, imperative lesson title",
  "phase": "planning" | "implementation" | "general",
  "polarity": "do" | "dont" | "neutral",
  "use_cases": "When this lesson applies; one or two sentences",
  "hints": ["concrete actionable hint 1", "hint 2"],
  "tech": "python" | "javascript" | ... | null,
  "confidence": 0.1..1.0,
  "source_observation_ids": ["o-abc123"]
}
```

- `tech: null` if the lesson is language-agnostic.
- `source_observation_ids` MUST be actual ids from the OBSERVATIONS list in the get_context payload. Not placeholders.
- `polarity`: `do` = encourage; `dont` = warn against; `neutral` = reference / context only.
- `confidence`: 0.3-0.5 for one-off observations; 0.6-0.8 for confirmed patterns; 0.9+ only with strong evidence across episodes.

### `augment` entries (fields ALL REQUIRED)

When an EXISTING reflection (in the `reflections` array of get_context) gains new evidence from this episode:

```json
{
  "reflection_id": "r-existing-id",
  "add_hints": ["new hint based on this episode's evidence"],
  "rewrite_use_cases": null,
  "confidence_delta": 0.1,
  "add_source_observation_ids": ["o-def456"]
}
```

- `rewrite_use_cases: null` to leave existing text unchanged. Supply full replacement text only when the existing wording is genuinely too narrow.
- `confidence_delta`: small positive (0.05-0.2) when episode confirms; small negative when mixed/contradictory; 0.0 to leave confidence alone.

### `merge` entries (fields ALL REQUIRED)

Use to combine TWO existing reflection ids that name the SAME lesson OR are complementary aspects of the same use case (see "Merge criteria" below). Both ids MUST appear in the `reflections` array of the current `get_context` response. **NEVER emit a merge entry with null, empty, or invented ids.**

```json
{
  "source_id": "r-narrower",
  "target_id": "r-broader",
  "justification": "Why these belong as one reflection, ~1 sentence"
}
```

`source` is superseded into `target`. Target absorbs source's observation links and stays active. Source becomes status='superseded'.

**Merge criteria — when to combine:**

- **Same lesson, different framing** — both reflections describe the same fact/rule with different words. Strong merge.
- **Complementary lessons, shared use case** — reflection B is a required step / special case / corollary of reflection A, and they trigger together (same `use_cases`). Example: "Bridge async via worker thread + new_event_loop" + "Worker-thread bridge must wrap entire body in try/except" — one is HOW to do the pattern, the other is a required correctness step OF that pattern. Merge: target the broader one, fold the narrower's hints in.
- **Special case of a general rule** — reflection B names a specific instance of the general rule in reflection A. Example: "Use getattr for platform-only stdlib attributes" + "Windows DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP needs only these flags" — the second is a worked example of the first.

**Anti-patterns — when NOT to merge:**

- **Same domain, different mechanisms** — three distinct HTMX gotchas (event bubbling vs non-2xx responses vs hx-swap=none) share the tech tag but have different triggers, different fixes, and different failure modes. Keep separate.
- **Same library, unrelated lifecycles** — e.g. mkdocs `| url` filter behavior vs mkdocs `md_in_html` blank-line requirement. Same library, but a developer hits one or the other, never both at once.
- **Generic / parent reflection** — don't merge a specific lesson INTO a vague catch-all (e.g. don't merge anything into "Prioritize Root Cause Fixing"). Better to retire the vague one separately.
- **One is `do`, the other is `dont` on the same axis** — keep separate; the polarity carries information for retrieval.

When in doubt, prefer NOT to merge — it's easier to merge later than to split a bloated reflection.

### `ignore` is an array of observation id strings

For observations that are ephemeral, episode-specific, or not worth distilling:

```json
"ignore": ["o-noise789", "o-routine-task-456"]
```

Observations you don't address (neither in `new`, `augment`, nor `ignore`) are auto-ignored at apply time. So `ignore` is for explicit non-actionable observations; you can leave routine ones out entirely if convenient.

## Decision rubric

**Step 0 — scan the existing `reflections` array for merge candidates BEFORE looking at this episode's observations.**

The `get_context` response gives you a tech-filtered slice of all active reflections. Skim titles + use_cases for clusters that should be one reflection (see "Merge criteria" above). Each call is your only opportunity to merge the pairs that appear together — `merge` cannot operate cross-episode.

Don't force merges every call. Most calls will produce zero merge entries. But the skim is non-optional.

**Step 1 — for each observation in the episode, ask:**

1. **Is this a generalizable lesson worth surfacing in future sessions?**
   - Yes, and it's NEW → `new`
   - Yes, and it CONFIRMS / EXTENDS an existing reflection → `augment`
   - No (ephemeral, routine, or episode-specific) → omit (auto-ignored) or list in `ignore`

2. **Is an existing reflection too narrow given new evidence?**
   - Yes → `augment` with `rewrite_use_cases`

3. **Before adding a `new` entry**, confirm no existing reflection (in the response's `reflections` array) covers the same use case. If it does, `augment` instead — and consider whether your would-be `new` is actually evidence that two existing reflections should `merge`.

## Worked example

`memory.synthesize_next_get_context` returns:

```json
{
  "episode_id": "ep-42",
  "queue": {"pending": 8, "in_cooldown": 0, "done": 3},
  "episode": {
    "id": "ep-42",
    "project": "myapp",
    "goal": "Fix flaky CI on Linux",
    "tech": "python",
    "outcome": "success"
  },
  "observations": [
    {"id": "o-1", "content": "Pinning pytest-asyncio to ~=1.3 fixed test collection failure on 3.12.", "tech": "python", "outcome": "failure", ...},
    {"id": "o-2", "content": "Ran `git status` before pushing.", "outcome": "neutral", ...}
  ],
  "reflections": [
    {"id": "r-existing", "title": "Pin major versions of pytest plugins", "polarity": "do", "tech": "python", ...}
  ]
}
```

`o-1` is a generalizable lesson AND there's already a related reflection → `augment`.
`o-2` is routine workflow → `ignore` (or omit; auto-ignore would catch it).

Submit:

```
memory.synthesize_next_apply(
  episode_id="ep-42",
  decision={
    "new": [],
    "augment": [{
      "reflection_id": "r-existing",
      "add_hints": ["pytest-asyncio specifically required ~=1.3 on Python 3.12"],
      "rewrite_use_cases": null,
      "confidence_delta": 0.1,
      "add_source_observation_ids": ["o-1"]
    }],
    "merge": [],
    "ignore": ["o-2"]
  }
)
```

Result:

```json
{
  "ok": true,
  "episode_id": "ep-42",
  "counts": {"created": 0, "augmented": 1, "merged": 0, "ignored": 1, "auto_ignored": 0},
  "queue": {"pending": 7, "in_cooldown": 0, "done": 4}
}
```

Then loop: call `synthesize_next_get_context` again for the next episode.

## Drain pattern

```
loop:
  result = memory.synthesize_next_get_context()
  if result.episode_id is null:
    report "queue empty" and stop
  decision = decide_for_episode(result)
  apply_result = memory.synthesize_next_apply(
    episode_id=result.episode_id,
    decision=decision,
  )
  if not apply_result.ok:
    # Validation error — fix the decision JSON and retry the same episode
    log apply_result.message
    retry once with corrections; if still failing, submit an
    all-ignore decision to mark the episode processed and move on
```

## Common pitfalls

- **Inventing entries** to mimic the example schema. Use `[]` when nothing applies.
- **Placeholder ids** in `source_observation_ids`. They MUST be the actual ids from the OBSERVATIONS list of the episode you're processing.
- **Cross-episode merging**: `merge` only operates on existing reflections shown in the same response's `reflections` array. Don't try to merge against ids you remember from earlier in the loop — they may be retired.
- **Augmenting retired reflections**: the apply layer drops them silently. The reflections array won't include `retired` / `superseded` reflections so this should not occur, but if you ever cache ids across iterations, a previously-merged source may now be `superseded`.
- **Over-confident new entries**: a single observation rarely warrants `confidence > 0.7`.
- **Sycophantic ignore**: don't dump everything into `ignore` to clear the queue. The point is to extract real lessons. If you genuinely cannot find a lesson, ignore is correct — but think first.
- **Skipping the merge scan**: the `reflections` array isn't just for cross-checking your `new` entries — it's the only place you can act on accumulated similarity drift. Skim it every call.
- **Over-merging**: don't collapse three distinct gotchas into one bloated reflection because they share a tech tag. Different mechanism + different fix = keep separate. See "Anti-patterns" under merge criteria.

## Reporting

After draining (or stopping), tell the user:
- How many episodes you processed
- Cumulative counts: created N reflections, augmented N, merged N, ignored N observations
- Whether the queue is now empty or how many remain
