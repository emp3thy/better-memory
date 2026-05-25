"""Storage backend abstraction for better-memory.

Two implementations: SqliteBackend (default) wraps the existing services;
AgentCoreBackend (added in Plan 2) talks to AWS Bedrock AgentCore Memory.
Both satisfy the StorageBackend Protocol.
"""

from better_memory.storage.factory import build_backend
from better_memory.storage.protocol import (
    Outcome,
    StorageBackend,
    UseOutcome,
)
from better_memory.storage.sqlite import SqliteBackend

__all__ = [
    "Outcome",
    "SqliteBackend",
    "StorageBackend",
    "UseOutcome",
    "build_backend",
]
