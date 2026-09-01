# GAP / DECISION — Retention runs: rule-based archival vs event expiry

**Status:** converged (2026-07-25, verified against live AgentCore `eu-west-2` acct `708306701628`)
**UI surface:** Diagnostics tab → Retention-runs panel
**Verdict:** hide the panel in agentcore mode; retention is a sqlite-only engine, superseded AWS-side by event expiry.

This is one of the residual **HARD** agentcore-UI cases from
`docs/agentcore-ui/agentcore-mapping.md` (row: _Retention archive/prune of observations_).
It is HARD *and* effectively moot — but unlike the Episodes tab it has no existing capability
gate, so it needs a decision and a small amount of wiring.

---

## 1. What the data is / what the UI does today (sqlite mode)

The **retention engine** is an observation lifecycle GC. It flips stale observations to
`status='archived'` by rule, then optionally hard-deletes long-archived rows. It runs as a
manual/scheduler-driven MCP operation, never from the UI. The UI is a **read-only window** onto
its audit ledger.

The retention engine (`better_memory/services/retention.py`, `RetentionService`) applies three
archive rules atomically inside `SAVEPOINT retention_archive`, then an optional prune:

- **Rule A — `_archive_rule_a_retired_reflection`**: observation linked *only* to `retired`
  reflections, oldest retirement ≥ `retention_days` old.
- **Rule B — `_archive_rule_b_consumed_without_reflection`**: `status='consumed_without_reflection'`
  and `status_changed_at` ≥ `retention_days` old.
- **Rule C — `_archive_rule_c_no_outcome_episode`**: observation's episode has
  `outcome='no_outcome'` and `ended_at` ≥ `retention_days` old.
- **Prune — `_prune`**: hard-`DELETE` archived observations older than `prune_age_days` (default
  365) that have **no** `reflection_sources` rows (sourced rows are kept for the audit trail).
  [Correction: this bullet originally also said prune explicitly deletes the matching
  `observation_embeddings` row; that table was dropped in migration 0018 (remove-ollama-embeddings)
  and `_prune` no longer touches it.]

Each run records one row in the local `retention_runs` ledger via
`RetentionScheduler._record_run` (`services/retention_scheduler.py`), guarded by a 24h
`maybe_run` / `_too_soon` check and triggered from `memory.retrieve` after the spool drain.

The **UI Retention-runs panel** (`GET /diagnostics/panel/retention-runs` →
`fragments/panel_retention_runs.html`, `queries.retention_runs_list_for_ui`) is a pure read:

```sql
SELECT id, run_at,
       archived_via_retired_reflection,
       archived_via_consumed_without_reflection,
       archived_via_no_outcome_episode,
       pruned, triggered_by
FROM retention_runs
ORDER BY run_at DESC, id DESC LIMIT 30
```

Rows are grouped by day; the panel auto-refreshes on load and every 60s (no change-event, since
the UI never writes runs). The UI has **no** button to trigger a run — it only displays history.

## 2. Where it comes from, and why agentcore cannot serve it the same way

**Data sources (all local sqlite today):**

- The **ledger** the panel reads — `retention_runs` — is *operational-state*: it is written by
  `_record_run` on the local `memory.db` and stays local in both modes. Reading it is trivially
  safe in agentcore mode (same as `hook_errors`).
- The **engine that produces the counts**, however, is *content-mutation*: rules A/B/C read the
  `reflection_sources → reflections → episodes` graph and `UPDATE observations SET status='archived'`;
  `_prune` `DELETE`s from `observations`. [Correction: this used to also say "+
  `observation_embeddings`" — that table was dropped in migration 0018
  (remove-ollama-embeddings); see the section-1 correction above.] Every one of those tables is
  AWS-side in agentcore mode.

**Why the engine cannot run against AgentCore (live-API facts):**

1. **No joins.** Rules A and C are graph reads across `reflection_sources → reflections →
   episodes`. AgentCore has no `reflection_sources` link table, and `episodes` do not exist
   server-side (`supports_episodes=False`). There is nothing to join.
2. **Immutable events.** Observations are immutable `CreateEvents` keyed by `actorId+sessionId`,
   carrying only creation-time metadata. There is **no mutable `status` column** — you cannot flip
   an event to `'archived'`. Rules A/B/C have no target to `UPDATE`.
3. **Records have no TTL; only events expire.** Reflections/semantic records under
   `projects/{project}/{reflections,semantic}/` are durable. The only server-side expiry knob is
   `eventExpiryDuration` on the *event* plane: `min 3 / max 365` days, updatable live via
   `UpdateMemory`. In the live instance the **episodic** strategy was bumped `90→365`
   (code default `DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS=90` in `_agentcore_strategies.py` predates the
   live change); **semantic** default is `365` (`DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS`).

**The semantic difference — this is the crux.** sqlite retention is **rule-based archival**: it
selects observations by *lifecycle state* (reflection retired, consumed-without-reflection,
no-outcome episode) and demotes/deletes them regardless of age-alone. AgentCore's mechanism is
**time-based expiry**: an event silently disappears `eventExpiryDuration` days after creation, by
age alone, with no notion of whether it was ever reflected on, consumed, or belonged to a dead
episode. The two are **not equivalent GC policies** — event expiry is coarser (age-only) and
non-selective, but it is the *de-facto* pruning mechanism in agentcore mode. Retention parity is
already a stated **design non-goal** (`docs/superpowers/specs/2026-07-24-agentcore-parity-design.md`,
Non-goals: "Episode/synthesis/retention parity (unchanged agentcore gaps)").

## 3. Options going forward

### Option A — Hide the Retention-runs panel in agentcore mode (RECOMMENDED)

Add a backend capability flag (e.g. `supports_retention`, analogous to the existing
`supports_episodes` on `storage/protocol.py`) returning `False` on `AgentCoreBackend` and `True`
on `SqliteBackend`. Thread it into `create_app`/`base.html` alongside the episodes gate and drop
the retention-runs panel (and its `/diagnostics/panel/retention-runs` route wiring) from the
Diagnostics tab when false. Document event expiry as the agentcore pruning story in the tab or a
tooltip.

- **Pros:** Matches the converged design and the local-vs-content split exactly. The panel would
  otherwise be permanently empty in agentcore mode (`_record_run` never fires, because the engine
  never runs) — showing an always-empty audit panel is actively misleading. Purely additive, one
  boolean; sqlite mode is byte-for-byte unchanged. Consistent with the already-gated Episodes tab.
- **Cons:** Users lose *any* visibility into what is being GC'd in agentcore mode; event expiry is
  invisible (AWS deletes events silently, emitting no ledger row). Mitigated by surfacing the
  configured `eventExpiryDuration` per strategy as static informational text.

### Option B — Repurpose the panel to show configured event-expiry settings

Instead of hiding, replace the run-history table in agentcore mode with a read of the current
`eventExpiryDuration` per strategy (episodic / semantic) via `GetMemory`/`ListMemories`, e.g.
"Episodic events expire after 365 days; Semantic after 365 days."

- **Pros:** Keeps a retention-adjacent panel meaningful; gives the maintainer the one number that
  actually governs agentcore GC; the value is live-updatable via `UpdateMemory` so it is honest.
- **Cons:** It is a *different feature* wearing the same panel — no per-run history, no counts, no
  archival breakdown, because none of that exists AWS-side. Conflates two semantics under one
  heading and risks implying agentcore performs rule-based archival when it does not. More work
  than Option A for marginal value; better delivered as a separate settings/diagnostics readout
  than as the retention panel.

### Option C — Reimplement rule-based retention against a local content mirror

Keep a local shadow of observations/reflections/episodes and run the existing `RetentionService`
against it in agentcore mode, feeding the same `retention_runs` ledger.

- **Pros:** Full feature parity; the panel keeps working unchanged.
- **Cons:** Requires the entire local content-mirror machinery that the parity design explicitly
  rejected. The archival would be a *local fiction* — flipping a mirror row to `'archived'` does
  nothing to the AWS-side event, which still expires purely by `eventExpiryDuration`. Two
  divergent GC policies running in parallel (rule-based locally, time-based AWS-side) with no
  reconciliation. High cost, actively misleading output. **Not worth it.**

### Option D — "We are screwed": accept there is no server-side archival and do nothing

Leave the panel wired but permanently empty in agentcore mode; write no gate.

- **Pros:** Zero code change.
- **Cons:** The panel reads an always-empty `retention_runs` in agentcore mode and silently
  implies "no retention has ever happened," when in fact events *are* being expired by AWS out of
  band. This is the worst of both: it neither serves the feature nor tells the truth. Rejected —
  Option A is a one-boolean fix that removes the lie.

**Recommendation: Option A.** It is the converged design: retention is a sqlite-only rule-based
engine, the panel is content-derived and cannot be reproduced, event expiry (`eventExpiryDuration`,
365d live) is the de-facto agentcore pruning mechanism, and a `supports_retention=False` capability
flag hides the panel exactly as `supports_episodes=False` hides the Episodes tab — leaving sqlite
mode untouched.
