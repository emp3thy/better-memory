"""Tests for `better-memory agentcore smoke`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

from better_memory.cli.agentcore import _handle_smoke


def _make_args(home: Path, region: str | None = None) -> argparse.Namespace:
    """Build an argparse.Namespace the handler accepts."""
    return argparse.Namespace(
        home=str(home),
        region=region,
        subcommand="smoke",
    )


def _write_config(home: Path) -> None:
    cfg = {
        "schema_version": 1,
        "region": "eu-west-2",
        "episodic": {
            "memory_id": "epi-X",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/epi-X",
            "memory_name": "better_memory_episodic",
            "strategy_id": "epi-strat",
            "strategy_name": "episodicReflections",
            "event_expiry_duration_days": 90,
        },
        "semantic": {
            "memory_id": "sem-X",
            "memory_arn": "arn:aws:bedrock-agentcore:eu-west-2:123:memory/sem-X",
            "memory_name": "better_memory_semantic",
            "strategy_id": "sem-strat",
            "strategy_name": "userPreference",
            "event_expiry_duration_days": 365,
        },
    }
    (home / "agentcore.json").write_text(json.dumps(cfg))


def test_smoke_exits_1_when_config_missing(tmp_path) -> None:
    rc = _handle_smoke(_make_args(tmp_path))
    assert rc == 1


def test_smoke_runs_full_cycle_against_existing_memories(
    tmp_path, monkeypatch
) -> None:
    _write_config(tmp_path)
    data = MagicMock(name="bedrock-agentcore")
    # CreateEvent (observation) + CreateEvent (closure)
    data.create_event.side_effect = [
        {"event": {"eventId": "evt-1"}},
        {"event": {"eventId": "evt-2"}},
    ]
    # ListEvents returns the two events
    data.list_events.return_value = {
        "events": [
            {"eventId": "evt-1", "sessionId": "smoke-sess"},
            {"eventId": "evt-2", "sessionId": "smoke-sess"},
        ]
    }
    # BatchCreateMemoryRecords for a semantic write — the SERVER mints the
    # durable id (live dialect); the smoke must consume it.
    data.batch_create_memory_records.return_value = {
        "successfulRecords": [
            {
                "memoryRecordId": "mem-11111111-2222-3333-4444-555555555555",
                "status": "SUCCEEDED",
                "requestIdentifier": "echoed-back",
            }
        ],
        "failedRecords": [],
    }
    # GetMemoryRecord readback (read-your-write — list_memory_records has a
    # ~60s indexing lag, so the smoke reads by id).
    data.get_memory_record.return_value = {
        "memoryRecord": {
            "memoryRecordId": "mem-11111111-2222-3333-4444-555555555555",
            "content": {"text": "smoke test semantic record"},
        }
    }
    # BatchDeleteMemoryRecords cleans up
    data.batch_delete_memory_records.return_value = {
        "successfulRecords": [
            {"memoryRecordId": "mem-11111111-2222-3333-4444-555555555555"}
        ],
        "failedRecords": [],
    }

    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_data_client",
        lambda region: data,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_smoke(_make_args(tmp_path))
    assert rc == 0
    assert data.create_event.call_count == 2
    assert data.list_events.call_count >= 1
    assert data.batch_create_memory_records.call_count == 1

    # Live-verified BatchCreate record shape (aws_record_dialect.md §1):
    # requestIdentifier + namespaces + content + timestamp; memoryRecordId
    # is NOT a legal input key (real botocore raises ParamValidationError).
    record = data.batch_create_memory_records.call_args.kwargs["records"][0]
    assert "memoryRecordId" not in record
    assert record["requestIdentifier"]
    assert record["namespaces"] == ["projects/smoke/semantic/"]
    assert record["content"] == {"text": "smoke test semantic record"}
    assert "timestamp" in record

    # Readback by the SERVER-minted id, then cleanup with the same id.
    get_kwargs = data.get_memory_record.call_args.kwargs
    assert (
        get_kwargs["memoryRecordId"]
        == "mem-11111111-2222-3333-4444-555555555555"
    )
    assert data.batch_delete_memory_records.call_count == 1
    delete_kwargs = data.batch_delete_memory_records.call_args.kwargs
    assert delete_kwargs["records"] == [
        {"memoryRecordId": "mem-11111111-2222-3333-4444-555555555555"}
    ]
    # The smoke never lists the lagging index.
    data.list_memory_records.assert_not_called()


def test_smoke_readback_retries_transient_404(tmp_path, monkeypatch) -> None:
    """get_memory_record is read-your-write at ~1s on real AWS; the smoke
    still retries a transient ResourceNotFoundException in the sub-second
    window instead of failing the whole run."""
    _write_config(tmp_path)

    class _FakeClientError(Exception):
        def __init__(self) -> None:
            super().__init__("not found yet")
            self.response = {"Error": {"Code": "ResourceNotFoundException"}}

    data = MagicMock(name="bedrock-agentcore")
    data.create_event.side_effect = [
        {"event": {"eventId": "evt-1"}},
        {"event": {"eventId": "evt-2"}},
    ]
    data.list_events.return_value = {
        "events": [{"eventId": "evt-1"}, {"eventId": "evt-2"}]
    }
    data.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "mem-retry-1", "status": "SUCCEEDED"}],
        "failedRecords": [],
    }
    data.get_memory_record.side_effect = [
        _FakeClientError(),
        {"memoryRecord": {"memoryRecordId": "mem-retry-1"}},
    ]
    data.batch_delete_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "mem-retry-1"}],
        "failedRecords": [],
    }
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_data_client",
        lambda region: data,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_smoke(_make_args(tmp_path))
    assert rc == 0
    assert data.get_memory_record.call_count == 2


def test_smoke_exits_1_when_any_step_fails(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    data = MagicMock()
    data.create_event.side_effect = RuntimeError("simulated AWS failure")
    monkeypatch.setattr(
        "better_memory.cli.agentcore._build_data_client",
        lambda region: data,
    )
    monkeypatch.setattr("better_memory.cli.agentcore.time.sleep", lambda _s: None)

    rc = _handle_smoke(_make_args(tmp_path))
    assert rc == 1
