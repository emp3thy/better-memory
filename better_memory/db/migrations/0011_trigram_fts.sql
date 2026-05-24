-- Migration 0011: trigram FTS5 table for the `sqlite` embeddings backend.
--
-- Adds a second FTS5 virtual table over observations.content using the
-- trigram tokenizer. The `sqlite` backend uses this as the second source
-- in hybrid_search's RRF fusion (replacing vec0 kNN).
--
-- The table is populated regardless of which backend is active, so
-- switching BETTER_MEMORY_EMBEDDINGS_BACKEND between ollama and sqlite
-- requires no data migration at runtime.

CREATE VIRTUAL TABLE observation_trigram_fts USING fts5(
    content,
    content='observations',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Backfill from existing observations.
INSERT INTO observation_trigram_fts(rowid, content)
SELECT rowid, content FROM observations;

-- Keep in sync. Names suffixed with _trigram so they do not collide with
-- the existing observations_ai / _ad / _au triggers (created in
-- 0002_episodic.sql) which write to observation_fts.

CREATE TRIGGER observations_trigram_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observation_trigram_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

CREATE TRIGGER observations_trigram_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER observations_trigram_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observation_trigram_fts(observation_trigram_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO observation_trigram_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;
