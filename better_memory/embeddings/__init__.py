"""Embedding clients for better-memory."""

from better_memory.embeddings.ollama import EmbeddingError, OllamaEmbedder
from better_memory.embeddings.tfidf import TfidfRetriever

__all__ = ["OllamaEmbedder", "EmbeddingError", "TfidfRetriever"]
