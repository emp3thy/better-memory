-- Migration 0012: contextual exposure source + contextual diagnostics.
--
-- The contextual_inject hook (UserPromptSubmit / PreToolUse) now records
-- exposures so contextually-injected memories are rateable. SQLite cannot
-- ALTER a CHECK constraint, so session_memory_exposure is recreated to
-- widen source IN ('bootstrap','retrieve') to include 'contextual'.
-- No table holds a foreign key into session_memory_exposure.

CREATE TABLE session_memory_exposure_new (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve', 'contextual')),
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

-- Contextual-injection observability counters (R1=A in the spec).

INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_fired_userprompt', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_fired_pretool', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_injected', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_suppressed_floor', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_suppressed_dedup', 0);
