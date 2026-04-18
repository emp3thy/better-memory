-- better-memory initial schema.
--
-- Consolidated migration: spec baseline (observations, insights, audit, hooks)
-- + episodic reinforcement extensions (outcome, reinforcement_score, scope_path,
-- polarity) in a single file. See docs/superpowers/specs/2026-04-06-better-memory-design.md
-- (sections 2, 3, 8) for the baseline and the Phase 1 task brief for the
-- episodic layer.

----------------------------------------------------------------------
-- Observations
----------------------------------------------------------------------

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    project TEXT NOT NULL,
    component TEXT,
    theme TEXT,
    session_id TEXT,
    trigger_type TEXT,
    status TEXT DEFAULT 'active',
    retrieved_count INTEGER DEFAULT 0,
    used_count INTEGER DEFAULT 0,
    validated_true INTEGER DEFAULT 0,
    validated_false INTEGER DEFAULT 0,
    last_retrieved TIMESTAMP,
    last_used TIMESTAMP,
    last_validated TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Episodic extensions
    outcome TEXT NOT NULL DEFAULT 'neutral'
        CHECK(outcome IN ('success', 'failure', 'neutral')),
    reinforcement_score REAL NOT NULL DEFAULT 0.0,
    scope_path TEXT
);

CREATE INDEX idx_observations_project_component_outcome
    ON observations(project, component, outcome);

CREATE INDEX idx_observations_scope_outcome
    ON observations(scope_path, outcome);

-- FTS5 external-content table. FTS5 does NOT auto-sync when using
-- ``content=''``, so explicit triggers mirror writes from the base table.
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

-- Vector index keyed by observation id (text PK correlates to observations.id).
CREATE VIRTUAL TABLE observation_embeddings USING vec0(
    observation_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

----------------------------------------------------------------------
-- Insights
----------------------------------------------------------------------

CREATE TABLE insights (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    project TEXT,
    component TEXT,
    status TEXT DEFAULT 'pending_review',
    confidence TEXT DEFAULT 'low',
    evidence_count INTEGER DEFAULT 0,
    last_validated TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Episodic extension
    polarity TEXT NOT NULL DEFAULT 'neutral'
        CHECK(polarity IN ('do', 'dont', 'neutral'))
);

CREATE TABLE insight_sources (
    insight_id TEXT REFERENCES insights(id),
    observation_id TEXT REFERENCES observations(id),
    PRIMARY KEY (insight_id, observation_id)
);

CREATE TABLE insight_relations (
    from_insight_id TEXT REFERENCES insights(id),
    to_insight_id TEXT REFERENCES insights(id),
    relation_type TEXT,  -- related | contradicts | supersedes
    PRIMARY KEY (from_insight_id, to_insight_id)
);

CREATE VIRTUAL TABLE insight_fts USING fts5(
    title,
    content,
    component,
    content='insights',
    content_rowid='rowid'
);

CREATE TRIGGER insights_ai AFTER INSERT ON insights BEGIN
    INSERT INTO insight_fts(rowid, title, content, component)
    VALUES (new.rowid, new.title, new.content, new.component);
END;

CREATE TRIGGER insights_ad AFTER DELETE ON insights BEGIN
    INSERT INTO insight_fts(insight_fts, rowid, title, content, component)
    VALUES ('delete', old.rowid, old.title, old.content, old.component);
END;

CREATE TRIGGER insights_au AFTER UPDATE ON insights BEGIN
    INSERT INTO insight_fts(insight_fts, rowid, title, content, component)
    VALUES ('delete', old.rowid, old.title, old.content, old.component);
    INSERT INTO insight_fts(rowid, title, content, component)
    VALUES (new.rowid, new.title, new.content, new.component);
END;

CREATE VIRTUAL TABLE insight_embeddings USING vec0(
    insight_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

----------------------------------------------------------------------
-- Audit log
----------------------------------------------------------------------

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    triggered_by TEXT,
    actor TEXT,
    detail TEXT,
    session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at);

----------------------------------------------------------------------
-- Hook events (spool drain target)
----------------------------------------------------------------------

CREATE TABLE hook_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    tool TEXT,
    file TEXT,
    content_snippet TEXT,
    cwd TEXT,
    session_id TEXT,
    processed INTEGER DEFAULT 0,
    event_timestamp TIMESTAMP,
    drained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_hook_events_session ON hook_events(session_id);
CREATE INDEX idx_hook_events_processed ON hook_events(processed);
