-- better-memory migration 0003: add last_goal column to synthesis_runs.
--
-- Used by ReflectionSynthesisService's short-circuit (spec §5 "Short-circuit"):
-- when memory.start_episode re-runs with the same goal inside a 10-minute
-- window and no new observations arrived, synthesis is skipped and the
-- existing reflection set is returned directly.
-- Nullable; older rows default NULL. Callers that call synthesize() populate
-- the column; Phase 5 unit tests also accept NULL explicitly.

ALTER TABLE synthesis_runs ADD COLUMN last_goal TEXT;
