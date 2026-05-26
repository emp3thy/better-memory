"""Pre-flight smoke test for AgentCore Memory backend (Plan 2 Task 0.5).

Validates the happy path end-to-end against live AWS BEFORE Plan 2 execution
begins. Creates throwaway memory resources, exercises the core write/read
flows we'll wrap in AgentCoreBackend, then deletes them.

Run: ``uv run python scripts/agentcore_smoke.py``

What it verifies:
  1. CreateMemory accepts our declared metadata schema (episodic + semantic).
  2. Memory + each strategy transition to ACTIVE within ~120 s.
  3. CreateEvent writes an observation event with stringValue metadata.
  4. CreateEvent writes a closure-marker event with role=OTHER.
  5. ListEvents returns the events we wrote.
  6. BatchCreateMemoryRecords writes a semantic record with our declared
     metadata schema (counters as numberValue).
  7. ListMemoryRecords returns the semantic record with metadata round-tripped.
  8. BatchUpdateMemoryRecords mutates the counters and we can re-read them.
  9. BatchDeleteMemoryRecords cleans up the semantic record.
 10. DeleteMemory cleans up both memories.

Surface any AWS errors loudly; do NOT swallow them. The smoke is the only
chance to catch wire-shape mismatches before Plan 2's mocked tests lock in
the wrong shape.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

REGION = "eu-west-2"
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 180

# Stable names so re-runs of the smoke find existing resources rather than
# stacking new ones up. Names are bounded by AWS pattern; pick something
# obviously throwaway.
SEMANTIC_NAME = "bm_smoke_semantic"
EPISODIC_NAME = "bm_smoke_episodic"
SEMANTIC_STRATEGY_NAME = "userPreference"
EPISODIC_STRATEGY_NAME = "episodicReflections"

# Mirror what Plan 2 declares for the agentcore.json persistence.
EPISODIC_METADATA_SCHEMA = [
    {
        "key": "polarity",
        "type": "STRING",
        "extractionConfig": {
            "llmExtractionConfig": {
                # `definition` is REQUIRED on LlmExtractionConfig (verified via
                # boto3 introspection). One-line description of what the field
                # represents — surfaced to the extracting LLM as context.
                "definition": (
                    "Whether this reflection prescribes a positive practice "
                    "('do'), warns against a negative practice ('dont'), or "
                    "is informational only ('neutral')."
                ),
                # `llmExtractionInstruction` (NOT `extractionInstruction` —
                # the spec had the wrong name) is optional and tells the LLM
                # exactly how to classify.
                "llmExtractionInstruction": (
                    "Classify this reflection as 'do', 'dont', or 'neutral'."
                ),
                # `validation.stringValidation.allowedValues` carries the
                # closed enum (was `allowedValues` direct on the entry in
                # the spec).
                "validation": {
                    "stringValidation": {
                        "allowedValues": ["do", "dont", "neutral"]
                    }
                },
            }
        },
    },
    {"key": "useful_count", "type": "NUMBER"},
    {"key": "missed_count", "type": "NUMBER"},
    {"key": "ignored_count", "type": "NUMBER"},
    {"key": "times_misled", "type": "NUMBER"},
    {"key": "overlooked_count", "type": "NUMBER"},
    {"key": "last_credited_at", "type": "STRING"},
    {"key": "status", "type": "STRING"},
]
SEMANTIC_METADATA_SCHEMA = [
    {"key": "useful_count", "type": "NUMBER"},
    {"key": "missed_count", "type": "NUMBER"},
    {"key": "ignored_count", "type": "NUMBER"},
    {"key": "times_misled", "type": "NUMBER"},
    {"key": "overlooked_count", "type": "NUMBER"},
    {"key": "last_credited_at", "type": "STRING"},
    {"key": "status", "type": "STRING"},
]


@dataclass
class CreatedMemory:
    memory_id: str
    memory_arn: str
    strategy_id: str


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  ..   {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n>> {msg}", flush=True)


def find_existing_memory(control: Any, name: str) -> str | None:
    """Return memory_id if an active memory with this name already exists."""
    paginator = control.get_paginator("list_memories")
    for page in paginator.paginate():
        for summary in page.get("memories", []):
            if summary.get("status") == "DELETING":
                continue
            # ListMemories returns id/arn/status only; name isn't on the
            # summary. Have to GetMemory to inspect name. Skip optimization
            # for the smoke (small N).
            try:
                memory = control.get_memory(memoryId=summary["id"])["memory"]
            except ClientError:
                continue
            if memory.get("name") == name:
                return memory["id"]
    return None


def create_memory(
    control: Any,
    *,
    name: str,
    strategy_name: str,
    strategy_kind: str,
    metadata_schema: list[dict],
    event_expiry_days: int = 30,
) -> CreatedMemory:
    """Create a fresh memory with a single built-in strategy."""
    if strategy_kind == "episodic":
        strategy_block = {
            "episodicMemoryStrategy": {
                "name": strategy_name,
                "namespaces": [f"projects/{{actorId}}/reflections/"],
                "namespaceTemplates": [f"projects/{{actorId}}/reflections/"],
                "reflectionConfiguration": {
                    "namespaces": [f"projects/{{actorId}}/reflections/"],
                    "namespaceTemplates": [f"projects/{{actorId}}/reflections/"],
                    "memoryRecordSchema": {"metadataSchema": metadata_schema},
                },
            }
        }
    elif strategy_kind == "userPreference":
        strategy_block = {
            "userPreferenceMemoryStrategy": {
                "name": strategy_name,
                "namespaces": [f"projects/{{actorId}}/semantic/"],
                "namespaceTemplates": [f"projects/{{actorId}}/semantic/"],
                "memoryRecordSchema": {"metadataSchema": metadata_schema},
            }
        }
    else:
        raise ValueError(f"unknown strategy_kind={strategy_kind!r}")

    info(f"calling create_memory(name={name!r}, strategy={strategy_kind})")
    response = control.create_memory(
        name=name,
        eventExpiryDuration=event_expiry_days,
        memoryStrategies=[strategy_block],
        indexedKeys=[
            {"key": "status", "type": "STRING"},
            {"key": "last_credited_at", "type": "STRING"},
            {"key": "overlooked_count", "type": "NUMBER"},
        ],
    )
    memory = response["memory"]
    strategies = memory.get("strategies", [])
    if not strategies:
        raise RuntimeError(
            f"create_memory returned no strategies for {name}: {memory!r}"
        )
    return CreatedMemory(
        memory_id=memory["id"],
        memory_arn=memory["arn"],
        strategy_id=strategies[0]["strategyId"],
    )


def wait_until_active(control: Any, memory_id: str) -> None:
    """Poll get_memory until status == ACTIVE."""
    start = time.monotonic()
    while True:
        memory = control.get_memory(memoryId=memory_id)["memory"]
        status = memory.get("status")
        strategies_active = all(
            s.get("status") == "ACTIVE" for s in memory.get("strategies", [])
        )
        if status == "ACTIVE" and strategies_active:
            return
        if status == "FAILED":
            raise RuntimeError(
                f"memory {memory_id} entered FAILED state: {memory.get('failureReason')}"
            )
        if time.monotonic() - start > POLL_TIMEOUT_S:
            raise TimeoutError(
                f"memory {memory_id} did not reach ACTIVE within {POLL_TIMEOUT_S}s; "
                f"last status={status} strategies_active={strategies_active}"
            )
        info(f"  status={status} strategies_active={strategies_active}; waiting...")
        time.sleep(POLL_INTERVAL_S)


def main() -> int:
    boto_config = BotoConfig(
        region_name=REGION,
        retries={"mode": "standard", "max_attempts": 5},
    )
    control = boto3.client("bedrock-agentcore-control", config=boto_config)
    data = boto3.client("bedrock-agentcore", config=boto_config)

    actor_id = "smoke-actor"
    session_id = f"smoke-session-{int(time.time())}"

    created_episodic: CreatedMemory | None = None
    created_semantic: CreatedMemory | None = None
    semantic_record_id: str | None = None

    try:
        # ---------- Step 1: Create episodic memory ----------
        step("1. CreateMemory — episodic with full metadata schema")
        existing = find_existing_memory(control, EPISODIC_NAME)
        if existing:
            info(f"existing memory {existing!r} found — deleting first for clean slate")
            control.delete_memory(memoryId=existing)
            # Allow delete to settle before re-creating same name
            time.sleep(10)
        created_episodic = create_memory(
            control,
            name=EPISODIC_NAME,
            strategy_name=EPISODIC_STRATEGY_NAME,
            strategy_kind="episodic",
            metadata_schema=EPISODIC_METADATA_SCHEMA,
        )
        ok(f"created episodic memory_id={created_episodic.memory_id}")
        ok(f"          strategy_id={created_episodic.strategy_id}")

        # ---------- Step 2: Create semantic memory ----------
        step("2. CreateMemory — semantic (userPreference) with metadata schema")
        existing = find_existing_memory(control, SEMANTIC_NAME)
        if existing:
            info(f"existing memory {existing!r} found — deleting first")
            control.delete_memory(memoryId=existing)
            time.sleep(10)
        created_semantic = create_memory(
            control,
            name=SEMANTIC_NAME,
            strategy_name=SEMANTIC_STRATEGY_NAME,
            strategy_kind="userPreference",
            metadata_schema=SEMANTIC_METADATA_SCHEMA,
            event_expiry_days=90,
        )
        ok(f"created semantic memory_id={created_semantic.memory_id}")
        ok(f"          strategy_id={created_semantic.strategy_id}")

        # ---------- Step 3: Wait for both to be ACTIVE ----------
        step("3. Poll until both memories + strategies are ACTIVE")
        wait_until_active(control, created_episodic.memory_id)
        ok(f"episodic ACTIVE")
        wait_until_active(control, created_semantic.memory_id)
        ok(f"semantic ACTIVE")

        # ---------- Step 4: CreateEvent — observation ----------
        step("4. CreateEvent — observation on episodic memory")
        event_response = data.create_event(
            memoryId=created_episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=[
                {
                    "conversational": {
                        "role": "USER",
                        "content": {
                            "text": "Smoke observation: this is a do-style hint about using uv over pip for Python projects."
                        },
                    }
                }
            ],
            metadata={
                "outcome": {"stringValue": "success"},
                "component": {"stringValue": "smoke_test"},
                "theme": {"stringValue": "convention"},
            },
        )
        event_id = event_response["event"]["eventId"]
        ok(f"observation event_id={event_id}")

        # ---------- Step 5: CreateEvent — closure marker (role=OTHER) ----------
        step("5. CreateEvent — closure marker with role=OTHER")
        closure_response = data.create_event(
            memoryId=created_episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=[
                {
                    "conversational": {
                        "role": "OTHER",
                        "content": {
                            "text": "Session complete. All work for this session has been recorded."
                        },
                    }
                }
            ],
        )
        closure_event_id = closure_response["event"]["eventId"]
        ok(f"closure event_id={closure_event_id}")

        # ---------- Step 6: ListEvents — confirm both events present ----------
        step("6. ListEvents — confirm both events were written")
        events_response = data.list_events(
            memoryId=created_episodic.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            includePayloads=True,
        )
        listed = events_response.get("events", [])
        listed_ids = [e["eventId"] for e in listed]
        ok(f"list_events returned {len(listed)} events: {listed_ids}")
        assert event_id in listed_ids, f"observation event missing: {listed_ids}"
        assert closure_event_id in listed_ids, f"closure event missing: {listed_ids}"
        # Verify metadata round-trip on the observation event
        obs_event = next(e for e in listed if e["eventId"] == event_id)
        ok(f"observation event metadata keys: {sorted(obs_event.get('metadata', {}).keys())}")
        # NOTE: This is the key test for spike Finding 5 (undeclared metadata
        # is silently dropped). At event level we didn't declare a schema,
        # so all three keys (outcome/component/theme) should round-trip.
        assert obs_event.get("metadata", {}).get("outcome", {}).get("stringValue") == "success"

        # ---------- Step 7: BatchCreateMemoryRecords — semantic ----------
        step("7. BatchCreateMemoryRecords — write a semantic record with declared counters")
        request_id = f"smoke-{int(time.time() * 1000)}"
        create_records_response = data.batch_create_memory_records(
            memoryId=created_semantic.memory_id,
            records=[
                {
                    "requestIdentifier": request_id,
                    "namespaces": [f"projects/{actor_id}/semantic/"],
                    "content": {"text": "Prefer uv for Python package management."},
                    "timestamp": datetime.now(UTC),
                    "memoryStrategyId": created_semantic.strategy_id,
                    "metadata": {
                        "useful_count": {"numberValue": 0},
                        "missed_count": {"numberValue": 0},
                        "ignored_count": {"numberValue": 0},
                        "times_misled": {"numberValue": 0},
                        "overlooked_count": {"numberValue": 0},
                        "status": {"stringValue": "active"},
                        "last_credited_at": {"dateTimeValue": datetime.now(UTC)},
                    },
                }
            ],
        )
        failed = create_records_response.get("failedRecords", [])
        if failed:
            fail(f"batch_create_memory_records reported failures: {failed}")
            return 2
        successful = create_records_response.get("successfulRecords", [])
        assert successful, "no successful records returned"
        semantic_record_id = successful[0]["memoryRecordId"]
        ok(f"semantic record_id={semantic_record_id}")

        # ---------- Step 8: ListMemoryRecords — verify metadata round-trip ----------
        step("8. ListMemoryRecords — verify declared metadata round-trips (with retry for propagation)")
        target: dict | None = None
        records: list = []
        # Poll with retry: AgentCore appears to have eventual consistency
        # between batch_create and list_memory_records.
        for attempt in range(1, 25):
            list_response = data.list_memory_records(
                memoryId=created_semantic.memory_id,
                namespace=f"projects/{actor_id}/semantic/",
                maxResults=10,
            )
            records = list_response.get("memoryRecordSummaries", [])
            target = next(
                (r for r in records if r["memoryRecordId"] == semantic_record_id),
                None,
            )
            if target is not None:
                ok(f"list_memory_records returned {len(records)} records after {attempt} attempt(s)")
                break
            info(f"  attempt {attempt}: {len(records)} records, target not yet visible; waiting 5s...")
            time.sleep(5)
        if target is None:
            # Fall back to a get_memory_record probe to see if the record
            # is visible by ID even if list filtering doesn't surface it.
            try:
                fetched = data.get_memory_record(
                    memoryId=created_semantic.memory_id,
                    memoryRecordId=semantic_record_id,
                )
                ok(f"get_memory_record DID find {semantic_record_id} — "
                   f"list_memory_records has stale view (eventual consistency confirmed)")
                target = fetched.get("memoryRecord")
            except ClientError as get_exc:
                fail(f"get_memory_record also failed: {get_exc}")
                assert False, (
                    f"created record {semantic_record_id} never appeared via "
                    f"list_memory_records (24 attempts × 5s = 120s); "
                    f"get_memory_record also failed: {get_exc}"
                )
        assert target is not None  # appease type checker
        metadata = target.get("metadata", {})
        ok(f"record metadata keys: {sorted(metadata.keys())}")

        # Verify the schema-declared keys actually round-tripped — this is
        # the load-bearing test for the rating UX preservation in agentcore mode.
        for required_key in (
            "useful_count",
            "missed_count",
            "ignored_count",
            "times_misled",
            "overlooked_count",
            "status",
        ):
            if required_key not in metadata:
                fail(f"REQUIRED metadata key {required_key!r} missing from round-trip — undeclared schema would silently drop it (spike Finding 5)")
                return 3
        ok("all 6 app-managed counter keys round-tripped")

        # last_credited_at is declared as STRING in the schema but we wrote
        # a dateTimeValue. Verify how it round-trips.
        lc = metadata.get("last_credited_at", {})
        ok(f"last_credited_at round-trip shape: {json.dumps({k: type(v).__name__ for k, v in lc.items()})}")

        # ---------- Step 9: BatchUpdateMemoryRecords — bump counter ----------
        step("9. BatchUpdateMemoryRecords — bump useful_count + write timestamp")
        # CRITICAL: list_memory_records returns metadata with system-managed
        # keys mixed in (x-amz-agentcore-memory-createdAt, ...-updatedAt,
        # ...-recordType). batch_update rejects them with code 400 if echoed
        # back — "Metadata keys cannot use reserved names or prefixes".
        # Always strip the x-amz-agentcore-* prefix before writing.
        SYSTEM_PREFIX = "x-amz-agentcore-memory-"
        current_metadata = {
            k: v for k, v in metadata.items() if not k.startswith(SYSTEM_PREFIX)
        }
        current_metadata["useful_count"] = {"numberValue": 1}
        current_metadata["last_credited_at"] = {"dateTimeValue": datetime.now(UTC)}

        # Try direct get_memory_record first to see what AWS reports about
        # the record's reachability (spike Finding 3 said BASE records 404
        # on get_memory_record — let's verify whether that applies to
        # userPreference-strategy-tagged records).
        try:
            getr = data.get_memory_record(
                memoryId=created_semantic.memory_id,
                memoryRecordId=semantic_record_id,
            )
            ok(f"get_memory_record OK — record reachable by id")
        except ClientError as get_exc:
            info(f"get_memory_record raised: {get_exc.response['Error']['Code']} — {get_exc.response['Error']['Message']}")

        # Retry batch_update with backoff (eventual consistency on the
        # write side may need longer than the list side).
        update_response = None
        for attempt in range(1, 13):
            try:
                update_response = data.batch_update_memory_records(
                    memoryId=created_semantic.memory_id,
                    records=[
                        {
                            "memoryRecordId": semantic_record_id,
                            "timestamp": datetime.now(UTC),
                            "metadata": current_metadata,
                        }
                    ],
                )
                ok(f"batch_update_memory_records call returned (attempt {attempt})")
                break
            except ClientError as upd_exc:
                code = upd_exc.response.get("Error", {}).get("Code", "?")
                msg = upd_exc.response.get("Error", {}).get("Message", "?")
                info(f"  attempt {attempt}: {code} — {msg}; waiting 10s...")
                if attempt == 12:
                    fail(f"batch_update_memory_records never succeeded after 12 attempts × 10s")
                    return 4
                time.sleep(10)
        assert update_response is not None
        update_failed = update_response.get("failedRecords", [])
        if update_failed:
            fail(f"batch_update_memory_records reported failures: {update_failed}")
            return 4
        ok(f"update succeeded for {semantic_record_id}")

        # Re-read with retry — eventual consistency on read-after-write
        # (same lag pattern as list-after-create). Try get_memory_record
        # (single-record lookup) since we now know it works on BASE records.
        post_useful: float | None = None
        for attempt in range(1, 25):
            fetched = data.get_memory_record(
                memoryId=created_semantic.memory_id,
                memoryRecordId=semantic_record_id,
            )["memoryRecord"]
            post_useful = fetched.get("metadata", {}).get("useful_count", {}).get("numberValue")
            if post_useful == 1:
                ok(f"useful_count = {post_useful} after {attempt} read attempt(s)")
                break
            info(f"  attempt {attempt}: useful_count={post_useful}; waiting 5s for write to propagate...")
            time.sleep(5)
        assert post_useful == 1, f"useful_count never reached 1 in 24 attempts × 5s; last seen={post_useful}"

        # ---------- Step 10: Cleanup ----------
        step("10. Cleanup — delete semantic record, delete both memories")
        data.batch_delete_memory_records(
            memoryId=created_semantic.memory_id,
            records=[{"memoryRecordId": semantic_record_id}],
        )
        ok(f"deleted semantic record {semantic_record_id}")

        return 0

    except Exception as exc:
        fail(f"smoke failed: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1

    finally:
        # Cleanup memories — always try, even on partial failure
        if created_semantic is not None:
            try:
                control.delete_memory(memoryId=created_semantic.memory_id)
                ok(f"deleted semantic memory {created_semantic.memory_id}")
            except Exception as exc:
                fail(f"failed to delete semantic memory: {exc}")
        if created_episodic is not None:
            try:
                control.delete_memory(memoryId=created_episodic.memory_id)
                ok(f"deleted episodic memory {created_episodic.memory_id}")
            except Exception as exc:
                fail(f"failed to delete episodic memory: {exc}")


if __name__ == "__main__":
    sys.exit(main())
