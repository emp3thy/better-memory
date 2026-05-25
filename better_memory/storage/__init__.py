"""Storage backend abstraction for better-memory.

Two implementations: SqliteBackend (default) wraps the existing services;
AgentCoreBackend (added in Plan 2) talks to AWS Bedrock AgentCore Memory.
Both satisfy the StorageBackend Protocol.
"""

from better_memory.storage.protocol import (
    Outcome,
    StorageBackend,
    UseOutcome,
)

__all__ = ["Outcome", "StorageBackend", "UseOutcome"]
