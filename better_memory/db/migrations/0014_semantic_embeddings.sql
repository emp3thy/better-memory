-- Migration 0014: vector index for semantic memories.
--
-- reflection_embeddings has existed since 0002; semantic memories never got
-- the parallel table, so they have no semantic-search substrate. Written by
-- SemanticMemoryService on create/update, healed lazily on retrieve, and
-- backfilled once by cli.backfill_embeddings. 768 dims = nomic-embed-text,
-- matching observation_embeddings and reflection_embeddings.

CREATE VIRTUAL TABLE semantic_embeddings USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);
