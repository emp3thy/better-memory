# Episodic synthesis — design

**Status:** Approved 2026-05-03
**Branch target:** new feature branch off `main` (name TBD, e.g. `episodic-synthesis`)
**Predecessor:** synth-bridge sub-design B (`2026-05-02-synthesis-route-hardening-design.md`) — landed in PR #18 — which hardened the worker/timeout/mutex around the existing batch synthesize. This redesign replaces the *batch* itself.

## Goal

Replace the watermark-driven batch synthesis (`ReflectionSynthesisService.synthesize` feeding all eligible observations to one LLM call) with an episode-driven loop: each closed episode is a single, atomic synthesis step. The UI's `/observations/synthesize` route becomes "process one episode," chained by an htmx self-firing fragment until the pending queue is empty.

## Why now

A real-world synthesis on `better-memory`'s own DB tonight failed three times in a row. The diagnostic chain (recorded for memory): the prompt for 67 closed-episode observations is ~14 379 tokens; `llama3:8B`'s default ctx is 8 192; ollama silently truncates the second half of the prompt (which includes the JSON-shape instructions); the truncated prompt produces an unparseable response that `format=json` constrained-decoding spends minutes thrashing on; the per-call httpx timeout (60 s) closes the connection; OllamaChat retries 3×, each retry hits the same wall; the route's 180 s `synth_timeout` fires; user sees `504 Synthesis timed out`. **No reflection ever lands.** This isn't a model-speed problem — it's that the unit of synthesis is wrong. The natural unit is one episode (1-5 observations, 1-2 KB prompt, fits any model trivially), not "every active observation since the watermark."

A band-aid landed alongside this design (default `OllamaChat` timeout bumped 60 → 240 s, `CONSOLIDATE_MODEL=llama3.2:3b` for the user's UI process) but the user explicitly held off running synthesis until the redesign is in place — they don't want batch synth to run again, period.

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Per-episode tracking | New column `episodes.synthesized_at TIMESTAMP NULL` | Cleanest. `WHERE outcome IS NOT NULL AND synthesized_at IS NULL` directly enumerates pending work; resumability is free; one partial index keeps the lookup cheap. Alternative join table was over-engineered for current needs. |
| Watermark table fate | `DROP TABLE synthesis_runs` in migration 0006 | Per-episode tracking supersedes its entire purpose. `last_run_at` is derivable from `MAX(synthesized_at)` if ever needed; `last_goal` becomes meaningless (each episode has its own goal). The 10-min same-goal short-circuit becomes vestigial: if every closed episode has `synthesized_at IS NOT NULL`, there's nothing to do — the natural empty-queue case. |
| Failure split | LLM-class errors (`ChatError`, `SynthesisResponseError`) → ROLLBACK, stamp `synth_failed_at`, return `SynthesisStep` with `failure` set, chain continues. DB / structural / unexpected → ROLLBACK, propagate to route which surfaces 500 and stops the chain. | Per-episode resilience is the whole motivation: one slow LLM call can't poison the rest. But a structural failure (IntegrityError on `reflection_sources`) means the schema invariant is broken; continuing would just produce more bad rows. `asyncio.TimeoutError` is dropped from the catch — httpx wraps timeouts as `TransportError` and `OllamaChat` already converts those into `ChatError` after retry exhaustion. |
| Failure cooldown | Add column `episodes.synth_failed_at TIMESTAMP`. `_pick_oldest_pending` excludes episodes where `synth_failed_at > datetime('now', '-300 seconds')`. On LLM error, `UPDATE episodes SET synth_failed_at = now() WHERE id = ?`. | Without this, a persistently-failing episode is the oldest-pending forever — MCP's drain loop spins infinitely on it; UI auto-fire shows the same failure card 67 times before reaching pending=0. The 300 s cooldown excludes a just-failed episode for the rest of the current chain (a 67-episode run is ~10 min, but cooldown only needs to outlast the per-call duration). Next user click after the cooldown elapses retries the failure naturally. |
| HX-Trigger cadence | `observations-synthesized` fires on every successful step (and on failure-with-progress steps). Empty-queue / done steps still fire it so the panel does a final reconcile. | Live progress IS the UX — watching the obs panel deplete every ~10s is exactly the per-episode progress signal. 67 panel reloads over ~10 min costs ~1-2 s of total render work; trivial. The alternative (fire only at chain-end) leaves the user staring at an unchanging panel for 10 min. |
| Counts consistency | Service returns a single `EpisodeQueueCounts(done, pending, in_cooldown)` triple alongside `SynthesisStep` (or as a field on it), computed from one connection in one statement. Route uses these directly, never derives from a second source. | Original spec had route compute `done` from its own connection while service returned `pending_remaining` from the worker connection. Two reads, two connections, race window if a commit lands between them — reviewer would flag inconsistency. Single-source eliminates the issue. |
| In-UI cancellation | Out of v1. Closing the tab stops the auto-fire chain (the auto-fire div is no longer in the DOM). The next user click resumes from the next pending episode. | A 5-15 min chain doesn't need an in-UI stop button for v1; the cooldown machinery + tab-close interruption are sufficient. Future v2 can add a server-side cancellation flag + a chain-id query param if needed. |
| Reflection scope per prompt | Tech-filtered: `reflection.tech == episode.tech OR reflection.tech IS NULL` | Mirrors the existing `load_context` semantic so behavior at the reflection level is preserved — only the *batching* changes. Naturally bounded by tech; cap-at-N is a future YAGNI. |
| Observations included in prompt | All observations for the episode, regardless of `status` | Per-episode synthesis is "what happened in *this episode*, holistically." Feeding only `active` observations would show a partial story. The apply layer respects existing status (already-consumed observations don't get re-flipped; `INSERT OR IGNORE` dedups source links). |
| Iteration order | Oldest `ended_at` first | Later episodes naturally see reflections that earlier-episode steps just produced — this is exactly how cross-episode merging emerges. No need for a separate cross-batch synthesis pass. |
| Run shape / progress UX | Auto-chained per-episode: one HTTP request = one episode; response banner contains an `hx-trigger="load delay:200ms"` element iff `pending_remaining > 0`; chain stops naturally when pending is 0 | Pure htmx idiom. Live progress, no long-held HTTP connection, trivial to interrupt (close tab). The mutex now means "one episode at a time" rather than "one batch at a time" — the same lock primitive is reused. |
| Service API | New method `synthesize_next(*, project: str) -> SynthesisStep`; existing `synthesize(*, goal, tech, project)` removed | Step-shaped API matches the route 1:1. The MCP "drain everything" caller becomes a 4-line `while`-loop. Drops `goal` and `tech` from the synthesis API entirely — they were never about synthesis; they were context for a now-per-episode prompt. |
| Backfill strategy | `UPDATE episodes SET synthesized_at = ended_at WHERE outcome IS NOT NULL AND id NOT IN (SELECT DISTINCT episode_id FROM observations WHERE status='active')` | The accurate read of current ground truth: if every observation in an episode has been consumed, the episode has effectively been consolidated. Closed episodes with leftover active observations get one fresh look from the new design. Avoids stranding the 99 active observations on the user's DB. |
| Reflection / observation scope | Add `scope TEXT NOT NULL DEFAULT 'project' CHECK(scope IN ('project','general'))` to both `observations` and `reflections`. Retrieval queries OR-merge `(project = ? OR scope = 'general')`. Synthesis derives a new reflection's scope from source observations: all-general → general, otherwise project. Augment preserves the existing reflection's scope. | The user's workflow rules (e.g. "always assign per-step confidence to a superpowers plan") are project-agnostic and currently get hidden behind project scope. A general scope makes them surface in every project's `memory_retrieve`. Synthesis propagation means user only marks observations general at write time; the rest flows automatically. |
| `EpisodeForPrompt.project` field | Carry `project: str` on `EpisodeForPrompt` (populated from `_pick_oldest_pending`'s row), so `_load_episode_context` reads project directly instead of via subquery `(SELECT project FROM episodes WHERE id = ?)`. | Removes an awkward subquery, simplifies the SQL, and makes the dataclass self-sufficient. Mitigates the original Task 7 confidence dip. |
| `ObservationForPrompt.status` default | Make the new `status` field default to `'active'` in the dataclass. | Lets the (about-to-be-deleted) `load_context` keep working without modification through Task 11; Task 12 removes it whole. Eliminates the dual-state edit Task 5 originally required. |
| PR shape | Single PR; five commits (migration → service → route → MCP → scope) | Migration 0006 + service refactor + route + MCP + migration 0007 / scope are tightly coupled — they share the migration sequence and touch the same retrieval surface. Splitting risks a half-migrated codebase. Reviewer sees the whole change in one diff. |

## Approach

Single branch, five logical commits:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `feat(db): migration 0006 — episodes.synthesized_at + drop synthesis_runs` | `better_memory/db/migrations/0006_per_episode_synthesis.sql` (new), `tests/db/test_migration_0006.py` (new) | Feature |
| 2 | `refactor(reflection): replace batch synthesize with per-episode synthesize_next` | `better_memory/services/reflection.py`, `tests/services/test_reflection.py` | Refactor |
| 3 | `refactor(ui): /observations/synthesize is one-step + auto-chain banner` | `better_memory/ui/app.py`, `better_memory/ui/templates/fragments/synth_step_banner.html` (replaces `observations_synth_banner.html`), `tests/ui/test_observations.py` | Refactor |
| 4 | `refactor(mcp): start_episode drains pending then fetches buckets` | `better_memory/mcp/server.py`, `tests/mcp/test_episode_tools.py` | Refactor |
| 5 | `feat(scope): general-scope reflections surface across projects` | `better_memory/db/migrations/0007_reflection_scope.sql` (new), `tests/db/test_migration_0007.py` (new), `better_memory/services/observation.py`, `better_memory/services/reflection.py`, `better_memory/mcp/server.py`, `tests/services/test_observation.py`, `tests/services/test_reflection.py`, `tests/mcp/test_*` | Feature |

Commits are dependency-ordered: each one assumes the previous landed. CI (pyright + pytest) green at each commit boundary.

## Commit 1 — Migration 0006

`better_memory/db/migrations/0006_per_episode_synthesis.sql`:

```sql
-- 1. Add per-episode tracking column.
ALTER TABLE episodes ADD COLUMN synthesized_at TIMESTAMP;

-- 2. Add per-episode failure-cooldown column.
ALTER TABLE episodes ADD COLUMN synth_failed_at TIMESTAMP;

-- 3. Partial index for "find next pending episode".
--    Cheap: only indexes rows where outcome IS NOT NULL AND synthesized_at IS NULL.
--    The cooldown filter is applied at SELECT-time (datetime('now') is non-deterministic
--    so it can't live in the partial-index predicate).
CREATE INDEX idx_episodes_pending_synth
    ON episodes(project, ended_at)
    WHERE outcome IS NOT NULL AND synthesized_at IS NULL;

-- 4. Backfill: episodes whose observations are ALL non-active are "done".
--    Episodes with any leftover active observation stay NULL → next synth picks them up.
UPDATE episodes
   SET synthesized_at = ended_at
 WHERE outcome IS NOT NULL
   AND id NOT IN (
       SELECT DISTINCT episode_id
         FROM observations
        WHERE status = 'active'
   );

-- 5. Drop the watermark table — superseded by per-episode tracking.
DROP TABLE synthesis_runs;
```

Migration test (`tests/db/test_migration_0006.py`) seeds:

- closed episode A: 2 observations both `consumed_into_reflection` → `synthesized_at` set to `ended_at`
- closed episode B: 2 observations, 1 `active` 1 `consumed_*` → `synthesized_at` stays NULL
- closed episode C: 0 observations → `synthesized_at` set to `ended_at`
- open episode D: outcome NULL → `synthesized_at` stays NULL
- a `synthesis_runs` row → assert `SELECT COUNT(*) FROM sqlite_master WHERE name='synthesis_runs'` is 0 after migration
- new column `synth_failed_at` exists, all rows NULL after migration
- backfill correctness invariant: `count(closed episodes with no active observations) == count(closed episodes with synthesized_at IS NOT NULL)` after migration

Forward-only test (no down-migration). The schema migration runner is `db/schema.py:apply_migrations` — already lives.

**Test cleanup in same commit** (`tests/db/test_schema.py`):

- `test_synthesis_runs_exists` — delete (table is gone)
- `test_synthesis_runs_composite_pk` — delete
- `test_synthesis_runs_has_last_goal_column` — delete
- `test_synthesis_runs_last_goal_round_trips` — delete
- New: `test_episodes_has_synthesized_at_column`
- New: `test_episodes_has_synth_failed_at_column`
- New: `test_idx_episodes_pending_synth_partial_index_exists`

**Pre-flight grep** before merging commit 1: `grep -r 'synthesis_runs' better_memory/ tests/` must return only references inside the migration files themselves and the strings inside `tests/db/test_migration_0006.py`. Any other hit means a stale caller wasn't removed in commit 2.

## Commit 2 — Service refactor

`better_memory/services/reflection.py`:

**Removed:**

- `ReflectionSynthesisService.synthesize(goal, tech, project)` — replaced.
- `ReflectionSynthesisService.load_context(project, tech)` — folded into `_load_episode_context`.
- `ReflectionSynthesisService.build_prompt(goal, tech, context)` — replaced by `_build_episode_prompt`.
- `ReflectionSynthesisService._should_short_circuit(...)` — gone; empty-queue is the natural short-circuit.
- `ReflectionSynthesisService._upsert_watermark(...)` — gone; per-episode `synthesized_at` UPDATE replaces it.
- The `SynthesisContext` dataclass — replaced by `EpisodeContext` (new).

**Added:**

```python
@dataclass(frozen=True)
class EpisodeForPrompt:
    id: str
    project: str    # NEW — populated from the row, removes load_context's subquery
    goal: str
    tech: str | None
    outcome: str

@dataclass(frozen=True)
class ObservationForPrompt:  # gains 'status' field; otherwise unchanged
    id: str
    content: str
    outcome: str
    component: str | None
    theme: str | None
    tech: str | None
    created_at: str
    status: str = "active"  # NEW — defaults to 'active' so the existing load_context call site doesn't need updating before being deleted in Task 12

@dataclass(frozen=True)
class EpisodeContext:
    episode: EpisodeForPrompt
    observations: list[ObservationForPrompt]  # ALL observations for the episode
    reflections: list[ReflectionForPrompt]    # tech-filtered: tech == episode.tech OR tech IS NULL

@dataclass(frozen=True)
class EpisodeQueueCounts:
    """Single-source-of-truth queue counts, computed from one connection."""
    done: int                            # COUNT(*) WHERE outcome IS NOT NULL AND synthesized_at IS NOT NULL
    pending: int                         # COUNT(*) eligible-now (synthesized_at NULL AND not in cooldown)
    in_cooldown: int                     # COUNT(*) failed within last 300 s
    @property
    def total(self) -> int: return self.done + self.pending + self.in_cooldown

@dataclass(frozen=True)
class SynthesisStep:
    processed: bool                      # False ⇔ no pending episode (queue empty or all in cooldown)
    episode_id: str | None               # which episode was processed (None if processed=False)
    counts: dict[str, int]               # this-step counters: created/augmented/merged/ignored/auto_ignored
    queue: EpisodeQueueCounts            # post-step queue snapshot, single connection, single moment
    failure: str | None                  # set if LLM-class error was caught

class ReflectionSynthesisService:
    async def synthesize_next(self, *, project: str) -> SynthesisStep:
        """Process the oldest closed-but-unsynthesized episode for project. One LLM call."""
        ...

    def _pick_oldest_pending(self, project: str) -> EpisodeForPrompt | None: ...
    def _load_episode_context(self, episode: EpisodeForPrompt) -> EpisodeContext: ...
    def _build_episode_prompt(self, ctx: EpisodeContext) -> str: ...
    def _mark_synthesized(self, episode_id: str) -> None: ...
    def _count_pending(self, project: str) -> int: ...
```

**Survives unchanged:**

- `parse_response(raw)` — same JSON shape; LLM still emits new/augment/merge/ignore.
- `_apply_new`, `_apply_augment`, `_apply_merge`, `_apply_ignore`, `_auto_ignore_unused` — apply layer is unchanged (called against one episode's response now).
- `_filter_existing_observations` — still filters to active rows; preserves the existing semantics where idempotency drops references to already-consumed observations.
- `retrieve_reflections(project, tech, phase, polarity, limit_per_bucket)` — still the public read API used by MCP and the UI.
- `ReflectionService` (lifecycle: confirm/retire/update_text) — separate class, untouched.

**`synthesize_next` body** (full pseudocode in Section 3 of the visual design; spec retains this for plan precision):

```python
async def synthesize_next(self, *, project: str) -> SynthesisStep:
    episode = self._pick_oldest_pending(project)
    if episode is None:
        return SynthesisStep(
            processed=False, episode_id=None,
            counts=dict(self._ZERO_COUNTS),
            queue=self._read_queue_counts(project),
            failure=None,
        )

    self._conn.execute("SAVEPOINT episode_synthesize")
    try:
        ctx = self._load_episode_context(episode)
        prompt = self._build_episode_prompt(ctx)
        raw = await self._chat.complete(prompt)
        response = self.parse_response(raw)

        self._apply_new(response.new, project=project)
        self._apply_augment(response.augment)
        self._apply_merge(response.merge)
        self._apply_ignore(response.ignore)
        active_ids = [o.id for o in ctx.observations if o.status == "active"]
        auto_ignored = self._auto_ignore_unused(active_ids)
        self._mark_synthesized(episode.id)
    except (ChatError, SynthesisResponseError) as exc:
        # LLM-class failure: ROLLBACK so partial reflection_sources/inserts
        # don't land, then stamp synth_failed_at OUTSIDE the savepoint so the
        # cooldown record persists. Without the cooldown stamp, _pick_oldest_pending
        # would return the same episode every call → MCP drain loop spins forever
        # and the UI auto-fire chain re-shows the same failure card 67 times.
        self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        self._conn.execute(
            "UPDATE episodes SET synth_failed_at = ? WHERE id = ?",
            (self._clock().isoformat(), episode.id),
        )
        self._conn.commit()
        # Note: if THIS UPDATE itself raises (e.g. busy_timeout exceeded),
        # the exception propagates to BaseException → route 500. The cooldown
        # would not be stamped, so the same episode is picked again next call.
        # Acceptable degradation: in practice this UPDATE is one row, single
        # column, by id, with PRAGMA busy_timeout=5000 — vanishingly rare.
        return SynthesisStep(
            processed=True, episode_id=episode.id,
            counts=dict(self._ZERO_COUNTS),
            queue=self._read_queue_counts(project),
            failure=str(exc),
        )
    except BaseException:
        self._conn.execute("ROLLBACK TO SAVEPOINT episode_synthesize")
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
        raise
    else:
        self._conn.execute("RELEASE SAVEPOINT episode_synthesize")
    self._conn.commit()

    counts = {
        "created": len(response.new),
        "augmented": len(response.augment),
        "merged": len(response.merge),
        "ignored": len(response.ignore),
        "auto_ignored": auto_ignored,
    }
    return SynthesisStep(
        processed=True, episode_id=episode.id,
        counts=counts,
        queue=self._read_queue_counts(project),
        failure=None,
    )
```

**`_read_queue_counts`** — single statement, single connection, single moment in time:

```sql
SELECT
  COUNT(*) FILTER (WHERE synthesized_at IS NOT NULL)                                 AS done,
  COUNT(*) FILTER (WHERE synthesized_at IS NULL
                     AND (synth_failed_at IS NULL
                          OR synth_failed_at < datetime('now','-300 seconds')))      AS pending,
  COUNT(*) FILTER (WHERE synthesized_at IS NULL
                     AND synth_failed_at >= datetime('now','-300 seconds'))          AS in_cooldown
FROM episodes
WHERE project = ? AND outcome IS NOT NULL;
```

The route uses `step.queue.done`, `step.queue.pending`, `step.queue.total` directly — no second-source counts.

**`_pick_oldest_pending`** SQL — selects `project` along with the row so `EpisodeForPrompt` is self-sufficient; applies the cooldown filter at SELECT-time:

```sql
SELECT id, project, goal, tech, outcome
  FROM episodes
 WHERE project = ?
   AND outcome IS NOT NULL
   AND synthesized_at IS NULL
   AND (synth_failed_at IS NULL
        OR synth_failed_at < datetime('now', '-300 seconds'))
 ORDER BY ended_at ASC
 LIMIT 1;
```

`_count_pending` mirrors the same filter so the banner's `pending_remaining` count matches what `_pick_oldest_pending` will actually find.

**`_load_episode_context`** queries:

```sql
-- Episode header
SELECT id, goal, tech, outcome FROM episodes WHERE id = ?;

-- All observations for this episode, regardless of status
SELECT id, content, outcome, component, theme, tech, created_at, status
  FROM observations
 WHERE episode_id = ?
 ORDER BY created_at ASC, rowid ASC;

-- Tech-filtered reflections, project-scoped OR scope='general'.
-- Uses episode.project directly (no subquery) thanks to the project field
-- on EpisodeForPrompt.
SELECT id, title, tech, phase, polarity, use_cases, hints, confidence, status, scope
  FROM reflections
 WHERE (project = ? OR scope = 'general')
   AND status IN ('pending_review', 'confirmed')
   AND (? IS NULL OR tech = ? OR tech IS NULL)
 ORDER BY confidence DESC, updated_at DESC;
```

(After Commit 4 lands the episodic redesign, Commit 5 adds the `OR scope = 'general'` clause; until then the query is project-only.)

**Per-episode prompt template** (`_build_episode_prompt`):

```
You are evaluating one coding episode for memory consolidation.

EPISODE
  goal:    {episode.goal}
  tech:    {episode.tech or "(unspecified)"}
  outcome: {episode.outcome}

OBSERVATIONS from this episode (all of them, regardless of prior status):
- id={obs.id} (outcome={obs.outcome}, component={obs.component or "-"}, theme={obs.theme or "-"})
  status: {obs.status}
  content: {obs.content}
- ...

EXISTING REFLECTIONS for this tech (you may augment, merge, leave alone):
- id={refl.id} [{polarity}/{phase}/{tech or "any-tech"}] (confidence {refl.confidence}, status {refl.status})
  title: {refl.title}
  use_cases: {refl.use_cases}
  hints: {refl.hints}
- ...

Decide what to do with this episode's observations. Respond ONLY with this JSON shape:
{
  "new":     [{"title": "...", "phase": "planning"|"implementation"|"general",
               "polarity": "do"|"dont"|"neutral", "use_cases": "...", "hints": ["..."],
               "tech": "..." or null, "confidence": 0.1..1.0,
               "source_observation_ids": ["..."]}, ...],
  "augment": [{"reflection_id": "...", "add_hints": ["..."],
               "rewrite_use_cases": "..." or null, "confidence_delta": 0.0,
               "add_source_observation_ids": ["..."]}, ...],
  "merge":   [{"source_id": "...", "target_id": "...", "justification": "..."}, ...],
  "ignore":  ["observation_id", ...]
}
```

The `status` field in each observation lets the LLM distinguish "already part of an existing reflection" rows from active ones — usually it'll propose ignore/augment for consumed ones rather than redundant `new` actions.

**Test rewrite** (`tests/services/test_reflection.py`):

| Existing test | Disposition |
|---|---|
| `test_synthesize_short_circuits_on_same_goal_recent_run` | Delete |
| `test_synthesize_no_short_circuit_after_window` | Delete |
| `test_synthesize_no_short_circuit_on_new_observations` | Delete |
| `test_synthesize_advances_watermark` | Delete; replaced by `test_synthesize_next_marks_synthesized_at` |
| `test_load_context_*` | Delete; replaced by `test_load_episode_context_includes_all_observations` and `test_load_episode_context_filters_reflections_by_tech` |
| `test_build_prompt_*` | Rewrite as `test_build_episode_prompt_*` |
| `test_apply_new_*` / `test_apply_augment_*` / `test_apply_merge_*` / `test_apply_ignore_*` / `test_auto_ignore_unused_*` | Survive verbatim — apply layer unchanged |
| `test_synthesize_round_trip` | Rewrite as `test_synthesize_next_round_trip_single_episode` |
| (new) | `test_synthesize_next_picks_oldest_pending` |
| (new) | `test_synthesize_next_returns_processed_false_when_empty` |
| (new) | `test_synthesize_next_chat_error_stamps_synth_failed_at_and_returns_failure` |
| (new) | `test_synthesize_next_chat_error_does_not_set_synthesized_at` (cooldown'd episode is retryable after window) |
| (new) | `test_synthesize_next_parse_error_stamps_synth_failed_at_and_returns_failure` |
| (new) | `test_synthesize_next_skips_episode_in_cooldown_window` (synth_failed_at within last 300s → not picked) |
| (new) | `test_synthesize_next_picks_episode_after_cooldown_elapses` (synth_failed_at older than 300s → picked again) |
| (new) | `test_synthesize_next_db_integrity_error_propagates_and_no_synth_failed_at` (DB error abort path leaves no half-state) |
| (new) | `test_synthesize_next_savepoint_release_on_success` (no leaked savepoint after happy path) |
| (new) | `test_synthesize_next_savepoint_release_on_chat_error` |
| (new) | `test_synthesize_next_loads_all_observations_regardless_of_status` |
| (new) | `test_synthesize_next_uses_episode_tech_for_reflection_filter` |
| (new) | `test_count_pending_excludes_cooldown_episodes` |

## Commit 3 — Route refactor

`better_memory/ui/app.py`:

The route shell stays — busy-flag mutex (`_try_acquire_synth` / `_release_synth`), `worker_dispatched` tracking, `run_async_in_worker(...)` with `synth_timeout`, the WorkerTimeout / BaseException / general except handlers — all of that proven hardening from PR #18 survives. What changes is the inner `_run` coroutine: it calls `synthesize_next` (one episode) instead of `synthesize` (the batch).

```python
@app.post("/observations/synthesize")
def observations_synthesize() -> tuple[str, int, dict[str, str]]:
    acquired_token = _try_acquire_synth()
    if acquired_token is None:
        return ('<div class="card card-error">…</div>', 429, {})

    worker_dispatched = False
    try:
        project = project_name()
        db_path_local = app.extensions["db_path"]
        ollama_host = app.extensions["ollama_host"]
        consolidate_model = app.extensions["consolidate_model"]

        def _build_coro():
            async def _run():
                local_conn = None
                chat = None
                try:
                    local_conn = connect(db_path_local)
                    chat = OllamaChat(host=ollama_host, model=consolidate_model)
                    svc = ReflectionSynthesisService(local_conn, chat=chat)
                    step = await svc.synthesize_next(project=project)
                    return step
                finally:
                    if chat is not None:
                        try: await chat.aclose()
                        except BaseException: pass
                    if local_conn is not None:
                        try: local_conn.close()
                        except BaseException: pass
                    _release_synth(acquired_token)
            return _run()

        worker_dispatched = True
        try:
            step = run_async_in_worker(_build_coro, timeout=synth_timeout)
        except WorkerTimeout:
            return ('<div class="card card-error">…</div>', 504, {})
        except BaseException as exc:
            _release_synth(acquired_token)
            return (f'<div class="card card-error"><p>{escape(str(exc))}</p></div>', 500, {})

        # Banner counts come from step.queue (single-connection snapshot, no race).
        rendered = render_template(
            "fragments/synth_step_banner.html",
            step=step, queue=step.queue,
        )
        # observations-synthesized fires on EVERY step (including failure-with-progress
        # and the final done step). The obs panel listens via hx-trigger="...from:body"
        # and reloads on each → live drain visualization. 67 cheap reloads over a 10-min
        # chain costs ~1-2 s of total render work.
        return (rendered, 200, {"HX-Trigger": "observations-synthesized"})
    finally:
        if not worker_dispatched:
            _release_synth(acquired_token)
```

`_count_done_for_banner` is a small helper (or inlined query): `SELECT COUNT(*) FROM episodes WHERE project = ? AND outcome IS NOT NULL AND synthesized_at IS NOT NULL`.

**Banner state machine — four states, deterministic by `(step.processed, step.failure, queue.pending)`:**

| State | Condition | Card class | Auto-fire div present? |
|---|---|---|---|
| **success-with-pending** | `processed AND not failure AND queue.pending > 0` | `card synth-step` | yes |
| **success-without-pending** (chain end) | `processed AND not failure AND queue.pending == 0` | `card synth-done` | no |
| **failure-with-pending** | `processed AND failure AND queue.pending > 0` | `card card-warning synth-step` | yes |
| **failure-without-pending** | `processed AND failure AND queue.pending == 0` | `card card-warning synth-step` | no |
| **empty-queue** (no work picked) | `not processed` | `card synth-done` | no |

**New banner template** `better_memory/ui/templates/fragments/synth_step_banner.html`:

```jinja
{% if not step.processed %}
  <div class="card synth-done">
    <p><strong>Synthesis complete.</strong> {{ queue.done }} episodes processed.
       {% if queue.in_cooldown %} ({{ queue.in_cooldown }} in cooldown after recent failures){% endif %}
    </p>
  </div>
{% elif step.failure %}
  <div class="card card-warning synth-step">
    <p><strong>Episode {{ queue.done }}/{{ queue.total }} failed:</strong> {{ step.failure }}</p>
    {% if queue.pending > 0 %}
      <div hx-post="/observations/synthesize"
           hx-trigger="load delay:200ms"
           hx-target="closest .synth-step"
           hx-swap="outerHTML">⏵ Continuing…</div>
    {% endif %}
  </div>
{% else %}
  <div class="card synth-step">
    <p>Episode <strong>{{ queue.done }}/{{ queue.total }}</strong> processed
       ({{ step.counts.created }} new,
        {{ step.counts.augmented }} augmented,
        {{ step.counts.merged }} merged,
        {{ step.counts.auto_ignored }} auto-ignored)</p>
    {% if queue.pending > 0 %}
      <div hx-post="/observations/synthesize"
           hx-trigger="load delay:200ms"
           hx-target="closest .synth-step"
           hx-swap="outerHTML">⏵ Continuing…</div>
    {% endif %}
  </div>
{% endif %}
```

The old `observations_synth_banner.html` is deleted.

**Stray-click semantics during a chain:** the Synthesize button on `/observations` has `hx-disabled-elt="this"` today, but the auto-fire div in the swapped fragment is a *different* element — once the first response is rendered, the button itself is no longer disabled (it isn't part of the swap target). If the user clicks the button while a chain is mid-flight, the existing busy-flag mutex returns 429 with a `card-error`. This is the primary defense against double-trigger; no additional disabling is needed in v1.

**Test rewrite** (`tests/ui/test_observations.py`):

| Existing test | Disposition |
|---|---|
| `test_synthesize_route_returns_banner` | Rewrite: assert step banner shape with `done`/`total`/`pending_remaining > 0` and presence of `hx-trigger` div |
| `test_synthesize_route_completes_with_no_pending` | New: empty-queue case returns `synth-done` card without auto-fire div |
| `test_synthesize_route_failure_banner_includes_auto_fire_when_pending` | New: ChatError raised in svc → 200 with failure card AND continuation div (failure on episode N doesn't stop the chain) |
| `test_synthesize_route_failure_banner_omits_auto_fire_when_pending_zero` | New: failure on the LAST pending episode → failure card without auto-fire (pending_remaining is 0 because the just-failed episode is now in cooldown) |
| `test_synthesize_route_429_during_chain_returns_card_error` | New: second tab clicking Synthesize while chain is mid-flight → 429 with card-error (chain-recovery: multi-tab) |
| `test_synthesize_route_resumes_after_page_refresh` | New: simulate a chain interruption (tab refresh between requests); next click resumes from the next oldest pending episode (no double-processing because synthesized_at gates the pick) |
| `test_synthesize_route_stray_click_during_chain_returns_429` | New: simulate the user clicking the Synthesize button mid-chain; mutex returns 429 with card-error, chain unaffected |
| `test_synthesize_route_hx_trigger_fires_every_step` | New: assert `HX-Trigger: observations-synthesized` header is present on success step, failure step, and the final done step (not just the chain-end) |
| `test_synthesize_route_banner_uses_step_queue_counts` | New: assert template-rendered counts come from `step.queue` not from a second-source query (mock the service to return distinct queue values, confirm template echoes them verbatim) |
| `test_synthesize_429_when_busy` / busy-flag leak path tests | Survive — mutex pattern unchanged |
| WorkerTimeout / BaseException leak tests | Survive — error envelope unchanged |
| `test_synthesize_route_setup_error_*` | Survive — pre-worker leak guards unchanged |

**Chain-recovery contract** (documented because it's load-bearing for the auto-fire pattern):

- **Mid-chain page refresh** → the auto-fire div was inside a partial response and is no longer in the DOM. The chain stops naturally. The next click resumes by picking the next oldest pending episode (the just-processed one already has `synthesized_at` set).
- **Browser back** → same. Partial responses are not part of the page's history; navigating back returns to the un-augmented page.
- **Multi-tab** → the busy-flag mutex (per-process) ensures only one tab's chain proceeds. The other tab's auto-fire request gets 429 with a card-error and stops automatically.
- **Failure mid-chain** → the failed episode's `synth_failed_at` is stamped, the chain continues to the next oldest pending. The failed episode becomes eligible again 5 min after `synth_failed_at`, picked up on the next user click after the cooldown.

## Commit 4 — MCP caller refactor

`better_memory/mcp/server.py:571`:

```python
# Before
buckets = await reflections.synthesize(
    goal=args["goal"], tech=args.get("tech"), project=project,
)

# After
# Drain pending episodes so the new episode's reflection context is fresh.
while (await reflections.synthesize_next(project=project)).processed:
    pass
buckets = reflections.retrieve_reflections(
    project=project, tech=args.get("tech"),
)
```

`retrieve_reflections` already takes `tech` and applies the same `tech == ? OR tech IS NULL` filter the prior `synthesize`'s return path used.

**Test rewrite** (`tests/mcp/test_episode_tools.py`):

- `test_start_episode_returns_buckets_after_synth` — rewrite: seed 3 closed pending episodes via the new `seed_pending_episodes` helper → assert each gets `synthesized_at` set after `start_episode` → assert returned buckets are tech-filtered.
- `test_start_episode_with_no_pending` — new: closed episodes already have `synthesized_at` → loop terminates immediately → buckets returned correctly.
- `test_start_episode_handles_synth_failure` — new: one of N pending episodes raises ChatError → that episode gets `synth_failed_at` stamped, loop continues past it (the cooldown filter excludes it from the next `_pick_oldest_pending` call), other episodes are processed, buckets still returned.
- `test_start_episode_loop_terminates_on_persistent_failure` — new: ALL pending episodes raise ChatError → each gets `synth_failed_at` stamped → cooldown filter excludes them → `_pick_oldest_pending` returns None → loop terminates with buckets returned (no infinite loop).

**New fixture helper** (`tests/conftest.py` or `tests/mcp/conftest.py`):

```python
def seed_pending_episodes(
    conn: sqlite3.Connection,
    project: str,
    n: int,
    obs_per_episode: int = 2,
    tech: str | None = None,
) -> list[str]:
    """Seed N closed-pending episodes, each with M active observations.

    Returns episode ids in creation order. Each episode is closed (outcome='success'),
    has synthesized_at NULL, has obs_per_episode 'active' observations.
    """
```

Reusable across `test_synthesize_next_*` (services), `test_synthesize_route_*` (UI), and `test_start_episode_*` (MCP) — anywhere a test needs a pre-populated pending queue.

## Commit 5 — General-scope reflections

### Migration 0007

`better_memory/db/migrations/0007_reflection_scope.sql`:

```sql
-- Migration 0007: cross-project (general) scope for reflections + observations.
--
-- Workflow rules and other project-agnostic lessons are currently bucketed
-- under a single project. Adding scope='general' lets them surface in every
-- project's retrieval queries.

-- 1. Scope on observations: pre-marked at write time. Synthesis derives
--    a new reflection's scope from its source observations.
ALTER TABLE observations
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

-- 2. Scope on reflections: the actual retrieval-side filter target.
ALTER TABLE reflections
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

-- 3. Index on general-scoped reflections — small, hot-path query.
CREATE INDEX idx_reflections_scope_general
    ON reflections(updated_at DESC)
    WHERE scope = 'general';

-- 4. One-shot fix-up: the workflow rule observation recorded today
--    (2026-05-04, "always assign per-step confidence to a superpowers plan")
--    should be general. Idempotent: no-op if the row doesn't exist.
UPDATE observations
   SET scope = 'general'
 WHERE id = '413d47550efd4adfa2c238d6ce5099f9';
```

Migration test (`tests/db/test_migration_0007.py`) seeds a small set of observations + reflections and asserts:

- `scope` column exists on both tables, default 'project' for existing rows
- Partial index on `(scope='general')` exists
- The CHECK constraint rejects `scope='invalid'` on INSERT
- The fix-up applied (id `413d47…` has scope='general') if the row exists; no-op otherwise

### Write path

`better_memory/services/observation.py:create` gains a keyword-only `scope: str = "project"` parameter. Validation: `if scope not in ("project", "general"): raise ValueError(...)`. The INSERT statement adds the column.

`memory.observe` MCP tool schema gains optional `scope` field with same default. Tool docs describe it: "'project' (default) for project-scoped observations; 'general' for cross-project workflow rules."

### Synthesis path

`_apply_new` derives the new reflection's scope from sources. Add a helper:

```python
def _derive_new_reflection_scope(self, source_obs_ids: list[str]) -> str:
    """Return 'general' iff every source observation has scope='general'."""
    if not source_obs_ids:
        return "project"  # defensive default
    placeholders = ",".join("?" * len(source_obs_ids))
    rows = self._conn.execute(
        f"SELECT scope FROM observations WHERE id IN ({placeholders})",
        source_obs_ids,
    ).fetchall()
    if not rows:
        return "project"
    return "general" if all(r["scope"] == "general" for r in rows) else "project"
```

`_apply_new` calls this and passes the result into the INSERT for `reflections`. The INSERT statement adds the `scope` column with the derived value.

`_apply_augment` does NOT touch `reflections.scope` — augmenting an existing reflection preserves its scope, regardless of source observations. A general reflection augmented with project-specific evidence stays general.

`_apply_merge` also preserves target scope.

The per-episode prompt's reflection block does NOT need to label scope explicitly; the LLM treats all visible reflections the same way (augment, merge, leave alone). Apply layer enforces scope semantics.

### Retrieval path

`retrieve_reflections` WHERE clause changes:

```sql
WHERE (project = ? OR scope = 'general')
  AND status IN ('pending_review', 'confirmed')
  AND ...
```

`memory.retrieve` MCP tool flows through this same code path — no tool-schema change needed; behavior expands.

`_load_episode_context`'s reflection query gets the same OR clause (general reflections are visible to the LLM during synthesis so they can be augmented if relevant).

### Test impact

| File | Additions |
|---|---|
| `tests/db/test_migration_0007.py` | New file — schema, defaults, CHECK constraint, fix-up. |
| `tests/services/test_observation.py` | New tests for `create(scope='project')` (default) and `create(scope='general')`; ValueError on invalid scope. |
| `tests/services/test_reflection.py` | New tests: `test_apply_new_derives_general_when_all_sources_general`, `test_apply_new_derives_project_when_any_source_project`, `test_apply_augment_preserves_general_scope`, `test_apply_merge_preserves_target_scope`, `test_retrieve_reflections_includes_general_from_other_projects`, `test_load_episode_context_includes_general_reflections`. |
| `tests/mcp/test_*` | New test for `memory.observe` with `scope='general'`. |

## Out-of-scope / future work

- **Stop button** for in-flight chains. The chain stops naturally when the tab closes; an in-UI Stop button needs a server-side cancellation flag and is excluded from v1.
- **Reflection cap-at-N** in the per-episode prompt. The tech filter naturally bounds reflection count for now; if reflection counts grow unbounded later, add a `LIMIT` to `_load_episode_context`'s reflection query.
- **Cross-episode synthesis pass.** If after the per-episode pass we want a follow-up "now look at all reflections globally and merge near-duplicates", that's a separate v2 feature with a separate UI button. Not required for v1 because the episode-by-episode `augment`/`merge` actions already do this incrementally.
- **Failure history beyond the latest stamp.** `synth_failed_at` records only the most recent failure timestamp; we don't track failure count, last error message, or full history. If a particular episode fails repeatedly across user-driven retries, the user sees the same failure card and investigates. A `synthesis_failure_log` table with full audit trail is YAGNI.
- **Removing the band-aid.** The `OllamaChat` default timeout bump to 240 s landed earlier tonight as a band-aid. It stays — it's a reasonable per-call ceiling for any future LLM caller and isn't tied to the batch-vs-episode distinction.

## Spec self-review (post-iteration)

- **Placeholders:** none. Every commit has files, code, and SQL specified concretely. Every test has a rationale.
- **Internal consistency:** ✓ — `synth_failed_at` is described identically in the decisions log, migration SQL, service pseudocode, `_pick_oldest_pending` SQL, and the test rewrite tables. The cooldown duration (300 s) is referenced in three places — all match. The `synthesized_at`-vs-`synth_failed_at` distinction is consistent: success path sets the former, failure path sets only the latter.
- **Scope:** ✓ — single PR, four commits, ~700 lines net delta after the cooldown / chain-recovery additions.
- **Ambiguity check:** auto-fire delay locked at 200 ms; cooldown locked at 300 s. Both calls sit comfortably between "feels instant but won't render-thrash" and "outlasts a chain but doesn't strand failures across hours."
- **Iteration log (2026-05-03 evening):**
  - **Pass 1 — found 2 real defects:** (1) `tests/db/test_schema.py` has 4 stale assertions on `synthesis_runs` that commit 1 must remove (caught by grep), and (2) without `synth_failed_at` cooldown, persistent per-episode failures cause `_pick_oldest_pending` to return the same episode forever, infinite-looping the MCP drain and showing 67× failure cards in the UI auto-fire chain (caught by walking the loop semantics step-by-step). Both fixes landed.
  - **Pass 2 — five hardenings on commit 3:** HX-Trigger cadence pinned to every step (live drain UX); single-source `EpisodeQueueCounts` dataclass replaces dual-source `done`/`pending_remaining` to eliminate the race window; `hx-disabled-elt` interaction with auto-chain documented (rely on 429 mutex); failure-of-failure path documented (UPDATE synth_failed_at raising propagates to 500, acceptable degradation); 4-state banner state-machine table added for implementer/reviewer clarity.
  - **Pass 3 — scope feature + lift below-90% steps (2026-05-04):** Added Commit 5 (general-scope reflections via migrations 0007 and write/synthesis/retrieval changes), motivated by the user's per-step-confidence workflow rule that needs to be project-agnostic. Plus four mitigations to lift Tasks 5/7/14/17 above 90%: (1) `EpisodeForPrompt.project` field replaces awkward subquery in `_load_episode_context`; (2) `ObservationForPrompt.status` defaults to 'active' so the existing `load_context` doesn't need a dual-state edit; (3) Task 14 worker-thread test harness pinned to OllamaChat.complete-stub level; (4) Task 17 MCP test harness pattern pinned by reading the existing `tests/mcp/test_episode_tools.py` setup before authoring new tests.
  - Final per-step confidence (Pass 3): 95% / 92% / 92% / 95% / 92%. Compound ~75-80% across five commits.
