"""Fixtures for live-AWS AgentCore integration tests.

Gated by ``BETTER_MEMORY_TEST_AGENTCORE=1``. CI does NOT set this. Local
runs need valid AWS credentials in the environment (boto3's default
discovery chain).

**Setup tax: ~3 minutes** per pytest session. Two memories created
sequentially, each ~90-115s. Tests are session-scoped so this cost is
paid once.

Teardown strategy:
- pytest fixture teardown deletes both memories on clean exit.
- atexit handler deletes them on Ctrl-C / SIGTERM / hard kill — same
  cleanup work, just registered on the interpreter's exit path so any
  abnormal termination still cleans up.
- ``BETTER_MEMORY_TEST_AGENTCORE_KEEP=1`` skips teardown for debugging.

Before fixture setup runs, ``_sweep_stale_memories`` lists every
``bm_int_*`` memory and deletes any older than 1h — catches leaked state
from previously interrupted runs (network drop, OS reboot, etc.) so
re-runs don't accumulate orphans in the AWS account.
"""

from __future__ import annotations

import atexit
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

_STALE_MEMORY_PREFIX = "bm_int_"
_STALE_MEMORY_AGE = timedelta(hours=1)


def _agentcore_enabled() -> bool:
    return os.environ.get("BETTER_MEMORY_TEST_AGENTCORE") == "1"


def _keep_after_test() -> bool:
    return os.environ.get("BETTER_MEMORY_TEST_AGENTCORE_KEEP") == "1"


def _sweep_stale_memories(control: Any) -> None:
    """Delete any ``bm_int_*`` memories older than 1h. Catches leaked state
    from previously interrupted test runs."""
    cutoff = datetime.now(UTC) - _STALE_MEMORY_AGE
    paginator = control.get_paginator("list_memories")
    for page in paginator.paginate():
        for summary in page.get("memories", []):
            if summary.get("status") == "DELETING":
                continue
            try:
                memory = control.get_memory(memoryId=summary["id"])["memory"]
            except Exception:
                continue
            name = memory.get("name", "")
            if not name.startswith(_STALE_MEMORY_PREFIX):
                continue
            created = memory.get("createdAt")
            if not isinstance(created, datetime):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                try:
                    print(f"  sweeping stale memory {name} (id={memory['id']})")
                    control.delete_memory(memoryId=memory["id"])
                except Exception as exc:
                    print(f"  WARN: failed to sweep {memory['id']}: {exc!r}")


@pytest.fixture(scope="session")
def agentcore_region() -> str:
    return os.environ.get("BETTER_MEMORY_TEST_AGENTCORE_REGION", "eu-west-2")


@pytest.fixture(scope="session")
def agentcore_throwaway_memories(agentcore_region: str):
    """Provision (semantic + episodic) memories; yield (semantic, episodic)
    AgentCoreConfig.MemoryRecord pair; delete on teardown."""
    if not _agentcore_enabled():
        pytest.skip("Set BETTER_MEMORY_TEST_AGENTCORE=1 to run real-AWS tests.")

    import boto3
    from botocore.config import Config as BotoConfig

    from better_memory.cli._agentcore_strategies import (
        DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS,
        DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS,
        INDEXED_KEYS,
        episodic_strategy_block,
        semantic_strategy_block,
    )
    from better_memory.storage.agentcore_persistence import MemoryRecord

    suffix = uuid.uuid4().hex[:8]
    epi_name = f"bm_int_epi_{suffix}"
    sem_name = f"bm_int_sem_{suffix}"

    control = boto3.client(
        "bedrock-agentcore-control",
        config=BotoConfig(
            region_name=agentcore_region,
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )

    # Sweep before creating so stale leaks from previously interrupted runs
    # are cleaned up — best-effort, errors logged not raised.
    try:
        _sweep_stale_memories(control)
    except Exception as exc:
        print(f"  WARN: stale-memory sweep failed: {exc!r}")

    def _create(name: str, strategy_block: dict, expiry_days: int) -> dict:
        response = control.create_memory(
            name=name,
            eventExpiryDuration=expiry_days,
            memoryStrategies=[strategy_block],
            indexedKeys=INDEXED_KEYS,
        )
        return response["memory"]

    def _poll_active(memory_id: str, *, timeout: int = 240, interval: int = 5) -> dict:
        """Poll until ACTIVE — slow path, ~90-115s typical, allow 4min."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            response = control.get_memory(memoryId=memory_id)
            memory = response["memory"]
            status = memory.get("status")
            strategies = memory.get("strategies") or []
            if (
                status == "ACTIVE"
                and strategies
                and all(s.get("status") == "ACTIVE" for s in strategies)
            ):
                return memory
            time.sleep(interval)
        raise TimeoutError(f"memory {memory_id} did not become ACTIVE in {timeout}s")

    epi_initial = _create(
        epi_name, episodic_strategy_block(), DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS
    )
    sem_initial = _create(
        sem_name, semantic_strategy_block(), DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS
    )

    # Register atexit teardown IMMEDIATELY after create — so any abnormal
    # termination (Ctrl-C, SIGTERM, pytest hard-kill) still triggers cleanup.
    # `_cleanup` is idempotent — pytest fixture teardown runs it too on the
    # happy path; the second call is a no-op since the memories are gone.
    cleaned_up: list[bool] = []

    def _cleanup() -> None:
        if cleaned_up or _keep_after_test():
            return
        cleaned_up.append(True)
        for mid in (epi_initial["id"], sem_initial["id"]):
            try:
                control.delete_memory(memoryId=mid)
            except Exception as exc:
                print(f"WARN: atexit failed to delete {mid}: {exc!r}")

    atexit.register(_cleanup)

    epi_active = _poll_active(epi_initial["id"])
    sem_active = _poll_active(sem_initial["id"])

    def _to_record(
        active: dict, expiry_days: int, default_strategy_name: str
    ) -> MemoryRecord:
        strategies = active.get("strategies") or []
        if not strategies:
            raise RuntimeError(
                f"memory {active.get('id')} ACTIVE but reports no strategies"
            )
        return MemoryRecord(
            memory_id=active["id"],
            memory_arn=active["arn"],
            memory_name=active.get("name", ""),
            strategy_id=strategies[0]["strategyId"],
            strategy_name=strategies[0].get("name", default_strategy_name),
            event_expiry_duration_days=expiry_days,
        )

    epi_record = _to_record(
        epi_active, DEFAULT_EPISODIC_EVENT_EXPIRY_DAYS, "episodicReflections"
    )
    sem_record = _to_record(
        sem_active, DEFAULT_SEMANTIC_EVENT_EXPIRY_DAYS, "userPreference"
    )

    yield sem_record, epi_record

    # Clean teardown — same code as atexit (idempotent).
    _cleanup()


@pytest.fixture
def agentcore_backend(agentcore_throwaway_memories, agentcore_region: str):
    """Construct an AgentCoreBackend pointing at the throwaway memories."""
    import boto3
    from botocore.config import Config as BotoConfig

    from better_memory.storage.agentcore import AgentCoreBackend
    from better_memory.storage.agentcore_persistence import AgentCoreConfig

    sem_record, epi_record = agentcore_throwaway_memories
    cfg = AgentCoreConfig(
        schema_version=1,
        region=agentcore_region,
        semantic=sem_record,
        episodic=epi_record,
    )

    boto_config = BotoConfig(
        region_name=agentcore_region,
        retries={"mode": "standard", "max_attempts": 5},
    )
    data_client = boto3.client("bedrock-agentcore", config=boto_config)
    control_client = boto3.client("bedrock-agentcore-control", config=boto_config)

    return AgentCoreBackend(
        config=cfg,
        data_client=data_client,
        control_client=control_client,
        session_id=f"int-test-{uuid.uuid4().hex[:8]}",
        project="integration",
    )
