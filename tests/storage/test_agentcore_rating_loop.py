"""E2E tests for the agentcore `apply_session_ratings` real sweep (Task 7,
agentcore-parity design §2). Boto3 clients are MagicMock stubs (unit-test
style, matching test_agentcore_unit.py); the local exposure ledger is a real
tmp sqlite connection with migrations applied, exercising the actual
exposure_log primitives end to end.

Covers: full sweep e2e (exposures -> sweep -> local stamps + AWS counter
bumps + one receipt CreateEvent), evidence rejection parity with sqlite,
'ignored' accepted in the sweep only, event-failure isolation, the
not_exposed/already_rated skip buckets, and the local_conn=None no-op
degrade path.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.storage.agentcore import AgentCoreBackend
from better_memory.storage.agentcore_persistence import AgentCoreConfig, MemoryRecord


@pytest.fixture
def ac_config() -> AgentCoreConfig:
    return AgentCoreConfig(
        schema_version=1,
        region="eu-west-2",
        semantic=MemoryRecord(
            memory_id="mem-sem-abc1234567",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-sem-abc1234567",
            memory_name="better-memory-semantic",
            strategy_id="userPreference-zXy1234567",
            strategy_name="userPreference",
            event_expiry_duration_days=365,
        ),
        episodic=MemoryRecord(
            memory_id="mem-epi-def4567890",
            memory_arn="arn:aws:bedrock-agentcore:eu-west-2:123:memory/mem-epi-def4567890",
            memory_name="better-memory-episodic",
            strategy_id="episodicReflections-qPr9876543",
            strategy_name="episodicReflections",
            event_expiry_duration_days=90,
        ),
    )


@pytest.fixture
def mock_data_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-data")


@pytest.fixture
def mock_control_client() -> MagicMock:
    return MagicMock(name="bedrock-agentcore-control")


@pytest.fixture
def local_conn(tmp_path):
    conn = connect(tmp_path / "ledger.db")
    try:
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def backend(ac_config, mock_data_client, mock_control_client) -> AgentCoreBackend:
    """No local_conn wired — exercises the degrade-to-current-behaviour path."""
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
    )


@pytest.fixture
def backend_with_ledger(
    ac_config, mock_data_client, mock_control_client, local_conn
) -> AgentCoreBackend:
    return AgentCoreBackend(
        config=ac_config,
        data_client=mock_data_client,
        control_client=mock_control_client,
        session_id="test-session-xyz",
        project="testproj",
        local_conn=local_conn,
    )


def _make_record_response(rec_id: str, **counters) -> dict:
    base = {
        "useful_count": 0, "missed_count": 0, "ignored_count": 0,
        "times_misled": 0, "overlooked_count": 0,
    }
    base.update(counters)
    return {
        "memoryRecord": {
            "memoryRecordId": rec_id,
            "content": {"text": "{}"},
            "memoryStrategyId": "episodicReflections-qPr9876543",
            "namespaces": ["projects/testproj/reflections/"],
            "createdAt": datetime(2026, 5, 24, tzinfo=UTC),
            "metadata": {
                **{k: {"numberValue": v} for k, v in base.items()},
                "status": {"stringValue": "active"},
                "polarity": {"stringValue": "do"},
            },
        }
    }


def _ok_update_response(rec_id: str) -> dict:
    return {
        "successfulRecords": [{"memoryRecordId": rec_id, "status": "SUCCEEDED"}],
        "failedRecords": [],
    }


def _seed_local_reflection(conn, rid: str, *, title: str) -> None:
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count)
           VALUES (?, ?, 'testproj', 'general', 'do', 'context', '[]',
                   0.8, '2026-01-01', '2026-01-01', 0)""",
        (rid, title),
    )
    conn.commit()


class _FakeClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError, mirroring the pattern
    already established in test_agentcore_unit.py (e.g.
    test_record_use_retries_on_transient_404): carries the `.response`
    dict boto3 errors expose, and is wired in via monkeypatching
    `agentcore._ClientError` so the source's `isinstance(exc, _ClientError)`
    check matches it regardless of whether real botocore is installed."""

    def __init__(self, code: str = "ThrottlingException", message: str = "rate exceeded") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class TestFullSweepE2E:
    def test_sweep_stamps_local_rows_bumps_counters_and_emits_one_event(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        for rid in ("r-cited", "r-ignored", "r-misled"):
            _seed_local_reflection(local_conn, rid, title=f"title-{rid}")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[
                ("reflection", "r-cited", None),
                ("reflection", "r-ignored", None),
                ("reflection", "r-misled", None),
            ],
            source="contextual",
        )

        mock_data_client.get_memory_record.side_effect = [
            _make_record_response("r-cited"),
            _make_record_response("r-ignored"),
            _make_record_response("r-misled"),
        ]
        mock_data_client.batch_update_memory_records.side_effect = [
            _ok_update_response("r-cited"),
            _ok_update_response("r-ignored"),
            _ok_update_response("r-misled"),
        ]
        mock_data_client.create_event.return_value = {"event": {"eventId": "evt-1"}}

        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r-cited", "class": "cited",
                 "evidence": "cited it directly"},
                {"kind": "reflection", "id": "r-ignored", "class": "ignored"},
                {"kind": "reflection", "id": "r-misled", "class": "misled",
                 "evidence": "led me down the wrong path"},
            ],
        )

        assert result["applied"] == {
            "cited": 1, "shaped": 0, "ignored": 1, "misled": 1, "overlooked": 0,
        }
        assert result["skipped"] == {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }

        # (3) counter push per non-skip entry, incl. ignored -> ignored_count.
        assert mock_data_client.batch_update_memory_records.call_count == 3
        calls = mock_data_client.batch_update_memory_records.call_args_list
        sent_by_id = {
            c.kwargs["records"][0]["memoryRecordId"]: c.kwargs["records"][0]
            for c in calls
        }
        assert sent_by_id["r-cited"]["metadata"]["useful_count"]["numberValue"] == 1
        assert sent_by_id["r-ignored"]["metadata"]["ignored_count"]["numberValue"] == 1
        assert sent_by_id["r-misled"]["metadata"]["times_misled"]["numberValue"] == 1

        # (2) local rows stamped with rated_at/classification/evidence.
        rows = {
            r["memory_id"]: r
            for r in local_conn.execute(
                "SELECT memory_id, rated_at, classification, evidence "
                "FROM session_memory_exposure"
            ).fetchall()
        }
        assert rows["r-cited"]["rated_at"] is not None
        assert rows["r-cited"]["classification"] == "cited"
        assert rows["r-cited"]["evidence"] == "cited it directly"
        assert rows["r-ignored"]["rated_at"] is not None
        assert rows["r-ignored"]["classification"] == "ignored"
        assert rows["r-ignored"]["evidence"] is None
        assert rows["r-misled"]["rated_at"] is not None
        assert rows["r-misled"]["evidence"] == "led me down the wrong path"

        # (4) exactly one best-effort receipt CreateEvent.
        mock_data_client.create_event.assert_called_once()
        kwargs = mock_data_client.create_event.call_args.kwargs
        assert kwargs["extractionMode"] == "SKIP"
        assert kwargs["metadata"] == {"type": {"stringValue": "ratings"}}
        assert kwargs["sessionId"] == "test-session-xyz"
        payload: list[dict[str, Any]] = kwargs["payload"]
        assert len(payload) == 1
        blob: dict[str, Any] = payload[0]["blob"]
        # Sanity: blob is a plain JSON-serializable dict (the "blob" Document
        # shape per CreateEvent's PayloadType — see agentcore.py
        # _emit_ratings_event), not a pre-serialized string.
        assert json.dumps(blob)
        rated_ids = {r["id"] for r in blob["ratings"]}
        assert rated_ids == {"r-cited", "r-ignored", "r-misled"}
        by_id = {r["id"]: r for r in blob["ratings"]}
        assert by_id["r-cited"]["class"] == "cited"
        assert by_id["r-cited"]["evidence"] == "cited it directly"
        assert by_id["r-ignored"]["class"] == "ignored"


class TestEvidenceRejectionParity:
    def test_non_ignored_without_evidence_rejects_whole_batch(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        _seed_local_reflection(local_conn, "r1", title="t1")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", "r1", None)],
            source="contextual",
        )
        with pytest.raises(ValueError, match="requires a non-empty evidence line"):
            backend_with_ledger.apply_session_ratings(
                session_id="test-session-xyz",
                ratings=[{"kind": "reflection", "id": "r1", "class": "shaped"}],
            )
        mock_data_client.get_memory_record.assert_not_called()
        mock_data_client.batch_update_memory_records.assert_not_called()
        mock_data_client.create_event.assert_not_called()
        row = local_conn.execute(
            "SELECT rated_at FROM session_memory_exposure"
        ).fetchone()
        assert row["rated_at"] is None


class TestIgnoredSweepOnlyParity:
    def test_ignored_accepted_in_sweep(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        _seed_local_reflection(local_conn, "r1", title="t1")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", "r1", None)],
            source="contextual",
        )
        mock_data_client.get_memory_record.return_value = _make_record_response("r1")
        mock_data_client.batch_update_memory_records.return_value = _ok_update_response("r1")

        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[{"kind": "reflection", "id": "r1", "class": "ignored"}],
        )
        assert result["applied"]["ignored"] == 1

    def test_ignored_rejected_in_credit_one(self, backend_with_ledger) -> None:
        with pytest.raises(
            ValueError,
            match=r"credit_one does not accept classification='ignored'",
        ):
            backend_with_ledger.credit_one(
                session_id="test-session-xyz",
                kind="reflection",
                id="r1",
                classification="ignored",
            )


class TestEventFailureIsolation:
    def test_create_event_raising_does_not_fail_the_sweep(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        _seed_local_reflection(local_conn, "r1", title="t1")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", "r1", None)],
            source="contextual",
        )
        mock_data_client.get_memory_record.return_value = _make_record_response("r1")
        mock_data_client.batch_update_memory_records.return_value = _ok_update_response("r1")
        mock_data_client.create_event.side_effect = Exception("AWS is down")

        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[{"kind": "reflection", "id": "r1", "class": "cited",
                      "evidence": "cited it"}],
        )
        assert result["applied"]["cited"] == 1
        mock_data_client.create_event.assert_called_once()

        # The local stamp + AWS counter bump are unaffected by the event failure.
        row = local_conn.execute(
            "SELECT rated_at FROM session_memory_exposure WHERE memory_id = 'r1'"
        ).fetchone()
        assert row["rated_at"] is not None


class TestCounterPushFailureIsolation:
    """Reviewer finding: apply_session_ratings' AWS counter push used to
    catch only ResourceNotFoundException — any other ClientError (a
    throttle, an access-denied; batch APIs share a 20 TPS pool, so
    throttling is plausible) raised out of the sweep mid-loop, aborting
    remaining entries even though the local stamp for the failing entry
    had already committed. Fixed: the local-stamp-then-AWS-push ORDER is
    unchanged (spec: local stamps are the session's evidence-of-record and
    must not be lost to a throttled/denied AWS call — "counters are
    statistics, not ledgers"); the AWS push's catch widened to
    best-effort-Exception, counting the entry as applied (it WAS applied
    to the local ledger) and continuing the sweep."""

    def test_non_404_client_error_on_one_entry_does_not_abort_the_sweep(
        self, backend_with_ledger, local_conn, mock_data_client, monkeypatch
    ) -> None:
        from better_memory.storage import agentcore as ac_module

        monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)

        for rid in ("r1", "r2", "r3"):
            _seed_local_reflection(local_conn, rid, title=f"title-{rid}")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[
                ("reflection", "r1", None),
                ("reflection", "r2", None),
                ("reflection", "r3", None),
            ],
            source="contextual",
        )

        mock_data_client.get_memory_record.side_effect = [
            _make_record_response("r1"),
            _make_record_response("r2"),
            _make_record_response("r3"),
        ]
        mock_data_client.batch_update_memory_records.side_effect = [
            _ok_update_response("r1"),
            _FakeClientError(code="ThrottlingException", message="rate exceeded"),
            _ok_update_response("r3"),
        ]
        mock_data_client.create_event.return_value = {"event": {"eventId": "evt-1"}}

        # Must not raise.
        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited",
                 "evidence": "cited it"},
                {"kind": "reflection", "id": "r2", "class": "shaped",
                 "evidence": "throttled mid-sweep"},
                {"kind": "reflection", "id": "r3", "class": "misled",
                 "evidence": "led me astray"},
            ],
        )

        # All three entries were attempted (no abort), and all three count
        # as applied — r2's counter push failed, but its local stamp is
        # the session's evidence-of-record and the rating IS applied.
        assert result["applied"] == {
            "cited": 1, "shaped": 1, "ignored": 0, "misled": 1, "overlooked": 0,
        }
        assert result["skipped"] == {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }
        assert mock_data_client.batch_update_memory_records.call_count == 3

        # r2's local stamp is intact despite the AWS failure.
        rows = {
            r["memory_id"]: r
            for r in local_conn.execute(
                "SELECT memory_id, rated_at, classification FROM session_memory_exposure"
            ).fetchall()
        }
        for rid, cls in (("r1", "cited"), ("r2", "shaped"), ("r3", "misled")):
            assert rows[rid]["rated_at"] is not None
            assert rows[rid]["classification"] == cls

        # The receipt event still fires exactly once, including r2.
        mock_data_client.create_event.assert_called_once()
        payload: list[dict[str, Any]] = mock_data_client.create_event.call_args.kwargs["payload"]
        blob: dict[str, Any] = payload[0]["blob"]
        rated_ids = {r["id"] for r in blob["ratings"]}
        assert rated_ids == {"r1", "r2", "r3"}

    def test_all_counter_pushes_failing_still_returns_normally(
        self, backend_with_ledger, local_conn, mock_data_client, monkeypatch
    ) -> None:
        from better_memory.storage import agentcore as ac_module

        monkeypatch.setattr(ac_module, "_ClientError", _FakeClientError)

        for rid in ("r1", "r2"):
            _seed_local_reflection(local_conn, rid, title=f"title-{rid}")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", "r1", None), ("reflection", "r2", None)],
            source="contextual",
        )

        mock_data_client.get_memory_record.side_effect = [
            _make_record_response("r1"),
            _make_record_response("r2"),
        ]
        mock_data_client.batch_update_memory_records.side_effect = [
            _FakeClientError(code="AccessDeniedException", message="not authorized"),
            _FakeClientError(code="ThrottlingException", message="rate exceeded"),
        ]
        mock_data_client.create_event.return_value = {"event": {"eventId": "evt-1"}}

        # Must not raise even though every single counter push fails.
        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited",
                 "evidence": "cited it"},
                {"kind": "reflection", "id": "r2", "class": "overlooked",
                 "evidence": "retrieved, never used"},
            ],
        )

        assert result["applied"] == {
            "cited": 1, "shaped": 0, "ignored": 0, "misled": 0, "overlooked": 1,
        }
        assert result["skipped"] == {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }

        rows = {
            r["memory_id"]: r
            for r in local_conn.execute(
                "SELECT memory_id, rated_at FROM session_memory_exposure"
            ).fetchall()
        }
        assert rows["r1"]["rated_at"] is not None
        assert rows["r2"]["rated_at"] is not None

        mock_data_client.create_event.assert_called_once()


class TestSkipBuckets:
    def test_not_exposed_and_already_rated(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        _seed_local_reflection(local_conn, "r1", title="t1")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", "r1", None)],
            source="contextual",
        )
        mock_data_client.get_memory_record.return_value = _make_record_response("r1")
        mock_data_client.batch_update_memory_records.return_value = _ok_update_response("r1")

        # r2 was never exposed -> not_exposed; r1 applies normally.
        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited",
                 "evidence": "cited it"},
                {"kind": "reflection", "id": "r2", "class": "shaped",
                 "evidence": "never exposed"},
            ],
        )
        assert result["applied"]["cited"] == 1
        assert result["skipped"]["not_exposed"] == 1

        # Re-rating r1 in a second sweep call now finds it already_rated.
        result2 = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "shaped",
                 "evidence": "second pass"},
            ],
        )
        assert result2["applied"]["shaped"] == 0
        assert result2["skipped"]["already_rated"] == 1
        # No second AWS write for r1 — only the one from the first sweep.
        assert mock_data_client.batch_update_memory_records.call_count == 1


class TestNoLocalConnDegrade:
    def test_local_conn_none_degrades_to_current_behaviour(
        self, backend, mock_data_client
    ) -> None:
        """No local ledger wired: never raises, skip-bucket counting stays
        at zero (no ledger to consult), and the AWS counter push still
        happens exactly as before this task."""
        mock_data_client.get_memory_record.return_value = _make_record_response("r1")
        mock_data_client.batch_update_memory_records.return_value = _ok_update_response("r1")

        result = backend.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[{"kind": "reflection", "id": "r1", "class": "cited",
                      "evidence": "cited it"}],
        )
        assert result["applied"]["cited"] == 1
        assert result["skipped"] == {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }
        assert mock_data_client.batch_update_memory_records.call_count == 1


class TestLedgerFailureIsBestEffort:
    def test_mid_batch_ledger_commit_failure_does_not_abort_sweep(
        self, backend_with_ledger, local_conn, mock_data_client, monkeypatch
    ) -> None:
        """Docstring for apply_session_ratings promises step (b) is
        "best-effort, per-item" and the method "never raises once past the
        up-front batch validation in step (a)". A concurrent UI writer
        holding the write lock past the 5000 ms busy_timeout can make one
        entry's exposure_log.stamp / _local_conn.commit raise
        `database is locked` mid-batch — that entry must be dropped and
        the rest of the batch must keep sweeping, rather than abort the
        whole sweep and lose entries 4-6 (bug: #108)."""
        for rid in ("r1", "r2", "r3"):
            _seed_local_reflection(local_conn, rid, title=f"t-{rid}")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", rid, None) for rid in ("r1", "r2", "r3")],
            source="contextual",
        )
        mock_data_client.get_memory_record.side_effect = [
            _make_record_response(rid) for rid in ("r1", "r3")
        ]
        mock_data_client.batch_update_memory_records.side_effect = [
            _ok_update_response(rid) for rid in ("r1", "r3")
        ]
        mock_data_client.create_event.return_value = {"event": {"eventId": "evt-1"}}

        # Simulate a ledger op raising on the middle entry only.
        import better_memory.services.exposure_log as exposure_log

        real_stamp = exposure_log.stamp
        call_ids: list[str] = []

        def _stamp_maybe_raise(conn, *, session_id, kind, memory_id, **kw):
            call_ids.append(memory_id)
            if memory_id == "r2":
                raise __import__("sqlite3").OperationalError("database is locked")
            return real_stamp(
                conn, session_id=session_id, kind=kind, memory_id=memory_id, **kw
            )

        monkeypatch.setattr(exposure_log, "stamp", _stamp_maybe_raise)

        result = backend_with_ledger.apply_session_ratings(
            session_id="test-session-xyz",
            ratings=[
                {"kind": "reflection", "id": "r1", "class": "cited",
                 "evidence": "e1"},
                {"kind": "reflection", "id": "r2", "class": "cited",
                 "evidence": "e2"},
                {"kind": "reflection", "id": "r3", "class": "cited",
                 "evidence": "e3"},
            ],
        )

        # r1 and r3 applied; r2 dropped (ledger raised, no AWS push, no
        # bucket increment because the return-shape SkippedCounts TypedDict
        # has no bucket for ledger errors — matching the "best-effort,
        # per-item" contract).
        assert result["applied"]["cited"] == 2
        assert result["skipped"] == {
            "not_exposed": 0, "already_rated": 0,
            "memory_missing": 0, "memory_retired": 0,
        }
        # r2's AWS credit must NOT have fired — that's the whole point:
        # without a committed stamp we'd double-credit on retry.
        assert mock_data_client.batch_update_memory_records.call_count == 2
        credited_ids = [
            c.kwargs["records"][0]["memoryRecordId"]
            for c in mock_data_client.batch_update_memory_records.call_args_list
        ]
        assert credited_ids == ["r1", "r3"]
        # The receipt CreateEvent still fires — non-empty rated_entries.
        assert mock_data_client.create_event.call_count == 1

    def test_commit_failure_after_stamp_is_rolled_back(
        self, backend_with_ledger, local_conn, mock_data_client
    ) -> None:
        """Regression: exposure_log.stamp's UPDATE lands in Python
        sqlite3's implicit deferred transaction. If it succeeds but the
        subsequent _local_conn.commit() raises (e.g. busy_timeout
        expires during commit), the pending UPDATE would otherwise ride
        the NEXT successful commit — flushing the dropped entry's
        stamp without any AWS credit and permanently losing the rating
        on retry (`skipped.already_rated`). The rollback in the except
        block must clear the pending transaction so r2's rated_at stays
        NULL, matching the docstring's "no committed stamp, no AWS
        credit" invariant. (Follow-up to review of #118.)"""
        for rid in ("r1", "r2", "r3"):
            _seed_local_reflection(local_conn, rid, title=f"t-{rid}")
        backend_with_ledger.record_exposures(
            session_id="test-session-xyz",
            items=[("reflection", rid, None) for rid in ("r1", "r2", "r3")],
            source="contextual",
        )
        mock_data_client.get_memory_record.side_effect = [
            _make_record_response(rid) for rid in ("r1", "r3")
        ]
        mock_data_client.batch_update_memory_records.side_effect = [
            _ok_update_response(rid) for rid in ("r1", "r3")
        ]
        mock_data_client.create_event.return_value = {"event": {"eventId": "evt-1"}}

        # Simulate a fault AFTER stamp's UPDATE has already been executed
        # against the shared connection — the exact "commit-time
        # busy_timeout" pattern the rollback protects. Wrapping stamp
        # here runs the real UPDATE (opening the deferred transaction),
        # then raises before commit — so r2's stamp is uncommitted and
        # pending on the connection when we jump into the except block.
        import better_memory.services.exposure_log as exposure_log

        real_stamp = exposure_log.stamp

        def _stamp_then_raise(conn, *, session_id, kind, memory_id, **kw):
            result = real_stamp(
                conn, session_id=session_id, kind=kind,
                memory_id=memory_id, **kw,
            )
            if memory_id == "r2":
                raise __import__("sqlite3").OperationalError(
                    "database is locked"
                )
            return result

        import unittest.mock as _mock
        stamp_patch = _mock.patch.object(exposure_log, "stamp", _stamp_then_raise)
        stamp_patch.start()
        try:
            result = backend_with_ledger.apply_session_ratings(
                session_id="test-session-xyz",
                ratings=[
                    {"kind": "reflection", "id": "r1", "class": "cited",
                     "evidence": "e1"},
                    {"kind": "reflection", "id": "r2", "class": "cited",
                     "evidence": "e2"},
                    {"kind": "reflection", "id": "r3", "class": "cited",
                     "evidence": "e3"},
                ],
            )
        finally:
            stamp_patch.stop()

        # r1 and r3 applied; r2 dropped.
        assert result["applied"]["cited"] == 2

        # THE ACTUAL POINT of the rollback: r2's exposure row must NOT
        # have rated_at set. Without the rollback, r3's successful
        # commit would flush r2's pending stamp too, and we'd read a
        # non-NULL rated_at here despite the dropped credit.
        rated_by_id = {
            row["memory_id"]: row["rated_at"]
            for row in local_conn.execute(
                "SELECT memory_id, rated_at FROM session_memory_exposure "
                "WHERE session_id = ? ORDER BY memory_id",
                ("test-session-xyz",),
            ).fetchall()
        }
        assert rated_by_id["r1"] is not None
        assert rated_by_id["r2"] is None, (
            "r2's pending stamp leaked past its own commit() failure — "
            "the rollback in the except block is not clearing the "
            "deferred transaction."
        )
        assert rated_by_id["r3"] is not None


class TestCreateEventSdkGuard:
    def test_botocore_create_event_supports_extraction_mode(self) -> None:
        """Guard against a silent regression, not a functional test: the
        installed botocore's bedrock-agentcore CreateEvent model must
        declare `extractionMode` — required for _emit_ratings_event's
        extractionMode='SKIP' receipt (keeps rated-batch payloads out of
        AgentCore's built-in LLM extraction). Older botocore (<1.43.56, the
        version originally pinned when Task 7 was written) lacks this
        member; a real (non-mocked) boto3 client then raises
        ParamValidationError on every call, which the sweep's best-effort
        try/except swallows — so the receipt event silently never fires,
        with zero test signal, unless something asserts on the SDK's shape
        directly. A future accidental downgrade of the boto3/botocore pins
        (pyproject.toml agentcore/dev groups) must fail THIS test loudly
        instead."""
        pytest.importorskip("botocore")
        import botocore.session

        model = botocore.session.get_session().get_service_model(
            "bedrock-agentcore"
        )
        input_shape = model.operation_model("CreateEvent").input_shape
        assert input_shape is not None
        # botocore's Shape.members is a CachedProperty (a hand-rolled
        # descriptor, not `@property`) — botocore ships no type stubs, so
        # pyright can't resolve the descriptor's return type statically.
        # Real attribute, confirmed at runtime; static-only false positive.
        members = input_shape.members  # pyright: ignore[reportAttributeAccessIssue]
        assert "extractionMode" in members
