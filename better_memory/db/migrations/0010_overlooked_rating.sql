-- Migration 0010: overlooked rating class.
--
-- Adds a 5th closed-loop rating class, `overlooked`: a memory that was
-- relevant and should have been applied, but was not — until the user
-- explicitly intervened. See issue #60 and
-- docs/superpowers/specs/2026-05-17-overlooked-memory-rating-class-design.md.

-- Widen the classification CHECK to admit 'overlooked'. SQLite cannot
-- ALTER a CHECK constraint, so session_memory_exposure is recreated.
-- No table holds a foreign key into session_memory_exposure, so no
-- foreign_keys pragma toggling is required.

CREATE TABLE session_memory_exposure_new (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve')),
    rated_at       TEXT,
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled', 'overlooked')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

INSERT INTO session_memory_exposure_new
    (session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification)
SELECT
    session_id, memory_kind, memory_id, exposed_at, source, rated_at, classification
FROM session_memory_exposure;

DROP TABLE session_memory_exposure;
ALTER TABLE session_memory_exposure_new RENAME TO session_memory_exposure;

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);

-- Per-memory overlooked counters, parallel to useful_count / times_misled
-- added in migration 0009.

ALTER TABLE reflections       ADD COLUMN times_overlooked   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections       ADD COLUMN last_overlooked_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_overlooked   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_overlooked_at TEXT;
