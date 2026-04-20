-- better-memory migration 0002: episodic memory schema.
--
-- Replaces the insight-based aggregation schema with episodes + reflections
-- per docs/superpowers/specs/2026-04-20-episodic-memory-design.md §4.
--
-- Subsequent tasks in the Phase 1 plan append DDL to this file in
-- dependency order: drops → episodes → episode_sessions → observations
-- → reflections → reflection_sources → synthesis_runs.

-- Marker statement so executescript has at least one statement to run.
SELECT 1;
