-- better-memory migration 0002: episodic memory schema.
--
-- Replaces the insight-based aggregation schema with episodes + reflections
-- per docs/superpowers/specs/2026-04-20-episodic-memory-design.md §4.
--
-- Subsequent tasks in the Phase 1 plan append DDL to this file in
-- dependency order: drops → episodes → episode_sessions → observations
-- → reflections → reflection_sources → synthesis_runs.

----------------------------------------------------------------------
-- Drop old insight aggregation schema (data dumped per design decision).
----------------------------------------------------------------------

DROP TRIGGER IF EXISTS insights_au;
DROP TRIGGER IF EXISTS insights_ad;
DROP TRIGGER IF EXISTS insights_ai;
DROP TABLE IF EXISTS insight_embeddings;
DROP TABLE IF EXISTS insight_fts;
DROP TABLE IF EXISTS insight_relations;
DROP TABLE IF EXISTS insight_sources;
DROP TABLE IF EXISTS insights;

----------------------------------------------------------------------
-- Episodes: goal-bounded arcs that group observations.
----------------------------------------------------------------------

CREATE TABLE episodes (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    tech          TEXT,
    goal          TEXT,
    started_at    TEXT NOT NULL,
    hardened_at   TEXT,
    ended_at      TEXT,
    close_reason  TEXT CHECK(close_reason IN (
        'goal_complete',
        'plan_complete',
        'abandoned',
        'superseded',
        'session_end_reconciled'
    )),
    outcome       TEXT CHECK(outcome IN (
        'success',
        'partial',
        'abandoned',
        'no_outcome'
    )),
    summary       TEXT
);

CREATE INDEX idx_episodes_project_ended ON episodes(project, ended_at);
CREATE INDEX idx_episodes_project_outcome ON episodes(project, outcome);
CREATE INDEX idx_episodes_tech ON episodes(tech);
