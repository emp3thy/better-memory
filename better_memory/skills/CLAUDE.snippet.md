## better-memory

This project uses better-memory for persistent AI knowledge.

### Skills

- Starting work → read `better_memory/skills/memory-retrieve.md`
- Decision point reached → read `better_memory/skills/memory-write.md`
- Validation arrives (evidence in hand) → read `better_memory/skills/memory-feedback.md`
- Session ending → read `better_memory/skills/session-close.md`

### Retrieval

When you begin a task, call `memory_retrieve` with a `query` describing
it. Do not do broad no-query retrieval at session start; memories
surface contextually as you work.

`memory_retrieve` returns distilled **reflections** — generalised
lessons from prior observations — bucketed by polarity: `do`, `dont`,
and `neutral`. Treat `dont` as a hard constraint list: do not repeat
approaches that live there.

For raw observation lookup — e.g. citing a specific past event or
hunting for an exact prior decision — use `memory_retrieve_observations`
instead. With a free-text query, results are ranked by hybrid search;
without one, they're ordered newest-first.

### Knowledge

At the start of a session, call `knowledge_list` to see what curated
knowledge is indexed for this project. Call `knowledge_search` when the
current task may be covered by curated documentation rather than raw
memory.

### Recording — the evidence-in-hand rule

At a decision point, record with a concise factual summary, an explicit
outcome, and component/theme tags where they apply.

Every observation has an `outcome`: `success`, `failure`, or `neutral`.

- **Default to `neutral`** at observe time. Only claim `success` or
  `failure` when the evidence exists RIGHT NOW (tests ran, approach
  reverted, user confirmed).
- For decisions whose outcome is not yet provable, write `neutral`,
  keep the returned id, and close the loop later with `record_use`
  once validation arrives.
- Record failures at the same cadence as successes — the `dont` bucket
  depends on it.

### Crediting memories you use

When a retrieved memory shapes your work — you quote it, follow its
guidance, or it misleads you — credit it with a one-line evidence
statement when a memory shapes your work. Do this as you go rather
than batching it at session end; anything left uncredited is caught by
the end-of-session sweep, but fresh-context credit is more reliable.

## Session-start reconciliation

After the mandatory better-memory retrieve at session start, check for
episodes left open by prior sessions. Each unclosed episode carries a
goal, project, tech, and start time.

**For each returned episode**, prompt the user in chat:

> Your prior session left an episode open:
> - goal: "{goal}" (or "background session" if null)
> - project: {project}, tech: {tech or "none"}
> - started: {started_at}
>
> How did it end? (success / abandoned / partial / no_outcome / continuing)

Apply the user's answer by closing the episode with that outcome —
EXCEPT for `continuing`, which is a no-op at the service layer (the
episode stays open and subsequent observations bind to it). If the user
ignores the prompt or proceeds without answering, default to `abandoned`
— it still feeds synthesis as a negative signal, so nothing is lost.

**Non-blocking:** do not gate regular work on getting through the
reconciliation queue. Ask about one or two and move on.

The Episodes tab in the management UI also lists unclosed episodes —
clicking a row opens a drawer with the same close actions, useful for
bulk reconcile or follow-up the LLM declined to handle in chat.

The Reflections tab in the same UI lists all reflections for the
current project with filters by tech / phase / polarity / status /
min confidence; clicking a row opens a drawer with the source
observations + their owning episode's outcome, plus actions to
confirm pending reflections, retire stale ones, or edit
`use_cases` and `hints` in place.

## Closing episodes on git commit + plan completion

### Git commits that complete the episode's goal

When you are about to make a commit that **completes the goal of the
currently-active episode**, add this trailer to the commit message:

```
Closes-Episode: true
```

Example:

```
Fix hook-to-drain race condition

Closes-Episode: true
```

The post-commit hook (if installed — see `docs/hooks-setup.md`) writes a
spool marker; SpoolService.drain closes the active episode as
`outcome=success`, `close_reason=goal_complete` on the next drain.

**Only add the trailer when the commit actually ends the goal.** Normal
mid-plan commits, review-fix commits, and WIP commits should NOT carry
the trailer — the episode stays open and continues to accrue
observations across later commits.

Truthy values: `true`, `yes`, `1` (case-insensitive). Anything else,
including absence, is a no-op.

### Plan-complete close

When the `superpowers:executing-plans` workflow (or any equivalent
multi-step plan run) finishes, close the active episode directly with
outcome `success` and close reason `plan_complete`.

Do this INSTEAD of the commit trailer if the final commit of the plan
doesn't itself map 1:1 to the plan's goal (e.g. the plan comprises
several logically-separate commits and the final one isn't the
"completion" commit). If the last commit of the plan already carries
`Closes-Episode: true`, the plan-complete call is a no-op (the episode
is already closed) — still safe to call.
