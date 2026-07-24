-- Migration 0015: tag exposures that came from the exploration slot.
--
-- The reserved per-bucket slot (spec 2026-07-23 retrieval-quality, section 2)
-- serves under-rated memories to earn them ratings. Those serves are an
-- investment the ranker makes, not a relevance claim, so the headline
-- usefulness metric excludes them. Ratings still apply to them normally.

ALTER TABLE session_memory_exposure
    ADD COLUMN via_exploration INTEGER NOT NULL DEFAULT 0;
