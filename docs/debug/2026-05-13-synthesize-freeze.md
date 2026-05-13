# Synthesize skill freeze — investigation log (2026-05-13)

## Symptom

When the user runs the `better-memory-synthesize` skill from Claude Code (via `/synthesis` or the "synthesize" keyword), it freezes — most recently for 8 minutes on the latest run, and once for ~4 hours on a prior run. Running the underlying MCP tools (`memory.synthesize_next_get_context` / `_apply`) directly from another session works fine.

## What we know

- Queue size at start: **43 pending episodes** across 4 projects (8 better-memory, 26 general, 7 nuke, 2 weatherToBattery). The skill processes only the project the MCP server resolves — currently `general`.
- MCP server is healthy. Each `get_context` / `apply` call completes in single-digit ms.
- No rows in `hook_errors`. No recent `synth_failed_at`.
- Pre-existing `synth_failed_at` stamps are from 2026-05-04 and unrelated.

## Repro this session

Re-ran the synthesize tools manually:
- `_get_context` → returned an episode in ~50 ms (with old code; new code added timing).
- `_apply` (all-ignore for that episode) → returned `ok:true`, `pending` decremented 27 → 26.

Both worked. So the MCP tools are not the blocker.

## Smoking gun from the audit log

After adding logging (see Changes section) and asking the user to re-run the frozen session, `~/.better-memory/logs/synthesize.jsonl` contained exactly **one** call pair across ~8 minutes:

```
08:32:22.730  start    call_id=faac9083f4b6  get_context  project=general
08:32:22.734  complete call_id=faac9083f4b6  get_context  →
              episode_id=722103110b96497f8c037c4e9be98c12
              result_kind=episode, obs_count=0, refl_count=4, latency_ms=4
```

The MCP server returned in 4 ms. There is **no subsequent `apply` call and no further `get_context`**. The driving LLM session has been silent for 8 minutes after receiving a trivial response.

The specific episode:
- `id`: `722103110b96497f8c037c4e9be98c12`
- `project`: general
- `goal`: "Implement Phase 2.5 Task 2: Add GameState.lastOrders field, isHuman() helper, and seed lastOrders in initialState. TDD order."
- `tech`: typescript
- `outcome`: success, `close_reason`: goal_complete
- **0 active observations**

The correct decision for an empty-observation episode is `{"new":[], "augment":[], "merge":[], "ignore":[]}` — trivial.

## Diagnosis

The freeze is on the LLM side, not in better-memory. Most likely cause: the skill rubric framed even a 0-observation episode as deep work (a mandatory merge scan + per-observation analysis), and the LLM spirals on what should be a no-op decision. There is no fast path for empty observations, no per-call thinking budget, and no drain cap, so a 26-episode queue gives many opportunities for the model to deliberate.

The 4-hour prior freeze likely had the same shape: full-queue drain + heavyweight rubric per episode + no early-exit hints.

## Changes made

### 1. Audit log (so we can see what happened)

`better_memory/mcp/server.py`:
- Added `_append_synth_audit(home, payload)` — appends one JSON line to `{config.home}/logs/synthesize.jsonl`, best-effort (swallows IO errors, logs via module logger).
- Added `_audit_synth_call(home, *, tool, project, episode_id)` context manager — writes a `start` row at entry and a `complete` row at exit, paired by a short `call_id` (uuid4 prefix). Complete row includes `latency_ms`, `result_kind`, `error`, plus tool-specific fields (`obs_count` / `refl_count` for get_context; `counts` for apply). Re-raises on exception.
- Both `memory.synthesize_next_get_context` and `memory.synthesize_next_apply` handlers are wrapped.

`tests/mcp/test_synth_audit_log.py` — 8 unit tests covering the writer + context manager (success, exception, validation/state early-return, multiple calls, IO failure).

Result: every synthesize tool call now leaves two JSON rows. A `start` with no matching `complete` (same `call_id`) localises the hang. `latency_ms` on completes lets us spot outliers.

### 2. SKILL.md edits — `.claude/skills/better-memory-synthesize/SKILL.md`

Two edits to address the LLM-side spiral:

(a) **Fast path for empty observations** — new section at the top of "Decision rubric":
> "If the episode's `observations` array is empty, the decision is `{"new":[], "augment":[], "merge":[], "ignore":[]}`. Submit it immediately, with no further analysis — no merge scan, no rubric, no reasoning. … Skip directly to the apply call."

(b) **Softened Step 0** (merge scan) — replaced "the skim is non-optional" with "skim briefly; trust your first read; missing a merge is cheap" and removed the cross-episode urgency framing. Matching "Common pitfalls" entry updated from "Skipping the merge scan" (mandate) to "Over-deliberation on the merge scan" (warning against the actual failure mode).

Parked for now (option 3+4 from the proposal — apply if 1+2 don't fix it):
- **Drain budget** — cap each `/synthesis` invocation at N (5–10) episodes, then end and ask the user to re-run.
- **Output discipline** — explicit "produce the JSON quickly; do not deliberate at length per episode."

## How to retest in the next session

1. Cancel any frozen `/synthesis` session — `apply` was never called for the stuck episode, so the queue state is unchanged (`synthesized_at` is still NULL on `722103110b96497f8c037c4e9be98c12`). Cancellation is clean.
2. Start a fresh Claude Code session in this repo and trigger `/synthesis` (or type "synthesize").
3. Skills are read from disk per `Skill` invocation, so **no Claude restart is needed**. The MCP server picked up the audit-log code when the user restarted it earlier this session.
4. Watch `~/.better-memory/logs/synthesize.jsonl` (e.g. `Get-Content -Wait` on Windows). Expectations:
   - Empty-observation episodes complete the get_context → apply round-trip in tens of ms.
   - Non-empty episodes should still complete in seconds, not minutes.
   - If `synthesize.jsonl` shows a `start` row with no matching `complete`, the hang is still at the LLM. Read the episode_id + goal to characterise it.
   - If complete rows exist but `latency_ms` is enormous, the MCP server itself is the bottleneck — re-investigate.

## Open questions

- Why the MCP server's resolved project is `general` rather than the cwd-derived `better-memory` — likely a process-launch-cwd issue. The drain only touches the project the server resolves; the 8 `better-memory` / 7 `nuke` / 2 `weatherToBattery` pending episodes never get reached even when the skill runs to completion on `general`.
- Whether the LLM is hanging in extended thinking specifically (could a tighter skill prompt forbid extended thinking for this skill?).
- Whether very long get_context responses (full-tech-null episodes can include up to ~56 reflections in one payload) contribute to slowdown on top of the empty-observation case.

## Related files

- `better_memory/mcp/server.py` — `_append_synth_audit`, `_audit_synth_call`, handler wiring (lines ~104–180, ~1252–1320).
- `.claude/skills/better-memory-synthesize/SKILL.md` — fast path + softened Step 0.
- `tests/mcp/test_synth_audit_log.py` — coverage for the audit helper.
- `better_memory/services/reflection.py` — `_pick_oldest_pending`, `get_next_pending_context`, `apply_decision`.
- Audit log: `~/.better-memory/logs/synthesize.jsonl`.
