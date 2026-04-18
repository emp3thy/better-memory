"""Search layer for better-memory (pure SQLite — no embedder dependency)."""

from better_memory.search.hybrid import SearchFilters, SearchResult, hybrid_search

__all__ = ["SearchFilters", "SearchResult", "hybrid_search"]
