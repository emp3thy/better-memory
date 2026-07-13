"""Hermetic unit tests for storage/agentcore_migrate.py (T4).

No AWS, no boto3. Exercises the pure record builders, the deterministic
content hash, the batching helper, and the SQLite migration ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from better_memory.storage import agentcore_migrate as m


def _rr(row, **kw):
    """build_reflection_record for an ACTIVE row, narrowed to non-None.

    Active rows always build a record; only retired/superseded rows return
    None (covered separately). Keeps pyright's optional-subscript analysis
    happy without changing any test's behavior."""
    rec = m.build_reflection_record(row, **kw)
    assert rec is not None
    return rec


# --------------------------------------------------------------------------- #
# Fixtures / row factories
# --------------------------------------------------------------------------- #
def _reflection_row(**over):
    row = {
        "id": "refl-123",
        "title": "Prefer body-first parse",
        "project": "better-memory",
        "tech": "python",
        "phase": "implementation",
        "polarity": "do",
        "use_cases": "when parsing records",
        "hints": json.dumps(["hint one", "hint two"]),
        "confidence": 0.8,
        "status": "pending_review",
        "superseded_by": None,
        "evidence_count": 5,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "scope": "project",
        "useful_count": 4,
        "last_useful_at": "2026-07-09T00:00:00+00:00",
        "times_misled": 1,
        "last_misled_at": "2026-07-08T00:00:00+00:00",
        "times_overlooked": 2,
        "last_overlooked_at": "2026-07-11T00:00:00+00:00",
    }
    row.update(over)
    return row


def _semantic_row(**over):
    row = {
        "id": "sem-77",
        "content": "The user prefers no emojis in output.",
        "project": "better-memory",
        "scope": "project",
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "useful_count": 3,
        "last_useful_at": "2026-07-05T00:00:00+00:00",
        "times_misled": 0,
        "last_misled_at": None,
        "times_overlooked": 1,
        "last_overlooked_at": "2026-07-06T00:00:00+00:00",
    }
    row.update(over)
    return row


@pytest.fixture()
def ledger_conn():
    conn = sqlite3.connect(":memory:")
    m.ensure_ledger(conn)
    yield conn
    conn.close()


# --------------------------------------------------------------------------- #
# build_reflection_record — ALL state in the body, NO metadata
# --------------------------------------------------------------------------- #
def test_reflection_all_state_in_body_no_metadata():
    rec = _rr(_reflection_row(), strategy_id="strat-episodic")

    # No custom metadata map is emitted (AWS would silently drop it, §1b).
    assert "metadata" not in rec

    body = json.loads(rec["content"]["text"])
    # Body carries the content fields AND the fields §3 put in metadata.
    for key in (
        "title", "use_cases", "hints", "confidence", "tech", "phase",
        "evidence_count", "updated_at", "polarity", "status", "useful_count",
        "times_misled", "times_overlooked", "source_row_id", "source_backend",
    ):
        assert key in body, f"{key} missing from body"

    assert body["hints"] == ["hint one", "hint two"]  # decoded to a list
    assert body["confidence"] == 0.8
    assert body["evidence_count"] == 5
    assert body["source_row_id"] == "refl-123"
    assert body["source_backend"] == "sqlite"
    assert rec["memoryStrategyId"] == "strat-episodic"
    assert "timestamp" in rec


def test_reflection_namespace_project_vs_general():
    proj = _rr(_reflection_row(), strategy_id="s")
    assert proj["namespaces"] == ["projects/better-memory/reflections/"]

    gen = _rr(
        _reflection_row(scope="general"), strategy_id="s"
    )
    assert gen["namespaces"] == ["general/reflections/"]


def test_reflection_status_remap():
    a = _rr(
        _reflection_row(status="pending_review"), strategy_id="s"
    )
    assert json.loads(a["content"]["text"])["status"] == "active"

    b = _rr(
        _reflection_row(status="confirmed"), strategy_id="s"
    )
    assert json.loads(b["content"]["text"])["status"] == "promoted"


def test_reflection_retired_and_superseded_skipped_on_create():
    assert m.build_reflection_record(
        _reflection_row(status="retired"), strategy_id="s"
    ) is None
    assert m.build_reflection_record(
        _reflection_row(status="superseded"), strategy_id="s"
    ) is None


def test_reflection_deterministic_request_identifier():
    r1 = _rr(_reflection_row(), strategy_id="s")
    r2 = _rr(
        _reflection_row(updated_at="2026-01-01T00:00:00+00:00"), strategy_id="s"
    )
    assert r1["requestIdentifier"] == "bm-reflection-refl-123"
    assert r1["requestIdentifier"] == r2["requestIdentifier"]


def test_reflection_request_identifier_truncated_to_80():
    long_id = "x" * 200
    rec = _rr(
        _reflection_row(id=long_id), strategy_id="s"
    )
    assert len(rec["requestIdentifier"]) == 80
    assert rec["requestIdentifier"].startswith("bm-reflection-x")


# --------------------------------------------------------------------------- #
# build_semantic_record — raw text content + DECLARED metadata
# --------------------------------------------------------------------------- #
def test_semantic_content_is_raw_text_not_json():
    rec = m.build_semantic_record(_semantic_row(), strategy_id="strat-pref")
    assert rec["content"]["text"] == "The user prefers no emojis in output."
    # Raw text, not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(rec["content"]["text"])
    assert rec["memoryStrategyId"] == "strat-pref"


def test_semantic_declares_source_row_id_and_counters_in_metadata():
    rec = m.build_semantic_record(_semantic_row(), strategy_id="s")
    md = rec["metadata"]

    assert md["source_row_id"] == {"stringValue": "sem-77"}
    assert md["useful_count"] == {"numberValue": 3}
    assert md["times_misled"] == {"numberValue": 0}
    # times_overlooked renamed to overlooked_count.
    assert md["overlooked_count"] == {"numberValue": 1}
    assert "times_overlooked" not in md
    assert md["status"] == {"stringValue": "active"}


def test_semantic_last_credited_at_is_string_value():
    rec = m.build_semantic_record(_semantic_row(), strategy_id="s")
    lc = rec["metadata"]["last_credited_at"]
    # MUST be stringValue (declared STRING indexed key); dateTimeValue rejects
    # the whole record (§3 landmine).
    assert "stringValue" in lc
    assert "dateTimeValue" not in lc
    # Max of the three last_*_at timestamps.
    assert lc["stringValue"] == "2026-07-06T00:00:00+00:00"


def test_semantic_request_identifier_deterministic():
    rec = m.build_semantic_record(_semantic_row(), strategy_id="s")
    assert rec["requestIdentifier"] == "bm-semantic-sem-77"


def test_semantic_namespace_project_vs_general():
    proj = m.build_semantic_record(_semantic_row(), strategy_id="s")
    assert proj["namespaces"] == ["projects/better-memory/semantic/"]
    gen = m.build_semantic_record(_semantic_row(scope="general"), strategy_id="s")
    assert gen["namespaces"] == ["general/semantic/"]


# --------------------------------------------------------------------------- #
# chunk() batching
# --------------------------------------------------------------------------- #
def test_chunk_batches_evenly_and_remainder():
    records = [{"i": i} for i in range(7)]
    batches = list(m.chunk(records, 3))
    assert [len(b) for b in batches] == [3, 3, 1]
    # Order + content preserved.
    assert [r["i"] for b in batches for r in b] == list(range(7))


def test_chunk_empty_and_single_batch():
    assert list(m.chunk([], 5)) == []
    assert list(m.chunk([{"a": 1}], 5)) == [[{"a": 1}]]


def test_chunk_rejects_zero_batch_size():
    with pytest.raises(ValueError):
        list(m.chunk([{"a": 1}], 0))


# --------------------------------------------------------------------------- #
# canonical_content_hash — deterministic, timestamp-independent
# --------------------------------------------------------------------------- #
def test_content_hash_ignores_timestamp():
    r1 = _rr(
        _reflection_row(), strategy_id="s",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    r2 = _rr(
        _reflection_row(), strategy_id="s",
        timestamp=datetime(2026, 12, 31, tzinfo=UTC),
    )
    assert m.canonical_content_hash(r1) == m.canonical_content_hash(r2)


def test_content_hash_changes_with_body():
    base = _rr(_reflection_row(), strategy_id="s")
    changed = _rr(
        _reflection_row(confidence=0.1), strategy_id="s"
    )
    assert m.canonical_content_hash(base) != m.canonical_content_hash(changed)


def test_content_hash_excludes_memory_strategy_id():
    # The strategy id is a routing attribute filled in only after provisioning
    # (§5.2). Re-keying it MUST NOT change the hash, else the --provision path
    # forces a spurious update on every later run.
    a = _rr(_reflection_row(), strategy_id="")
    b = _rr(_reflection_row(), strategy_id="real-strat")
    assert a["memoryStrategyId"] != b["memoryStrategyId"]
    assert m.canonical_content_hash(a) == m.canonical_content_hash(b)

    sa = m.build_semantic_record(_semantic_row(), strategy_id="")
    sb = m.build_semantic_record(_semantic_row(), strategy_id="real-strat")
    assert m.canonical_content_hash(sa) == m.canonical_content_hash(sb)


# --------------------------------------------------------------------------- #
# push_batch — thin wrapper
# --------------------------------------------------------------------------- #
def test_push_batch_calls_data_client():
    calls = {}

    class _Client:
        def batch_create_memory_records(self, *, memoryId, records):
            calls["memoryId"] = memoryId
            calls["records"] = records
            return {"successfulRecords": records, "failedRecords": []}

    resp = m.push_batch(_Client(), "mem-episodic", [{"a": 1}, {"a": 2}])
    assert calls["memoryId"] == "mem-episodic"
    assert calls["records"] == [{"a": 1}, {"a": 2}]
    assert resp["failedRecords"] == []


# --------------------------------------------------------------------------- #
# Ledger decisions
# --------------------------------------------------------------------------- #
def test_ledger_no_entry_is_create(ledger_conn):
    assert m.plan_row(ledger_conn, "reflection", "refl-1", "hashA") == "create"


def test_ledger_unchanged_hash_is_skip(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="projects/p/reflections/", content_hash="hashA",
        target_record_id="mem-xyz",
    )
    assert m.plan_row(ledger_conn, "reflection", "refl-1", "hashA") == "skip"


def test_ledger_changed_hash_is_update(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="projects/p/reflections/", content_hash="hashA",
        target_record_id="mem-xyz",
    )
    assert m.plan_row(ledger_conn, "reflection", "refl-1", "hashB") == "update"


def test_ledger_sqlite_retired_is_retire(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="projects/p/reflections/", content_hash="hashA",
        target_record_id="mem-xyz",
    )
    # content_hash=None signals "source row no longer eligible" (retired).
    assert m.plan_row(ledger_conn, "reflection", "refl-1", None) == "retire"


def test_ledger_retire_no_prior_entry_is_skip(ledger_conn):
    assert m.plan_row(ledger_conn, "reflection", "never", None) == "skip"


def test_ledger_already_retired_is_skip(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="ns", content_hash="hashA", target_record_id="mem-xyz",
        status="retired",
    )
    assert m.plan_row(ledger_conn, "reflection", "refl-1", None) == "skip"


def test_ledger_failed_status_retries_as_create(ledger_conn):
    m.record_failure(
        ledger_conn, kind="reflection", source_id="refl-1",
        last_error="boom", content_hash="hashA",
    )
    assert m.plan_row(ledger_conn, "reflection", "refl-1", "hashA") == "create"


def test_ledger_record_failure_then_success_clears_error(ledger_conn):
    m.record_failure(
        ledger_conn, kind="semantic", source_id="sem-1", last_error="boom",
    )
    m.record_success(
        ledger_conn, kind="semantic", source_id="sem-1",
        namespace="ns", content_hash="hashZ", target_record_id="mem-1",
    )
    r = ledger_conn.execute(
        "SELECT status, last_error, target_record_id, content_hash "
        "FROM agentcore_migration WHERE source_kind='semantic' AND source_id='sem-1'"
    ).fetchone()
    assert r == ("migrated", None, "mem-1", "hashZ")


def test_ledger_retire_preserves_target_and_hash(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="ns", content_hash="hashA", target_record_id="mem-xyz",
    )
    # Retire transition: only status flips; target + hash preserved.
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1", status="retired",
    )
    r = ledger_conn.execute(
        "SELECT status, target_record_id, content_hash FROM agentcore_migration "
        "WHERE source_kind='reflection' AND source_id='refl-1'"
    ).fetchone()
    assert r == ("retired", "mem-xyz", "hashA")


def test_reconcile_ledger_reattaches_when_no_entry(ledger_conn):
    # Lost/absent ledger: reconcile binds a remote record's server id so the
    # next plan_row yields 'update' (empty hash) rather than a duplicate create.
    reattached = m.reconcile_ledger(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="projects/p/reflections/", target_record_id="mem-remote",
    )
    assert reattached is True
    r = ledger_conn.execute(
        "SELECT status, target_record_id, content_hash FROM agentcore_migration "
        "WHERE source_kind='reflection' AND source_id='refl-1'"
    ).fetchone()
    assert r == ("migrated", "mem-remote", "")
    # Empty stored hash != any real content hash -> plan_row returns 'update'.
    assert m.plan_row(ledger_conn, "reflection", "refl-1", "realhash") == "update"


def test_reconcile_ledger_preserves_existing_target(ledger_conn):
    m.record_success(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="ns", content_hash="hashA", target_record_id="mem-original",
    )
    reattached = m.reconcile_ledger(
        ledger_conn, kind="reflection", source_id="refl-1",
        namespace="ns", target_record_id="mem-DIFFERENT",
    )
    assert reattached is False
    r = ledger_conn.execute(
        "SELECT target_record_id, content_hash FROM agentcore_migration "
        "WHERE source_kind='reflection' AND source_id='refl-1'"
    ).fetchone()
    # Untouched: original target + hash preserved.
    assert r == ("mem-original", "hashA")


def test_reconcile_ledger_reattaches_failed_row(ledger_conn):
    # A create that failed locally but actually landed remotely: reconcile
    # binds the found id so the retry updates instead of re-creating.
    m.record_failure(
        ledger_conn, kind="semantic", source_id="sem-1", last_error="boom",
    )
    reattached = m.reconcile_ledger(
        ledger_conn, kind="semantic", source_id="sem-1",
        namespace="ns", target_record_id="mem-found",
    )
    assert reattached is True
    r = ledger_conn.execute(
        "SELECT status, target_record_id, last_error FROM agentcore_migration "
        "WHERE source_kind='semantic' AND source_id='sem-1'"
    ).fetchone()
    assert r == ("migrated", "mem-found", None)


def test_ensure_ledger_is_idempotent(ledger_conn):
    # Second call must not raise.
    m.ensure_ledger(ledger_conn)
    cols = {
        r[1]
        for r in ledger_conn.execute("PRAGMA table_info(agentcore_migration)")
    }
    assert cols == {
        "source_kind", "source_id", "namespace", "target_record_id",
        "content_hash", "status", "last_error", "migrated_at",
    }
