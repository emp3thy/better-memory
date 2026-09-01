"""MCP test fixtures and collection hooks.

Task 2 (remove-ollama-embeddings) deleted create_server's embedder
construction and Ollama probe entirely, so the subprocess-based MCP
integration tests (``test_server_integration.py``) no longer depend on a
reachable Ollama daemon — the auto-skip this module used to apply has
been removed.
"""

from __future__ import annotations
