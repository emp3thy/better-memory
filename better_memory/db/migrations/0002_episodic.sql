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

----------------------------------------------------------------------
-- Episode-sessions join: an episode may span multiple sessions
-- (user picks `continuing` at reconciliation).
----------------------------------------------------------------------

CREATE TABLE episode_sessions (
    episode_id  TEXT NOT NULL REFERENCES episodes(id),
    session_id  TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    left_at     TEXT,
    PRIMARY KEY (episode_id, session_id)
);

CREATE INDEX idx_episode_sessions_session ON episode_sessions(session_id);
CREATE INDEX idx_episode_sessions_open ON episode_sessions(session_id, left_at);

----------------------------------------------------------------------
-- Rebuild observations with episode_id (NOT NULL, FK) and tech.
-- Existing rows are dumped per design decision; the new schema requires
-- an active episode at write time, so backfilling with NULL would violate
-- the invariant.
----------------------------------------------------------------------

DROP TRIGGER IF EXISTS observations_au;
DROP TRIGGER IF EXISTS observations_ad;
DROP TRIGGER IF EXISTS observations_ai;
DROP TABLE IF EXISTS observation_embeddings;
DROP TABLE IF EXISTS observation_fts;
DROP TABLE IF EXISTS observations;

CREATE TABLE observations (
    id                  TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    project             TEXT NOT NULL,
    component           TEXT,
    theme               TEXT,
    session_id          TEXT,
    trigger_type        TEXT,
    status              TEXT DEFAULT 'active',
    retrieved_count     INTEGER DEFAULT 0,
    used_count          INTEGER DEFAULT 0,
    validated_true      INTEGER DEFAULT 0,
    validated_false     INTEGER DEFAULT 0,
    last_retrieved      TIMESTAMP,
    last_used           TIMESTAMP,
    last_validated      TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    outcome             TEXT NOT NULL DEFAULT 'neutral'
                        CHECK(outcome IN ('success', 'failure', 'neutral')),
    reinforcement_score REAL NOT NULL DEFAULT 0.0,
    scope_path          TEXT,
    -- Episodic columns (Phase 1):
    episode_id          TEXT NOT NULL REFERENCES episodes(id),
    tech                TEXT
);

CREATE INDEX idx_observations_project_component_outcome
    ON observations(project, component, outcome);
CREATE INDEX idx_observations_scope_outcome
    ON observations(scope_path, outcome);
CREATE INDEX idx_observations_episode
    ON observations(episode_id);
CREATE INDEX idx_observations_tech
    ON observations(tech);

CREATE VIRTUAL TABLE observation_fts USING fts5(
    content,
    component,
    theme,
    content='observations',
    content_rowid='rowid'
);

CREATE TRIGGER observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_fts(rowid, content, component, theme)
    VALUES (new.rowid, new.content, new.component, new.theme);
END;

CREATE TRIGGER observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_fts(observation_fts, rowid, content, component, theme)
    VALUES ('delete', old.rowid, old.content, old.component, old.theme);
END;

CREATE TRIGGER observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observation_fts(observation_fts, rowid, content, component, theme)
    VALUES ('delete', old.rowid, old.content, old.component, old.theme);
    INSERT INTO observation_fts(rowid, content, component, theme)
    VALUES (new.rowid, new.content, new.component, new.theme);
END;

CREATE VIRTUAL TABLE observation_embeddings USING vec0(
    observation_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

----------------------------------------------------------------------
-- Reflections: generalised lessons synthesised from observations.
-- Replaces the old insights table.
----------------------------------------------------------------------

CREATE TABLE reflections (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    project         TEXT NOT NULL,
    tech            TEXT,
    phase           TEXT NOT NULL
                    CHECK(phase IN ('planning', 'implementation', 'general')),
    polarity        TEXT NOT NULL
                    CHECK(polarity IN ('do', 'dont', 'neutral')),
    use_cases       TEXT NOT NULL,
    hints           TEXT NOT NULL,
    confidence      REAL NOT NULL
                    CHECK(confidence >= 0.1 AND confidence <= 1.0),
    status          TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK(status IN (
                        'pending_review', 'confirmed', 'retired', 'superseded'
                    )),
    superseded_by   TEXT REFERENCES reflections(id),
    evidence_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX idx_reflections_project_status ON reflections(project, status);
CREATE INDEX idx_reflections_tech ON reflections(tech);
CREATE INDEX idx_reflections_phase_polarity ON reflections(phase, polarity);

CREATE VIRTUAL TABLE reflection_fts USING fts5(
    title,
    use_cases,
    hints,
    content='reflections',
    content_rowid='rowid'
);

CREATE TRIGGER reflections_ai AFTER INSERT ON reflections BEGIN
    INSERT INTO reflection_fts(rowid, title, use_cases, hints)
    VALUES (new.rowid, new.title, new.use_cases, new.hints);
END;

CREATE TRIGGER reflections_ad AFTER DELETE ON reflections BEGIN
    INSERT INTO reflection_fts(reflection_fts, rowid, title, use_cases, hints)
    VALUES ('delete', old.rowid, old.title, old.use_cases, old.hints);
END;

CREATE TRIGGER reflections_au AFTER UPDATE ON reflections BEGIN
    INSERT INTO reflection_fts(reflection_fts, rowid, title, use_cases, hints)
    VALUES ('delete', old.rowid, old.title, old.use_cases, old.hints);
    INSERT INTO reflection_fts(rowid, title, use_cases, hints)
    VALUES (new.rowid, new.title, new.use_cases, new.hints);
END;

CREATE VIRTUAL TABLE reflection_embeddings USING vec0(
    reflection_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

----------------------------------------------------------------------
-- Reflection → observation link table.
----------------------------------------------------------------------

CREATE TABLE reflection_sources (
    reflection_id   TEXT NOT NULL REFERENCES reflections(id),
    observation_id  TEXT NOT NULL REFERENCES observations(id),
    PRIMARY KEY (reflection_id, observation_id)
);

CREATE INDEX idx_reflection_sources_observation
    ON reflection_sources(observation_id);
