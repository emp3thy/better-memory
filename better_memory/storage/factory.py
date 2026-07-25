"""Backend factory — picks SqliteBackend or AgentCoreBackend based on config."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from better_memory.storage.protocol import StorageBackend
from better_memory.storage.sqlite import SqliteBackend


class _ConfigLike(Protocol):
    """Structural type — the factory only reads storage_backend.

    Declared as a read-only property so frozen dataclasses (the real Config and
    test FakeConfigs) satisfy the Protocol — pyright treats a Protocol class
    attribute as read+write, which conflicts with frozen dataclasses' read-only
    fields.
    """

    @property
    def storage_backend(self) -> str: ...


def _resolve_home() -> Path:
    home = os.environ.get("BETTER_MEMORY_HOME", "~/.better-memory")
    return Path(home).expanduser()


def build_backend(
    *,
    config: _ConfigLike,
    memory_conn: sqlite3.Connection | None,
    embedder: Any = None,
    sync_embedder: Any = None,
    session_id: str | None,
    project: str,
) -> StorageBackend:
    """Construct the StorageBackend implementation appropriate for the config.

    ``sync_embedder``, when provided, must be the caller's already-built
    process-wide ``SyncEmbedder`` (see ``mcp/server.py``). It is forwarded
    as-is to ``SqliteBackend`` so its internal ``_semantic`` / ``_synthesis``
    services share the same circuit breaker as the caller's write-path
    tools instead of each backend build spinning up an independent one.
    Unused by the agentcore path, which has no local semantic/synthesis
    services of its own.
    """
    if config.storage_backend == "sqlite":
        if memory_conn is None:
            raise ValueError("sqlite backend requires memory_conn")
        return SqliteBackend(
            memory_conn=memory_conn,
            embedder=embedder,
            sync_embedder=sync_embedder,
            session_id=session_id,
            project=project,
        )
    if config.storage_backend == "agentcore":
        # Imports are local so sqlite-only deployments don't require boto3 /
        # botocore to be installed (they're declared in the optional
        # `agentcore` extra, not the runtime dependencies).
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise ModuleNotFoundError(
                "boto3 is required for the agentcore storage backend. "
                "Install it with: pip install 'better-memory[agentcore]'"
            ) from exc

        from better_memory.storage.agentcore import AgentCoreBackend
        from better_memory.storage.agentcore_persistence import (
            load_agentcore_config,
        )

        home = _resolve_home()
        ac_cfg = load_agentcore_config(home)
        if ac_cfg is None:
            raise FileNotFoundError(
                f"{home}/agentcore.json not found. Run `better-memory agentcore init` "
                f"to create the memory resources and persist their IDs."
            )

        # Region is single-sourced from agentcore.json — the file that also
        # carries the memory ids — so every client (server plane and hook
        # plane) signs against the region the resources were created in.
        boto_config = BotoConfig(
            region_name=ac_cfg.region,
            retries={"mode": "standard", "max_attempts": 5},
        )
        data_client = boto3.client("bedrock-agentcore", config=boto_config)
        control_client = boto3.client("bedrock-agentcore-control", config=boto_config)
        return AgentCoreBackend(
            config=ac_cfg,
            data_client=data_client,
            control_client=control_client,
            session_id=session_id,
            project=project,
            # Agentcore mode never stores memory CONTENT locally, but
            # session-operational state (exposure ledger, migration ledger,
            # hook errors) lives in the local memory.db regardless of
            # backend — thread the caller's connection through so
            # record_exposures / list_session_exposures have a ledger to
            # write to. None when the caller has none (memory_conn is
            # itself optional here, unlike the sqlite branch above).
            local_conn=memory_conn,
        )
    raise ValueError(f"unknown storage_backend={config.storage_backend!r}")
