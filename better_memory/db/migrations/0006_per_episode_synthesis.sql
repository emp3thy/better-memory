-- better-memory migration 0006: per-episode synthesis tracking.
--
-- Replaces the watermark-driven `synthesis_runs` model with a per-episode
-- one. See docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md.

-- 1. Per-episode tracking column. NULL means "needs synthesis".
ALTER TABLE episodes ADD COLUMN synthesized_at TIMESTAMP;

-- 2. Per-episode failure-cooldown column. Set on LLM-class failure;
--    excluded from `_pick_oldest_pending` for 300 s after stamping.
ALTER TABLE episodes ADD COLUMN synth_failed_at TIMESTAMP;

-- 3. Partial index for "find next pending episode".
--    Cheap: indexes only rows that are currently candidates for synth.
--    The cooldown filter is applied at SELECT-time because datetime('now')
--    is non-deterministic and can't live in a partial-index predicate.
CREATE INDEX idx_episodes_pending_synth
    ON episodes(project, ended_at)
    WHERE outcome IS NOT NULL AND synthesized_at IS NULL;

-- 4. Backfill: closed episodes whose observations are ALL non-active
--    have effectively been consolidated by the prior batch synth — mark
--    them done. Closed episodes with leftover active observations stay
--    NULL → next synth run picks them up.
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
