-- Migration 0018: local vector search removed; vec0 tables dropped.
-- sqlite-vec module is still loaded by db/connection.py this release,
-- which is what makes these DROPs legal. IF EXISTS keeps the script
-- idempotent, so the non-atomic executescript hazard (#27) cannot
-- strand it half-applied in a harmful state.
DROP TABLE IF EXISTS observation_embeddings;
DROP TABLE IF EXISTS reflection_embeddings;
DROP TABLE IF EXISTS semantic_embeddings;
