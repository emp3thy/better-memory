## better-memory

This project uses better-memory for persistent AI knowledge.

### Skills

- Starting work → read `better_memory/skills/memory-retrieve.md`
- Decision point reached → read `better_memory/skills/memory-write.md`
- Validation arrives (evidence in hand) → read `better_memory/skills/memory-feedback.md`
- Session ending → read `better_memory/skills/session-close.md`

### Memory outcomes — the evidence-in-hand rule

Every observation has an `outcome`: `success`, `failure`, or `neutral`.

- **Default to `neutral`** at observe time. Only claim `success` or `failure` when the evidence exists RIGHT NOW (tests ran, approach reverted, user confirmed).
- For decisions whose outcome is not yet provable, write `neutral`, keep the returned id, and close the loop later with `memory.record_use(id, outcome=...)` once validation arrives.
- Record failures at the same cadence as successes — the `dont` bucket depends on it.

### Retrieval buckets

`memory.retrieve` returns `{do, dont, neutral, insights, knowledge}`. Treat `dont` as a hard constraint list: do not repeat approaches that live there.

### MCP tools

- `memory.observe(content, component?, theme?, trigger_type?, outcome?)`
- `memory.retrieve(query?, component?, window?='30d', scope_path?)`
- `memory.record_use(id, outcome?)`
- `knowledge.search(query, project?)`
- `knowledge.list(project?)`

## Session-start reconciliation

After the mandatory better-memory retrieve at session start, call
`memory.reconcile_episodes()` to check for episodes left open by prior
sessions. The tool returns a list of unclosed episodes, each with
`episode_id`, `project`, `tech`, `goal`, and `started_at`.

**For each returned episode**, prompt the user in chat:

> Your prior session left an episode open:
> - goal: "{goal}" (or "background session" if null)
> - project: {project}, tech: {tech or "none"}
> - started: {started_at}
>
> How did it end? (success / abandoned / partial / no_outcome / continuing)

Apply the user's answer via `memory.close_episode(outcome=..., summary=...)`
— EXCEPT for `continuing`, which is a no-op at the service layer (the
episode stays open and subsequent observations bind to it). If the user
ignores the prompt or proceeds without answering, default to `abandoned`
— it still feeds synthesis as a negative signal, so nothing is lost.

**Non-blocking:** do not gate regular work on getting through the
reconciliation queue. Ask about one or two and move on; the Episodes
UI surface (Phase 8+) will eventually let users reconcile in bulk.

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
multi-step plan run) finishes, close the active episode directly:

```
memory.close_episode(outcome="success", close_reason="plan_complete")
```

Do this INSTEAD of the commit trailer if the final commit of the plan
doesn't itself map 1:1 to the plan's goal (e.g. the plan comprises
several logically-separate commits and the final one isn't the
"completion" commit). If the last commit of the plan already carries
`Closes-Episode: true`, the plan-complete call is a no-op (the episode
is already closed) — still safe to call; the drain layer swallows the
no-active-episode ValueError.
