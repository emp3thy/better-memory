-- Migration 0013: per-memory `ignored` counters, and a backfill from history.
--
-- Ratings were write-only for the `ignored` class: memory_rating._apply_one
-- bumps useful_count / times_misled / times_overlooked but does nothing at all
-- when a memory is classified `ignored`. Nothing in the system therefore ever
-- learns that a memory keeps being shown and keeps not mattering.
--
-- Measured on a live DB with 4670 rated exposures: 55 memories had been rated
-- 10+ times each and were useful ZERO times. Between them they accounted for
-- 1284 rated exposures — 27.5% of everything served — while contributing
-- nothing. They were never demoted because the ranking key
-- (useful_count + 3*times_overlooked) has no negative term.
--
-- These counters give the ranker that term. The backfill matters as much as
-- the schema: it turns the rating history that already exists into signal
-- immediately, instead of waiting to re-learn what the DB already knows.

ALTER TABLE reflections       ADD COLUMN times_ignored   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections       ADD COLUMN last_ignored_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_ignored   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_ignored_at TEXT;

-- Backfill from session_memory_exposure. Counted per distinct
-- (session_id, memory_id): a memory retrieved several times in one session
-- writes one exposure row per retrieval, and _apply_one stamps all of them
-- with the same classification, so counting rows would score retrieval
-- chattiness rather than sessions in which the memory failed to land.

UPDATE reflections SET
    times_ignored = COALESCE((
        SELECT COUNT(DISTINCT e.session_id)
        FROM session_memory_exposure e
        WHERE e.memory_kind = 'reflection'
          AND e.memory_id = reflections.id
          AND e.classification = 'ignored'
    ), 0),
    last_ignored_at = (
        SELECT MAX(e.rated_at)
        FROM session_memory_exposure e
        WHERE e.memory_kind = 'reflection'
          AND e.memory_id = reflections.id
          AND e.classification = 'ignored'
    );

UPDATE semantic_memories SET
    times_ignored = COALESCE((
        SELECT COUNT(DISTINCT e.session_id)
        FROM session_memory_exposure e
        WHERE e.memory_kind = 'semantic'
          AND e.memory_id = semantic_memories.id
          AND e.classification = 'ignored'
    ), 0),
    last_ignored_at = (
        SELECT MAX(e.rated_at)
        FROM session_memory_exposure e
        WHERE e.memory_kind = 'semantic'
          AND e.memory_id = semantic_memories.id
          AND e.classification = 'ignored'
    );

CREATE INDEX IF NOT EXISTS idx_reflections_ignored
    ON reflections(times_ignored) WHERE useful_count = 0;
