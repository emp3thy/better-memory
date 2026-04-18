-- better-memory knowledge-base schema.
--
-- Separate DB from memory.db: stores indexed markdown documents from the
-- knowledge-base tree (standards/, languages/<lang>/, projects/<project>/).
-- See docs/superpowers/specs/2026-04-06-better-memory-design.md Section 4.
--
-- The ``schema_migrations`` table is bootstrapped per-connection by
-- ``better_memory.db.schema.apply_migrations``, so it is NOT re-created here.

----------------------------------------------------------------------
-- Documents
----------------------------------------------------------------------

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,    -- POSIX-style, relative to knowledge-base root
    scope TEXT NOT NULL
        CHECK(scope IN ('standard', 'language', 'project')),
    project TEXT,                 -- null for standards/languages
    language TEXT,                -- null for standards/projects
    content TEXT NOT NULL,
    last_indexed TIMESTAMP,
    file_mtime TIMESTAMP
);

CREATE INDEX idx_documents_scope ON documents(scope);
CREATE INDEX idx_documents_project ON documents(project);
CREATE INDEX idx_documents_language ON documents(language);

----------------------------------------------------------------------
-- Full-text index (external content — kept in sync via triggers)
----------------------------------------------------------------------

CREATE VIRTUAL TABLE document_fts USING fts5(
    content,
    path,
    content='documents',
    content_rowid='rowid'
);

CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO document_fts(rowid, content, path)
    VALUES (new.rowid, new.content, new.path);
END;

CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO document_fts(document_fts, rowid, content, path)
    VALUES ('delete', old.rowid, old.content, old.path);
END;

CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO document_fts(document_fts, rowid, content, path)
    VALUES ('delete', old.rowid, old.content, old.path);
    INSERT INTO document_fts(rowid, content, path)
    VALUES (new.rowid, new.content, new.path);
END;
